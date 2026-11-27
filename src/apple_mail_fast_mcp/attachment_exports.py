"""Export the attachments of many messages chosen by a query.

Fork-local. ``save_attachments`` takes one message per call, which makes a
job of thousands of messages impractical. Here the server selects with an
IMAP SEARCH, shows what it found, holds that exact UID list, and a later
commit downloads those messages a bounded slice at a time and writes their
attachments under names built from a template.

What matters most, in order:

* **Mail is only ever read.** The mailbox is opened read-only and bodies are
  fetched with ``BODY.PEEK[]``, so not even the read flag changes. There is
  no flag, copy, move or expunge call in this module.
* **A file can only land inside the directory that was asked for.** Every
  path component is cleaned on its own, folders are created one level at a
  time and never through a symlink, and the resolved parent is checked.
* **Nothing is dropped quietly.** A message whose subject does not fit the
  pattern is recorded and not guessed at. A file already at the target with
  different content is a reported ``name_collision``, never a silent skip.
* **A call is bounded by elapsed time** and reports ``more``, like the query
  move, because a client that gives up on a slow call may kill this process.

The files on disk are the state. The manifest CSV in the save directory is a
record of which message produced which file; a rerun uses it only to avoid
downloading a message again, and only when the files it names are still
there at the recorded size.

IMAP only. There is no AppleScript fallback.
"""

from __future__ import annotations

import contextlib
import csv
import fcntl
import hashlib
import importlib
import logging
import os
import re
import secrets
import string
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from imapclient.exceptions import IMAPClientAbortError

from . import date_windows
from .draft_builder import extract_attachment_payloads
from .exceptions import MailError
from .imap_connector import (
    _decode,
    _decode_mime_header,
    _format_sender,
    _reject_control_chars,
    _search_charset,
    _strip_brackets,
)
from .query_moves import (
    CommitInProgressError,
    PreviewNotFoundError,
    PreviewTooLargeError,
    SelectionStaleError,
    Selector,
    _identity,
    _is_timeout,
    _socket_timeout,
    _uidvalidity,
    build_criteria,
)
from .utils import extract_address, rank_senders, sanitize_output_filename


def _pattern_tree(pattern: str) -> Any:
    """The parsed form of a pattern, from the standard library's own parser.

    The parser moved in Python 3.11 (``re._parser``, formerly ``sre_parse``),
    so it is looked up by name.
    """
    try:
        module = importlib.import_module("re._parser")
    except ImportError:  # pragma: no cover - Python 3.10
        module = importlib.import_module("sre_parse")
    return module.parse(pattern)


logger = logging.getLogger(__name__)

EXPORT_CEILING = 2000
"""Most messages one preview will hold. Lower than the move ceiling because
the only live timing is about 11 s to read 2,486 envelopes on iCloud."""

PREVIEW_TTL_SECONDS = 900.0
PREVIEW_BUDGET_SECONDS = 20.0
TIME_BUDGET_SECONDS = 20.0
"""A commit call starts no new batch after this long. Sized well under the
roughly 60 s after which a client was seen to restart the server."""

FETCH_CHUNK = 100
"""UIDs per metadata fetch in the preview."""

MAX_BATCH = 10
"""Most messages per body fetch."""

BATCH_BYTES = 8 * 1024 * 1024
"""Most message bytes per body fetch, by the sizes read at preview."""

SLOW_BATCH_SECONDS = 8.0
"""A batch slower than this halves the next one."""

FAST_BATCH_SECONDS = 3.0
"""A batch faster than this lets the next one grow back."""

RETRY_AFTER_SECONDS = 60

SENDER_LIMIT = 20
SAMPLE_LIMIT = 5
SUBJECT_LIMIT = 300
"""Subjects are cut to this before the caller's pattern is applied."""

PATTERN_LIMIT = 200
TEMPLATE_LIMIT = 300
SUFFIX_LIMIT = 1000

MANIFEST_NAME = "export_manifest.csv"
LOCK_NAME = ".export_manifest.lock"
MANIFEST_FIELDS = (
    "exported_at", "account", "mailbox", "uidvalidity", "uid", "message_id",
    "subject", "date_received", "sender", "attachment_index",
    "attachment_name", "saved_path", "size", "sha256", "status", "note",
)

IF_EXISTS = ("skip", "suffix", "overwrite")

# Statuses that mean "this message's file is on disk".
_FILE_STATUSES = frozenset({"saved", "identical_existing", "renamed", "overwritten"})

_TEXT_TOKENS = frozenset({"name", "stem", "ext", "subject", "sender", "index", "uid"})
_NAME_TOKENS = frozenset({"name", "stem", "ext", "index"})
_DATE_TOKENS = frozenset({"received", "sent", "subject_date"})
_DATE_DIRECTIVES = frozenset("YymdbBHMSjaA")
_DATE_LITERALS = frozenset(" .-_")
_PARSE_DIRECTIVES = frozenset("YymdbBj")
_DEFAULT_DATE_SPEC = "%Y-%m-%d"


class ExportError(MailError):
    """Base for export failures."""


class _Connector(Protocol):
    def _session(self) -> Any: ...


# --------------------------------------------------------------- templates


@dataclass(frozen=True)
class _Template:
    """A parsed template: literal text and token pieces, in order."""

    pieces: tuple[tuple[str, str | None, str], ...]  # literal, token, date spec

    @property
    def tokens(self) -> frozenset[str]:
        return frozenset(token for _, token, _ in self.pieces if token)


@dataclass(frozen=True)
class Naming:
    """How each exported file is named and filed."""

    filename: _Template
    subfolders: tuple[_Template, ...]
    pattern: re.Pattern[str] | None
    date_format: str | None

    @property
    def tokens(self) -> frozenset[str]:
        used = set(self.filename.tokens)
        for part in self.subfolders:
            used |= part.tokens
        return frozenset(used)


def _check_date_spec(spec: str, what: str, allowed: frozenset[str]) -> None:
    position = 0
    while position < len(spec):
        char = spec[position]
        if char == "%":
            directive = spec[position + 1:position + 2]
            if directive not in allowed:
                raise ValueError(
                    f"{what}: %{directive or ''} is not allowed; use only "
                    f"{', '.join('%' + d for d in sorted(allowed))}"
                )
            position += 2
            continue
        if char not in _DATE_LITERALS and not char.isdigit():
            raise ValueError(
                f"{what}: {char!r} is not allowed in a date format; between "
                f"the % parts use only space, dot, dash and underscore"
            )
        position += 1


def _parse_template(text: str, what: str, groups: frozenset[str]) -> _Template:
    if not text or not text.strip():
        raise ValueError(f"{what} must not be empty")
    if len(text) > TEMPLATE_LIMIT:
        raise ValueError(f"{what} is longer than {TEMPLATE_LIMIT} characters")
    _reject_control_chars(text, what)
    pieces: list[tuple[str, str | None, str]] = []
    try:
        parsed = list(string.Formatter().parse(text))
    except ValueError as exc:
        raise ValueError(f"{what}: {exc}") from exc
    for literal, token, spec, conversion in parsed:
        if token is None:
            pieces.append((literal, None, ""))
            continue
        if conversion:
            raise ValueError(f"{what}: conversions such as !{conversion} are not allowed")
        if not token.isidentifier():
            # Rejects "", "0", "name.attr" and "name[0]" alike.
            raise ValueError(f"{what}: {{{token}}} is not a token")
        spec = spec or ""
        if token in _DATE_TOKENS:
            _check_date_spec(spec, f"{what} {{{token}}}", _DATE_DIRECTIVES)
        elif token in _TEXT_TOKENS or token in groups:
            if spec:
                raise ValueError(
                    f"{what}: {{{token}}} takes no format; only date tokens do"
                )
        else:
            raise ValueError(
                f"{what}: unknown token {{{token}}}. Tokens are "
                f"{', '.join(sorted(_TEXT_TOKENS | _DATE_TOKENS))}, plus the "
                f"named groups of subject_pattern"
            )
        pieces.append((literal, token, spec))
    return _Template(tuple(pieces))


def _has_repeat(items: Any) -> bool:
    return any(_repeats(op, value) for op, value in items)


def _repeats(op: Any, value: Any) -> bool:
    name = str(op)
    if name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
        return True
    if name == "SUBPATTERN":
        return _has_repeat(value[3])
    if name == "BRANCH":
        return any(_has_repeat(branch) for branch in value[1])
    return False


def _check_pattern_shape(items: Any, inside_repeat: bool = False) -> None:
    """Refuse the constructs that make a pattern take exponential time."""
    for op, value in items:
        name = str(op)
        if name in ("ASSERT", "ASSERT_NOT"):
            raise ValueError("subject_pattern: lookahead and lookbehind are not allowed")
        if name in ("GROUPREF", "GROUPREF_EXISTS"):
            raise ValueError("subject_pattern: backreferences are not allowed")
        if name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT"):
            _low, high, inner = value
            wide = high > 1
            if wide and (_has_repeat(inner) or any(str(o) == "BRANCH" for o, _ in _flat(inner))):
                raise ValueError(
                    "subject_pattern: a repeat inside a repeat (or a choice "
                    "inside a repeat) is not allowed; it can take far too long"
                )
            _check_pattern_shape(inner, inside_repeat or wide)
        elif name == "SUBPATTERN":
            _check_pattern_shape(value[3], inside_repeat)
        elif name == "BRANCH":
            for branch in value[1]:
                _check_pattern_shape(branch, inside_repeat)


def _flat(items: Any) -> Iterator[tuple[Any, Any]]:
    for op, value in items:
        yield op, value
        if str(op) == "SUBPATTERN":
            yield from _flat(value[3])


def _compile_pattern(pattern: str) -> re.Pattern[str]:
    if not pattern.strip():
        raise ValueError("subject_pattern must not be blank; omit it instead")
    if len(pattern) > PATTERN_LIMIT:
        raise ValueError(f"subject_pattern is longer than {PATTERN_LIMIT} characters")
    try:
        compiled = re.compile(pattern)
        _check_pattern_shape(_pattern_tree(pattern))
    except re.error as exc:
        raise ValueError(f"subject_pattern is not a valid pattern: {exc}") from exc
    clash = (set(compiled.groupindex) & (_TEXT_TOKENS | _DATE_TOKENS)) - {"date"}
    if clash:
        raise ValueError(
            f"subject_pattern: group name {sorted(clash)[0]!r} is a built-in "
            f"token; choose another name"
        )
    return compiled


def build_naming(
    filename_template: str = "{name}",
    subfolder_template: str | None = None,
    subject_pattern: str | None = None,
    subject_date_format: str | None = None,
) -> Naming:
    """Validate the naming options. Raises ValueError with what to fix."""
    pattern = _compile_pattern(subject_pattern) if subject_pattern is not None else None
    groups = frozenset(pattern.groupindex) - {"date"} if pattern else frozenset()

    filename = _parse_template(filename_template, "filename_template", groups)
    subfolders: list[_Template] = []
    if subfolder_template is not None:
        for part in subfolder_template.split("/"):
            if part.strip() in ("", ".", ".."):
                raise ValueError(
                    "subfolder_template must be a relative path of plain "
                    "folder names: no leading slash, no empty part, no . or .."
                )
            subfolders.append(_parse_template(part, "subfolder_template", groups))
    naming = Naming(filename, tuple(subfolders), pattern, subject_date_format)

    if "subject_date" in naming.tokens:
        if pattern is None or "date" not in pattern.groupindex:
            raise ValueError(
                "{subject_date} needs subject_pattern to have a group named "
                "date, for example (?P<date>\\d{2}[A-Za-z]{3}\\d{2})"
            )
        if not subject_date_format:
            raise ValueError(
                "{subject_date} needs subject_date_format, the layout of the "
                "date in the subject, for example %d%b%y for 04May26"
            )
    if subject_date_format is not None:
        if not subject_date_format.strip() or len(subject_date_format) > 40:
            raise ValueError("subject_date_format must be 1 to 40 characters")
        _check_date_spec(subject_date_format, "subject_date_format", _PARSE_DIRECTIVES)
    return naming


def _clean_value(value: str) -> str:
    """Keep a value from mail to a single path component's worth of text."""
    return "".join(
        "_" if ch in "/\\" or ord(ch) < 32 or 127 <= ord(ch) < 160 else ch
        for ch in value
    )


def message_values(
    *,
    subject: str,
    sender: str,
    received: datetime | None,
    sent: datetime | None,
    uid: int,
    naming: Naming,
) -> dict[str, Any] | None:
    """Token values for one message, or None if it does not fit the pattern.

    None is "do not export this one": a subject the caller's pattern does
    not match, a date in it that does not parse, or a date token the message
    has no value for. Nothing is guessed in its place.
    """
    subject = subject[:SUBJECT_LIMIT]
    values: dict[str, Any] = {
        "subject": subject,
        "sender": sender,
        "uid": str(uid),
        "received": received,
        "sent": sent,
    }
    if naming.pattern is not None:
        match = naming.pattern.search(subject)
        if match is None:
            return None
        for group, text in match.groupdict().items():
            if text is None:
                return None
            if group == "date":
                if naming.date_format:
                    try:
                        values["subject_date"] = datetime.strptime(
                            text, naming.date_format
                        )
                    except ValueError:
                        return None
            else:
                values[group] = text.strip()
    for token in naming.tokens & _DATE_TOKENS:
        if not isinstance(values.get(token), datetime):
            return None
    return values


def _render(template: _Template, values: dict[str, Any]) -> str:
    out: list[str] = []
    for literal, token, spec in template.pieces:
        out.append(literal)
        if token is None:
            continue
        value = values[token]
        if isinstance(value, datetime):
            out.append(value.strftime(spec or _DEFAULT_DATE_SPEC))
        else:
            out.append(_clean_value(str(value)))
    return "".join(out)


def render_path(
    naming: Naming, values: dict[str, Any], *, name: str, index: int
) -> tuple[tuple[str, ...], str]:
    """Folder names and file name for one attachment, each already safe."""
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    full = {
        **values,
        "name": name,
        "stem": stem,
        "ext": f".{ext}" if ext else "",
        "index": str(index),
    }
    folders = tuple(
        sanitize_output_filename(_render(part, full)) for part in naming.subfolders
    )
    return folders, sanitize_output_filename(_render(naming.filename, full))


# ------------------------------------------------------------------- store


@dataclass
class ExportEntry:
    account: str
    mailbox: str
    uidvalidity: int | None
    identity: tuple[str | None, str | None]
    total: int
    remaining: list[int]
    sizes: dict[int, int]
    save_directory: Path
    naming: Naming
    attachment_name_contains: str | None
    if_exists: str
    expires_at: float = 0.0
    batch_size: int = MAX_BATCH
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)


class ExportStore:
    """Previewed UID sets, in memory until used or expired.

    A server restart drops them, which is the safe direction: the caller
    previews again, and the manifest lets that preview leave out what is
    already on disk.
    """

    def __init__(
        self,
        ttl: float = PREVIEW_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl
        self._clock = clock
        self._entries: dict[str, ExportEntry] = {}
        self._lock = threading.Lock()

    @property
    def ttl(self) -> float:
        return self._ttl

    def _prune(self) -> None:
        now = self._clock()
        for key in [k for k, e in self._entries.items() if e.expires_at <= now]:
            del self._entries[key]

    def put(self, entry: ExportEntry) -> str:
        preview_id = secrets.token_urlsafe(16)
        with self._lock:
            self._prune()
            entry.expires_at = self._clock() + self._ttl
            self._entries[preview_id] = entry
        return preview_id

    def get(self, preview_id: str) -> ExportEntry:
        with self._lock:
            self._prune()
            entry = self._entries.get(preview_id)
        if entry is None:
            raise PreviewNotFoundError(
                "preview not found: it is unknown, already completed, or "
                "older than the preview lifetime. Run the preview again."
            )
        return entry

    def touch(self, preview_id: str) -> None:
        """A long export is many calls; each one renews the lifetime."""
        with self._lock:
            entry = self._entries.get(preview_id)
            if entry is not None:
                entry.expires_at = self._clock() + self._ttl

    def discard(self, preview_id: str) -> None:
        with self._lock:
            self._entries.pop(preview_id, None)


# ---------------------------------------------------------------- manifest


def _csv_cell(value: Any) -> str:
    """Text for one cell, made inert for a spreadsheet.

    Subjects and names come from mail. A cell that begins with one of these
    characters is run as a formula when the file is opened.
    """
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


@contextlib.contextmanager
def _directory_lock(directory: Path) -> Iterator[None]:
    """One export at a time per save directory, across processes."""
    with open(directory / LOCK_NAME, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise CommitInProgressError(
                "another export into this directory is still running; wait "
                "for it to finish, then call again"
            ) from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _append_manifest(directory: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path = directory / MANIFEST_NAME
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS)
        if new:
            writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_cell(row.get(key, "")) for key in MANIFEST_FIELDS})
        handle.flush()
        os.fsync(handle.fileno())


def _exported_message_ids(directory: Path) -> tuple[set[str], bool]:
    """Message-IDs whose files the manifest names and the disk still has.

    Returns (ids, unreadable). A message counts only if every file row it
    has points at a file inside ``directory`` that exists at the recorded
    size. The manifest is evidence, not the authority: the files are.
    """
    path = directory / MANIFEST_NAME
    if not path.exists():
        return set(), False
    by_message: dict[str, list[dict[str, str]]] = {}
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                message_id = (row.get("message_id") or "").strip()
                if message_id:
                    by_message.setdefault(message_id, []).append(row)
    except (OSError, UnicodeDecodeError, csv.Error):
        return set(), True
    root = directory.resolve()
    done: set[str] = set()
    for message_id, rows in by_message.items():
        files = [r for r in rows if r.get("status") in _FILE_STATUSES]
        if files and all(_file_still_there(root, r) for r in files):
            done.add(message_id)
    return done, False


def _file_still_there(root: Path, row: dict[str, str]) -> bool:
    relative = row.get("saved_path") or ""
    try:
        size = int(row.get("size") or "")
        target = root / relative
        if target.is_symlink() or not target.resolve().is_relative_to(root):
            return False
        return target.is_file() and target.stat().st_size == size
    except (OSError, ValueError):
        return False


# ----------------------------------------------------------------- preview


def _check_directory(save_directory: Path) -> Path:
    if not save_directory.is_dir():
        raise ValueError(
            f"save_directory does not exist or is not a directory: {save_directory}"
        )
    return save_directory.resolve()


def _envelope_fields(data: dict[bytes, Any]) -> dict[str, Any]:
    envelope = data.get(b"ENVELOPE")
    received = data.get(b"INTERNALDATE")
    sent = getattr(envelope, "date", None)
    sender = _format_sender(envelope) if envelope is not None else ""
    return {
        "subject": _decode_mime_header(getattr(envelope, "subject", None)),
        "sender": extract_address(sender) or sender,
        "received": received if isinstance(received, datetime) else None,
        "sent": sent if isinstance(sent, datetime) else None,
        "message_id": _strip_brackets(_decode(getattr(envelope, "message_id", None))),
    }


class _WindowsInterrupted(Exception):
    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__("connection lost while reading received dates")
        self.result = result


def preview(
    connector: _Connector,
    store: ExportStore,
    *,
    account: str,
    mailbox: str,
    save_directory: Path,
    selector: Selector,
    naming: Naming,
    attachment_name_contains: str | None = None,
    if_exists: str = "skip",
    ceiling: int = EXPORT_CEILING,
    budget: float = PREVIEW_BUDGET_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Find what the selector matches and hold that set. Writes nothing."""
    _reject_control_chars(mailbox, "mailbox")
    if if_exists not in IF_EXISTS:
        raise ValueError(f"if_exists must be one of {', '.join(IF_EXISTS)}")
    if attachment_name_contains is not None and not attachment_name_contains.strip():
        raise ValueError("attachment_name_contains must not be blank; omit it instead")
    directory = _check_directory(save_directory)
    if selector.sender_not_contains or selector.read_status is not None or (
        selector.is_flagged is not None
    ):
        raise ValueError("exports filter by sender, subject and date only")
    criteria = build_criteria(selector)
    try:
        return _preview(
            connector, store, criteria,
            account=account, mailbox=mailbox, directory=directory,
            selector=selector, naming=naming,
            attachment_name_contains=attachment_name_contains,
            if_exists=if_exists, ceiling=ceiling, budget=budget, clock=clock,
        )
    except _WindowsInterrupted as lost:
        return {**lost.result, "windows_unavailable": True}


@dataclass
class _Found:
    """What the preview read from the server."""

    result: dict[str, Any]
    uidvalidity: int | None = None
    uids: list[int] = field(default_factory=list)
    read: dict[int, dict[str, Any]] = field(default_factory=dict)
    sizes: dict[int, int] = field(default_factory=dict)
    final: bool = False  # ``result`` is already the whole answer


def _read_matches(
    connector: _Connector,
    criteria: list[Any],
    *,
    mailbox: str,
    selector: Selector,
    ceiling: int,
    budget: float,
    clock: Callable[[], float],
) -> _Found:
    """Search, and read each match's envelope, received date and size."""
    charset = _search_charset(criteria)
    started = clock()
    with connector._session() as client, _socket_timeout(client):
        selected = client.select_folder(mailbox, readonly=True)
        uidvalidity = _uidvalidity(selected)
        if uidvalidity is None:
            raise ExportError(
                "the server did not report UIDVALIDITY for the mailbox; a "
                "preview cannot be bound to it safely"
            )
        matched = client.search(criteria, charset) if charset else client.search(criteria)
        uids = sorted(int(u) for u in matched)
        found = _Found({"matched": len(uids), "preview_id": None}, uidvalidity, uids)
        if not uids:
            found.final = True
            return found
        if len(uids) > ceiling:
            found.final = True
            found.result.update(over_limit=True, limit=ceiling)
            try:
                found.result.update(date_windows.suggest(
                    client, uids, ceiling=ceiling,
                    date_from=selector.date_from, date_to=selector.date_to,
                ))
            except (IMAPClientAbortError, OSError) as exc:
                # Raised through the session on purpose, so the broken
                # connection is dropped; the count is still returned.
                raise _WindowsInterrupted(found.result) from exc
            return found
        for start in range(0, len(uids), FETCH_CHUNK):
            if clock() - started >= budget:
                # Too slow to finish inside one call: say so rather than
                # hold a set that was only partly read.
                found.final = True
                found.result["over_budget"] = True
                return found
            chunk = uids[start:start + FETCH_CHUNK]
            fetched = client.fetch(chunk, ["ENVELOPE", "INTERNALDATE", "RFC822.SIZE"])
            for uid in chunk:
                data = fetched.get(uid)
                if not data or data.get(b"ENVELOPE") is None:
                    continue
                found.read[uid] = _envelope_fields(data)
                size = data.get(b"RFC822.SIZE")
                found.sizes[uid] = size if isinstance(size, int) else 0
    return found


@dataclass
class _Plan:
    """Which matches are left to export, and what their paths look like."""

    remaining: list[int] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)
    targets: Counter[str] = field(default_factory=Counter)
    no_match: int = 0
    already: int = 0


def _plan(
    uids: list[int],
    read: dict[int, dict[str, Any]],
    done_ids: set[str],
    naming: Naming,
) -> _Plan:
    plan = _Plan()
    for uid in uids:
        fields = read.get(uid)
        if fields is None:
            plan.remaining.append(uid)  # settled at commit, from the body fetch
            continue
        if fields["message_id"] and fields["message_id"] in done_ids:
            plan.already += 1
            continue
        plan.remaining.append(uid)
        values = message_values(
            subject=fields["subject"], sender=fields["sender"],
            received=fields["received"], sent=fields["sent"], uid=uid, naming=naming,
        )
        if values is None:
            plan.no_match += 1
            continue
        # The attachment's own name is not known until the body is read;
        # NAME and .EXT stand in for it here.
        folders, filename = render_path(naming, values, name="NAME.EXT", index=0)
        path = "/".join((*folders, filename))
        plan.targets[path] += 1
        if len(plan.samples) < SAMPLE_LIMIT:
            plan.samples.append(path)
    return plan


def _preview(
    connector: _Connector,
    store: ExportStore,
    criteria: list[Any],
    *,
    account: str,
    mailbox: str,
    directory: Path,
    selector: Selector,
    naming: Naming,
    attachment_name_contains: str | None,
    if_exists: str,
    ceiling: int,
    budget: float,
    clock: Callable[[], float],
) -> dict[str, Any]:
    found = _read_matches(
        connector, criteria, mailbox=mailbox, selector=selector,
        ceiling=ceiling, budget=budget, clock=clock,
    )
    result, uids, read, sizes = found.result, found.uids, found.read, found.sizes
    if found.final:
        return result
    uidvalidity = found.uidvalidity

    done_ids: set[str] = set()
    unreadable = False
    if if_exists == "skip":
        done_ids, unreadable = _exported_message_ids(directory)
    plan = _plan(uids, read, done_ids, naming)
    remaining = plan.remaining

    dates = [f["received"].date() for f in read.values() if f["received"]]
    ranked = rank_senders(
        [{"sender": f["sender"]} for f in read.values()], "address", max(len(read), 1)
    )
    result.update(
        to_export=len(remaining),
        already_exported=plan.already,
        pattern_no_match=plan.no_match,
        duplicate_target_paths=sum(n for n in plan.targets.values() if n > 1),
        oldest_date=min(dates).isoformat() if dates else None,
        newest_date=max(dates).isoformat() if dates else None,
        distinct_senders=len(ranked),
        senders=ranked[:SENDER_LIMIT],
        total_message_bytes=sum(sizes.get(u, 0) for u in remaining),
        sample_paths=plan.samples,
        if_exists=if_exists,
        manifest_path=str(directory / MANIFEST_NAME),
    )
    if unreadable:
        result["manifest_unreadable"] = True
    if not remaining:
        return result
    result["preview_id"] = store.put(ExportEntry(
        account=account,
        mailbox=mailbox,
        uidvalidity=uidvalidity,
        identity=_identity(connector),
        total=len(remaining),
        remaining=remaining,
        sizes=sizes,
        save_directory=directory,
        naming=naming,
        attachment_name_contains=attachment_name_contains,
        if_exists=if_exists,
    ))
    result["expires_in_seconds"] = int(store.ttl)
    result["commit_time_budget_seconds"] = int(TIME_BUDGET_SECONDS)
    return result


# ------------------------------------------------------------------ commit


@dataclass
class _Progress:
    """What one commit call got through before it stopped."""

    counts: Counter[str] = field(default_factory=Counter)
    done: int = 0
    bytes_written: int = 0
    slowest: float = 0.0
    error: str | None = None
    timed_out: bool = False


def _attachments(raw: bytes) -> list[tuple[str, bytes]]:
    """The one place this module depends on the attachment parser.

    Same enumeration ``save_attachments`` uses, so the indices agree.
    """
    return [(name, payload) for name, _main, _sub, payload in extract_attachment_payloads(raw)]


def _make_folders(root: Path, folders: tuple[str, ...]) -> Path:
    """Create ``folders`` under ``root`` one level at a time.

    Never through a symlink: a link inside the save directory could point
    anywhere, and ``mkdir(parents=True)`` would follow it.
    """
    current = root
    for name in folders:
        current = current / name
        if current.is_symlink():
            raise OSError(f"refusing to write through a symlink: {current}")
        if not current.exists():
            current.mkdir()
        elif not current.is_dir():
            raise OSError(f"a file is in the way of the folder: {current}")
    if not current.resolve().is_relative_to(root):
        raise OSError(f"folder resolves outside the save directory: {current}")
    return current


def _same_content(path: Path, payload: bytes, digest: str) -> bool:
    try:
        if path.is_symlink() or not path.is_file():
            return False
        if path.stat().st_size != len(payload):
            return False
        return hashlib.sha256(path.read_bytes()).hexdigest() == digest
    except OSError:
        return False


def _write(target: Path, payload: bytes, *, replace: bool) -> bool:
    """Write ``payload`` to ``target`` atomically. False if it already exists.

    The temp file is in the target's own folder, so the final link or
    replace stays on one filesystem. ``os.link`` is the no-clobber check.
    """
    handle = tempfile.NamedTemporaryFile(
        dir=target.parent, prefix=".export-", delete=False
    )
    temp = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
        if replace:
            os.replace(temp, target)
            return True
        try:
            os.link(temp, target)
        except FileExistsError:
            return False
        return True
    finally:
        with contextlib.suppress(OSError):
            temp.unlink()


def _place(target: Path, payload: bytes, digest: str, if_exists: str) -> tuple[str, Path]:
    """Put one attachment on disk. Returns (status, where it is)."""
    if not os.path.lexists(target):
        if _write(target, payload, replace=False):
            return "saved", target
    if _same_content(target, payload, digest):
        return "identical_existing", target
    if target.is_dir() and not target.is_symlink():
        raise OSError(f"a folder is in the way of the file: {target}")
    if if_exists == "overwrite":
        _write(target, payload, replace=True)
        return "overwritten", target
    if if_exists == "skip":
        return "name_collision", target
    stem, dot, ext = target.name.rpartition(".")
    if not dot or not stem:
        stem, ext = target.name, ""
    for number in range(2, SUFFIX_LIMIT):
        candidate = target.with_name(f"{stem} ({number}){'.' + ext if ext else ''}")
        if not os.path.lexists(candidate):
            if _write(candidate, payload, replace=False):
                return "renamed", candidate
        if _same_content(candidate, payload, digest):
            return "identical_existing", candidate
    raise OSError(f"too many files with this name already: {target}")


def _export_message(
    entry: ExportEntry,
    uid: int,
    data: dict[bytes, Any] | None,
    max_attachment_bytes: int,
    progress: _Progress,
) -> list[dict[str, Any]]:
    """Export one message's attachments. Returns its manifest rows."""
    base: dict[str, Any] = {
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "account": entry.account,
        "mailbox": entry.mailbox,
        "uidvalidity": entry.uidvalidity,
        "uid": uid,
    }
    raw = data.get(b"BODY[]") if data else None
    if not data or not isinstance(raw, (bytes, bytearray)) or not raw:
        progress.counts["missing"] += 1
        return [{**base, "status": "missing", "note": "gone since the preview"}]
    fields = _envelope_fields(data)
    base.update(
        message_id=fields["message_id"],
        subject=fields["subject"],
        sender=fields["sender"],
        date_received=fields["received"].isoformat() if fields["received"] else "",
    )
    values = message_values(
        subject=fields["subject"], sender=fields["sender"],
        received=fields["received"], sent=fields["sent"], uid=uid, naming=entry.naming,
    )
    if values is None:
        progress.counts["pattern_no_match"] += 1
        return [{**base, "status": "pattern_no_match"}]
    try:
        attachments = _attachments(bytes(raw))
    except Exception as exc:  # noqa: BLE001 - a malformed message, recorded
        progress.counts["errors"] += 1
        return [{**base, "status": "error", "note": f"could not be parsed: {exc}"}]

    wanted = (entry.attachment_name_contains or "").strip().casefold()
    rows: list[dict[str, Any]] = []
    for index, (name, payload) in enumerate(attachments):
        if wanted and wanted not in name.casefold():
            continue
        row = {**base, "attachment_index": index, "attachment_name": name,
               "size": len(payload)}
        if len(payload) > max_attachment_bytes:
            progress.counts["too_large"] += 1
            rows.append({**row, "status": "too_large"})
            continue
        digest = hashlib.sha256(payload).hexdigest()
        try:
            folders, filename = render_path(entry.naming, values, name=name, index=index)
            parent = _make_folders(entry.save_directory, folders)
            status, where = _place(parent / filename, payload, digest, entry.if_exists)
        except OSError as exc:
            progress.counts["errors"] += 1
            rows.append({**row, "status": "error", "note": str(exc)})
            continue
        key = "name_collisions" if status == "name_collision" else status
        progress.counts[key] += 1
        if status in ("saved", "renamed", "overwritten"):
            progress.bytes_written += len(payload)
        rows.append({
            **row,
            "status": status,
            "sha256": digest,
            "saved_path": str(where.relative_to(entry.save_directory)),
        })
    if not rows:
        progress.counts["no_matching_attachment"] += 1
        return [{**base, "status": "no_matching_attachment"}]
    return rows


def _next_batch(entry: ExportEntry, raw_cap: int) -> tuple[list[int], list[int]]:
    """The next UIDs to fetch, and any at the front too large to fetch."""
    batch: list[int] = []
    oversized: list[int] = []
    total = 0
    for uid in entry.remaining:
        size = entry.sizes.get(uid, 0)
        if size > raw_cap:
            if batch:
                break
            oversized.append(uid)
            continue
        if batch and (len(batch) >= entry.batch_size or total + size > BATCH_BYTES):
            break
        batch.append(uid)
        total += size
    return batch, oversized


def _next_size(size: int, took: float) -> int:
    if took >= SLOW_BATCH_SECONDS:
        return max(1, size // 2)
    if took <= FAST_BATCH_SECONDS:
        return min(MAX_BATCH, size * 2)
    return size


def _run(
    connector: _Connector,
    store: ExportStore,
    preview_id: str,
    entry: ExportEntry,
    max_attachment_bytes: int,
    time_budget: float,
    clock: Callable[[], float],
) -> _Progress:
    progress = _Progress()
    # A base64 body is about a third larger than what it carries.
    raw_cap = int(max_attachment_bytes * 1.4) + 1024 * 1024
    started = clock()
    try:
        with connector._session() as client, _socket_timeout(client):
            selected = client.select_folder(entry.mailbox, readonly=True)
            if _uidvalidity(selected) != entry.uidvalidity:
                store.discard(preview_id)
                raise SelectionStaleError(
                    "the mailbox was rebuilt since the preview (UIDVALIDITY "
                    "changed); nothing was exported. Preview again."
                )
            while entry.remaining:
                if progress.done and clock() - started >= time_budget:
                    break
                batch, oversized = _next_batch(entry, raw_cap)
                rows: list[dict[str, Any]] = []
                for uid in oversized:
                    progress.counts["too_large"] += 1
                    rows.append({
                        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "account": entry.account, "mailbox": entry.mailbox,
                        "uidvalidity": entry.uidvalidity, "uid": uid,
                        "size": entry.sizes.get(uid, 0), "status": "too_large",
                        "note": "message larger than the attachment limit",
                    })
                began = clock()
                fetched = (
                    client.fetch(batch, ["BODY.PEEK[]", "ENVELOPE", "INTERNALDATE"])
                    if batch else {}
                )
                for uid in batch:
                    rows.extend(_export_message(
                        entry, uid, fetched.get(uid), max_attachment_bytes, progress
                    ))
                _append_manifest(entry.save_directory, rows)
                handled = len(oversized) + len(batch)
                entry.remaining = entry.remaining[handled:]
                progress.done += handled
                took = clock() - began
                progress.slowest = max(progress.slowest, took)
                entry.batch_size = _next_size(entry.batch_size, took)
    except MailError:
        raise
    except Exception as exc:  # noqa: BLE001 - reported to the caller
        progress.error = str(exc) or type(exc).__name__
        if _is_timeout(exc):
            progress.timed_out = True
            entry.batch_size = max(1, entry.batch_size // 2)
    return progress


def commit(
    connector: _Connector,
    store: ExportStore,
    *,
    preview_id: str,
    max_count: int,
    max_attachment_bytes: int,
    time_budget: float = TIME_BUDGET_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Export the next slice of a previewed set. Repeat until ``complete``."""
    entry = store.get(preview_id)
    if not entry.lock.acquire(blocking=False):
        raise CommitInProgressError(
            "another commit call is still working on this preview; wait for "
            "it to finish, then call again"
        )
    try:
        if entry.total > max_count:
            raise PreviewTooLargeError(
                f"the preview holds {entry.total} messages, above max_count="
                f"{max_count}; nothing was exported"
            )
        if _identity(connector) != entry.identity:
            store.discard(preview_id)
            raise SelectionStaleError(
                "the account now resolves to a different server or login "
                "than the one previewed; nothing was exported. Preview again."
            )
        _check_directory(entry.save_directory)
        with _directory_lock(entry.save_directory):
            progress = _run(
                connector, store, preview_id, entry,
                max_attachment_bytes, time_budget, clock,
            )
    finally:
        entry.lock.release()

    counts = progress.counts
    result: dict[str, Any] = {
        "total": entry.total,
        "messages_done": progress.done,
        "remaining": len(entry.remaining),
        "saved": counts["saved"],
        "identical_existing": counts["identical_existing"],
        "name_collisions": counts["name_collisions"],
        "renamed": counts["renamed"],
        "overwritten": counts["overwritten"],
        "no_matching_attachment": counts["no_matching_attachment"],
        "pattern_no_match": counts["pattern_no_match"],
        "too_large": counts["too_large"],
        "missing": counts["missing"],
        "errors": counts["errors"],
        "bytes_written": progress.bytes_written,
        "slowest_batch_seconds": round(progress.slowest, 1),
        "manifest_path": str(entry.save_directory / MANIFEST_NAME),
    }
    if progress.error is not None:
        # The batch in flight stays in ``remaining``; nothing of it was
        # recorded, so the next call simply fetches it again.
        result["outcome"] = "failed"
        result["error"] = progress.error
        if progress.timed_out:
            result["server_slow"] = True
            result["retry_after_seconds"] = RETRY_AFTER_SECONDS
        store.touch(preview_id)
    elif entry.remaining:
        result["outcome"] = "more"
        store.touch(preview_id)
    else:
        result["outcome"] = "complete"
        store.discard(preview_id)
    return result
