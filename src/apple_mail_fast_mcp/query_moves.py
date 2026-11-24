"""Query-driven bulk move with a preview bound to the exact message set.

Fork-local. The ID-list tools make the caller carry every message ID through
its own context and make the server resolve each one again by header. Here
the server selects with an IMAP SEARCH, shows what it found, and holds that
exact UID list; a later commit moves those UIDs and nothing else.

Two properties matter more than speed:

* A selector is never silently widened. Blank filters are rejected rather
  than ignored, and at least one positive filter is required. A broad but
  valid filter (a date alone, say) is still possible; the preview's count
  and sender list, the server ceiling and the caller's ``max_count`` are
  what catch that.
* A commit moves only UIDs the preview saw. Mail delivered afterwards is
  never included, and a retry after a failure cannot move anything twice,
  because each chunk is re-checked for presence before it is moved.
* A commit call is bounded by elapsed time. A client that gives up on a slow
  call may kill this process outright (seen live: the call passed about a
  minute and the server was restarted mid-chunk), so each call stops well
  short of that and reports ``more``.
* On the UIDPLUS path every chunk is journalled to disk before its copy. If
  the process dies between the copy and the removal, the next commit
  finishes the removal instead of copying again, but only after finding
  every copy still in the destination (by the UIDs the server reported).
  If the copy is in doubt, or its copies have since gone, those messages
  are held back, never blindly retried and never removed.

IMAP only. Uses MOVE (RFC 6851) where the server advertises it, otherwise the
UIDPLUS (RFC 4315) sequence the ID-based move already uses: copy, flag
deleted, and expunge only those UIDs. iCloud is in the second group. There is
no AppleScript fallback: if IMAP cannot do it, the call fails.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple, Protocol

from imapclient.exceptions import IMAPClientAbortError, IMAPClientError

from .exceptions import (
    MailError,
    MailImapMoveUnsupportedError,
    MailMailboxNotFoundError,
)
from .imap_connector import (
    ImapConnector,
    _build_search_criteria,
    _format_sender,
    _reject_control_chars,
    _search_charset,
)
from .utils import rank_senders

logger = logging.getLogger(__name__)

MAX_PER_CALL = 1000
"""Most messages one commit call moves, whatever the time budget allows."""

MOVE_CHUNK = 25
"""UIDs per move step. Small on purpose: on iCloud's UIDPLUS path a chunk of
250 took over a minute, longer than a client waits."""

CHUNK_SOCKET_TIMEOUT_SECONDS = 15.0
"""Socket read timeout while moving. The connector's default is 30 s per
read, which lets one stalled chunk outlast a client on its own."""

TIME_BUDGET_SECONDS = 20.0
"""A commit call starts no new chunk after this long. Sized well under the
roughly 60 s after which a client was seen to restart the server."""

FETCH_CHUNK = 100
"""UIDs per ENVELOPE fetch while building the preview summary."""

PREVIEW_TTL_SECONDS = 900.0
DEFAULT_CEILING = 5000
SENDER_LIMIT = 50


class QueryMoveError(MailError):
    """Base for query-move failures."""


class QueryMoveUnsupportedError(QueryMoveError):
    """The account's server needs handling this tool does not implement."""


class PreviewNotFoundError(QueryMoveError):
    """The preview id is unknown, already used up, or expired."""


class PreviewTooLargeError(QueryMoveError):
    """The previewed set is larger than the caller's ``max_count``."""


class SelectionStaleError(QueryMoveError):
    """The mailbox was rebuilt since the preview, so its UIDs mean nothing."""


class CommitInProgressError(QueryMoveError):
    """Another commit call is already working on this preview."""


class JournalUnreadableError(QueryMoveError):
    """The interrupted-move journal exists but cannot be read.

    Refusing is the safe direction: the journal is what stops a half-moved
    chunk from being copied again.
    """


class MoveIdentity(NamedTuple):
    host: str
    login: str
    port: int
    account_id: str


class _AccountConnector(ImapConnector):
    _account_id: str


class _Connector(Protocol):
    _host: str
    _email: str
    _port: int
    _account_id: str

    def _session(self) -> Any: ...

    @staticmethod
    def _has_capability(client: Any, name: bytes) -> bool: ...


@dataclass(frozen=True)
class Selector:
    """Which messages to act on. Filters are ANDed.

    Exclusions are applied by the preview itself, against the senders it
    reads back, not sent to the server as ``NOT FROM``: iCloud takes longer
    than the socket timeout to answer a negated sender search, even over a
    few hundred messages.
    """

    sender_contains: str | None = None
    sender_not_contains: tuple[str, ...] = ()
    subject_contains: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    read_status: bool | None = None
    is_flagged: bool | None = None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["sender_not_contains"] = list(self.sender_not_contains)
        return {k: v for k, v in data.items() if v not in (None, [])}


def _require_text(value: str | None, name: str) -> None:
    """A filter that was supplied must say something.

    ``_build_search_criteria`` skips falsy values, so ``sender_contains=""``
    would otherwise drop out and silently widen the selection.
    """
    if value is not None and not value.strip():
        raise ValueError(f"{name} must not be blank; omit it instead")


def build_criteria(selector: Selector) -> list[Any]:
    """Translate a selector to IMAP SEARCH criteria, refusing an open one."""
    _require_text(selector.sender_contains, "sender_contains")
    _require_text(selector.subject_contains, "subject_contains")
    for term in selector.sender_not_contains:
        _require_text(term, "sender_not_contains")
        _reject_control_chars(term, "sender_not_contains")

    criteria = _build_search_criteria(
        selector.sender_contains,
        selector.subject_contains,
        selector.read_status,
        selector.is_flagged,
        date_from=selector.date_from,
        date_to=selector.date_to,
    )
    if criteria == ["ALL"]:
        raise ValueError(
            "the selector needs at least one of sender_contains, "
            "subject_contains, date_from, date_to, read_status or is_flagged; "
            "exclusions alone would select nearly the whole mailbox"
        )
    return criteria


def _is_excluded(sender: str, terms: tuple[str, ...]) -> bool:
    """Whether a sender (display name and address) contains an exclusion."""
    folded = sender.casefold()
    return any(term.strip().casefold() in folded for term in terms)


@dataclass
class PreviewEntry:
    account: str
    source_mailbox: str
    destination_mailbox: str
    uidvalidity: int | None
    total: int
    selector: dict[str, Any]
    expires_at: float
    identity: MoveIdentity
    remaining: list[int] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)


class PreviewStore:
    """Previewed UID sets, held in memory until used or expired.

    In memory on purpose: a server restart drops every pending preview, which
    is the safe direction. Nothing here is worth persisting.
    """

    def __init__(
        self,
        ttl: float = PREVIEW_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl
        self._clock = clock
        self._entries: dict[str, PreviewEntry] = {}
        self._lock = threading.Lock()

    @property
    def ttl(self) -> float:
        return self._ttl

    def _prune(self) -> None:
        now = self._clock()
        for key in [k for k, e in self._entries.items() if e.expires_at <= now]:
            del self._entries[key]

    def put(
        self,
        *,
        account: str,
        source_mailbox: str,
        destination_mailbox: str,
        uidvalidity: int | None,
        uids: list[int],
        selector: dict[str, Any],
        identity: MoveIdentity,
    ) -> str:
        preview_id = secrets.token_urlsafe(16)
        entry = PreviewEntry(
            account=account,
            source_mailbox=source_mailbox,
            destination_mailbox=destination_mailbox,
            uidvalidity=uidvalidity,
            total=len(uids),
            selector=selector,
            expires_at=self._clock() + self._ttl,
            remaining=list(uids),
            identity=identity,
        )
        with self._lock:
            self._prune()
            self._entries[preview_id] = entry
        return preview_id

    def get(self, preview_id: str) -> PreviewEntry:
        with self._lock:
            self._prune()
            entry = self._entries.get(preview_id)
        if entry is None:
            raise PreviewNotFoundError(
                "preview not found: it is unknown, already completed, or "
                "older than the preview lifetime. Run the preview again."
            )
        return entry

    def discard(self, preview_id: str) -> None:
        with self._lock:
            self._entries.pop(preview_id, None)


def connector_for(mail: Any, account: str) -> _AccountConnector:
    """Build an IMAP connector for ``account`` the way the move fast path does.

    Mirrors ``AppleMailConnector._imap_move_messages``. Raises the Keychain
    and account-lookup errors unchanged; the server reports them, because
    these tools have no AppleScript fallback.
    """
    with mail._account_identity_scope():
        account_id = mail._resolve_account_identity(account).id
        host, port, email = mail._resolve_imap_config(account_id)
        password = mail._get_imap_password_with_fallback(account_id, email)
    # Query UIDs must use this exact endpoint; the optional pool omits port.
    connector = _AccountConnector(host, port, email, password)
    connector._account_id = account_id
    return connector


def _identity(connector: _Connector) -> MoveIdentity:
    return MoveIdentity(connector._host, connector._email, connector._port, connector._account_id)


class MoveJournal:
    """On-disk record of UIDPLUS chunks that are part-way through a move.

    ``stage`` is ``"copying"`` from just before the COPY until it returns
    (the copy may or may not have landed) and ``"copied"`` from then until
    the originals are removed. A record that outlives its call is what lets
    the next call finish safely. With no path it keeps records in memory
    only, which is enough for a failure the process survives.

    Several server processes can share one data directory (each client
    launches its own), so every read-modify-write holds a file lock.
    """

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._memory: list[dict[str, Any]] = []

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        if self._path is None:
            yield
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path.with_suffix(".lock"), "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _read(self) -> list[dict[str, Any]]:
        if self._path is None:
            return list(self._memory)
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise JournalUnreadableError(
                f"the interrupted-move journal at {self._path} is not valid "
                f"JSON; nothing was moved. Inspect or remove it by hand."
            ) from exc
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise JournalUnreadableError(
                f"the interrupted-move journal at {self._path} has an "
                f"unexpected shape; nothing was moved."
            )
        return data

    def _write(self, records: list[dict[str, Any]]) -> None:
        if self._path is None:
            self._memory = list(records)
            return
        tmp = self._path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(records, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._path)

    def load(self) -> list[dict[str, Any]]:
        with self._locked():
            return self._read()

    def add(self, record: dict[str, Any]) -> None:
        with self._locked():
            self._write([*self._read(), record])

    def replace(self, old: dict[str, Any], new: dict[str, Any] | None) -> None:
        with self._locked():
            records = [r for r in self._read() if r != old]
            if new is not None:
                records.append(new)
            self._write(records)


def _mailbox_key(name: str) -> str:
    """INBOX is case-insensitive in IMAP; every other name is exact."""
    return "INBOX" if name.upper() == "INBOX" else name


def _records_for(
    journal: MoveJournal, identity: MoveIdentity, mailbox: str,
) -> list[dict[str, Any]]:
    """Validate all matching records before allowing any recovery writes."""
    records = []
    for row in journal.load():
        if (row.get("host"), row.get("login"), _mailbox_key(str(row.get("source_mailbox", "")))) != (
            identity.host, identity.login, _mailbox_key(mailbox)
        ):
            continue
        port = row.get("port")
        if type(port) is int and port != identity.port:
            continue
        if type(port) is not int or row.get("account_id") != identity.account_id:
            raise JournalUnreadableError(
                "An interrupted move for this mailbox cannot be bound to this account and port. "
                "Inspect the source, destination and journal before moving messages."
            )
        records.append(row)
    return records


_COPYUID_RE = re.compile(rb"^(?:COPYUID )?(\d+) (\S+) ([0-9:,]+)$")


def _expand_uid_set(text: bytes) -> list[int]:
    uids: list[int] = []
    for part in text.split(b","):
        low, _, high = part.partition(b":")
        first, last = int(low), int(high or low)
        uids.extend(range(min(first, last), max(first, last) + 1))
    return uids


def _parse_copyuid(value: Any, expected: int) -> tuple[int, list[int]] | None:
    """Destination UIDVALIDITY and UIDs from a COPYUID response code.

    None unless it names exactly ``expected`` destination UIDs: a later
    resume removes originals on the strength of this, so anything short of
    a full account of the copies is treated as "the server did not say".
    """
    if not isinstance(value, bytes):
        return None
    match = _COPYUID_RE.match(value.strip())
    if match is None:
        return None
    dest = _expand_uid_set(match.group(3))
    if len(dest) != expected:
        return None
    return int(match.group(1)), dest


def _copy_reporting_uids(
    client: Any, uids: list[int], destination: str
) -> tuple[int, list[int]] | None:
    """COPY, and return where the copies landed if the server said.

    imapclient's ``copy`` returns untagged FETCH data, not the tagged reply,
    so the COPYUID code (RFC 4315) has to be read from where imaplib files
    response codes.
    """
    responses = getattr(getattr(client, "_imap", None), "untagged_responses", None)
    if isinstance(responses, dict):
        responses.pop("COPYUID", None)
    client.copy(uids, destination)
    if not isinstance(responses, dict):
        return None
    values = responses.pop("COPYUID", None) or [None]
    return _parse_copyuid(values[-1], len(uids))


def _chunks(items: list[int], size: int) -> list[list[int]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _uidvalidity(select_response: Any) -> int | None:
    if not isinstance(select_response, dict):
        return None
    value = select_response.get(b"UIDVALIDITY")
    return int(value) if value is not None else None


def _check_server(connector: _Connector, client: Any) -> bool:
    """Refuse servers this tool cannot move on safely.

    Returns True when the server has MOVE, False when the UIDPLUS sequence
    must be used instead.
    """
    if connector._has_capability(client, b"X-GM-EXT-1"):
        raise QueryMoveUnsupportedError(
            "query moves are not supported on Gmail accounts: Gmail folders "
            "are labels and need different handling"
        )
    if connector._has_capability(client, b"MOVE"):
        return True
    if connector._has_capability(client, b"UIDPLUS"):
        return False
    # Without UIDPLUS the only expunge is unscoped, which would remove every
    # message flagged deleted in the mailbox, not just the ones moved here.
    raise MailImapMoveUnsupportedError(
        "the IMAP server advertises neither MOVE (RFC 6851) nor UIDPLUS "
        "(RFC 4315); query moves cannot be done safely"
    )


def _header_date(envelope: Any) -> date | None:
    value = getattr(envelope, "date", None)
    return value.date() if isinstance(value, datetime) else None


def _read_senders(
    client: Any, uids: list[int]
) -> dict[int, tuple[str, date | None]]:
    """Sender and header date of each message, by UID."""
    found: dict[int, tuple[str, date | None]] = {}
    for chunk in _chunks(uids, FETCH_CHUNK):
        for uid, data in client.fetch(chunk, ["ENVELOPE"]).items():
            envelope = data.get(b"ENVELOPE")
            if envelope is not None:
                found[int(uid)] = (_format_sender(envelope), _header_date(envelope))
    return found


def _apply_exclusions(
    uids: list[int],
    read: dict[int, tuple[str, date | None]],
    terms: tuple[str, ...],
) -> tuple[list[int], int, int]:
    """Drop excluded senders. Returns (kept, excluded, unreadable).

    A message whose sender could not be read is left out as well: it cannot
    be shown not to be one the caller asked to keep.
    """
    if not terms:
        return uids, 0, 0
    kept: list[int] = []
    excluded = unreadable = 0
    for uid in uids:
        sender = read.get(uid, ("", None))[0]
        if not sender:
            unreadable += 1
        elif _is_excluded(sender, terms):
            excluded += 1
        else:
            kept.append(uid)
    return kept, excluded, unreadable


def _summarize(
    read: dict[int, tuple[str, date | None]], uids: list[int], selector: Selector
) -> dict[str, Any]:
    rows = [{"sender": read[u][0]} for u in uids if u in read]
    dates = [d for u in uids if u in read and (d := read[u][1]) is not None]

    lower = date.fromisoformat(selector.date_from) if selector.date_from else None
    upper = date.fromisoformat(selector.date_to) if selector.date_to else None
    outside = sum(
        1 for d in dates
        if (lower is not None and d < lower) or (upper is not None and d > upper)
    )
    ranked = rank_senders(rows, "address", max(len(rows), 1))
    shown = ranked[:SENDER_LIMIT]
    return {
        "oldest_date": min(dates).isoformat() if dates else None,
        "newest_date": max(dates).isoformat() if dates else None,
        "header_dates_outside_window": outside,
        "distinct_senders": len(ranked),
        "senders": shown,
        "other_senders_count": sum(r["count"] for r in ranked[SENDER_LIMIT:]),
    }


def preview(
    connector: _Connector,
    store: PreviewStore,
    *,
    account: str,
    source_mailbox: str,
    destination_mailbox: str,
    selector: Selector,
    ceiling: int = DEFAULT_CEILING,
    journal_path: Path | None = None,
) -> dict[str, Any]:
    """Find what the selector matches and hold that set. Changes nothing."""
    _reject_control_chars(source_mailbox, "source_mailbox")
    _reject_control_chars(destination_mailbox, "destination_mailbox")
    if source_mailbox.strip().casefold() == destination_mailbox.strip().casefold():
        raise ValueError("source_mailbox and destination_mailbox must differ")
    criteria = build_criteria(selector)
    charset = _search_charset(criteria)

    with connector._session() as client:
        _check_server(connector, client)
        if not client.folder_exists(destination_mailbox):
            raise MailMailboxNotFoundError(
                f"destination mailbox not found: {destination_mailbox}"
            )
        selected = client.select_folder(source_mailbox, readonly=True)
        uidvalidity = _uidvalidity(selected)
        if uidvalidity is None:
            # Without it a rebuilt mailbox could not be told from the one
            # previewed, and the stored UIDs would point at other messages.
            raise QueryMoveUnsupportedError(
                "the server did not report UIDVALIDITY for the source "
                "mailbox; a preview cannot be bound to it safely"
            )
        # Messages already flagged \\Deleted are left out: that flag is what an
        # interrupted UIDPLUS move leaves behind after its copy has landed,
        # so selecting them again would copy them a second time.
        live = [*criteria, "UNDELETED"]
        flagged = [*criteria, "DELETED"]
        found = client.search(live, charset) if charset else client.search(live)
        skipped = client.search(flagged, charset) if charset else client.search(flagged)
        uids = sorted(int(u) for u in found)

        result: dict[str, Any] = {
            "matched": len(uids),
            "preview_id": None,
            "flagged_deleted_skipped": len(skipped),
        }
        # An earlier call may have left chunks part-way through. Say so, and
        # still issue an id when nothing else matches: the commit is what
        # settles them, and flagged originals no longer match the search.
        pending = sum(
            len(r.get("uids", []))
            for r in _records_for(MoveJournal(journal_path), _identity(connector), source_mailbox)
            if r.get("uidvalidity") == uidvalidity
        )
        if pending:
            result["interrupted_pending"] = pending
        if not uids and not pending:
            return result
        if len(uids) > ceiling:
            # Counted before exclusions: reading every sender of a set this
            # size is what the ceiling is there to avoid.
            result.update(over_limit=True, limit=ceiling)
            return result
        if uids:
            read = _read_senders(client, uids)
            uids, excluded, unreadable = _apply_exclusions(
                uids, read, selector.sender_not_contains
            )
            result["matched"] = len(uids)
            if selector.sender_not_contains:
                result["excluded_by_sender"] = excluded
                result["sender_unreadable_left_out"] = unreadable
            if not uids and not pending:
                return result
            result.update(_summarize(read, uids, selector))

    result["preview_id"] = store.put(
        account=account,
        source_mailbox=source_mailbox,
        destination_mailbox=destination_mailbox,
        uidvalidity=uidvalidity,
        uids=uids,
        selector=selector.as_dict(),
        identity=_identity(connector),
    )
    result["expires_in_seconds"] = int(store.ttl)
    result["max_per_commit_call"] = MAX_PER_CALL
    result["commit_time_budget_seconds"] = int(TIME_BUDGET_SECONDS)
    return result


def _remove_copied(client: Any, uids: list[int]) -> None:
    """Remove originals whose copy is known to exist."""
    client.add_flags(uids, [b"\\Deleted"], silent=True)
    client.uid_expunge(uids)


def _move_chunk(
    client: Any,
    uids: list[int],
    entry: PreviewEntry,
    has_move: bool,
    journal: MoveJournal,
) -> None:
    """Move ``uids`` out of the selected mailbox into the entry's destination.

    The UIDPLUS path copies first and removes last, so a failure part-way
    leaves the originals in place. Each step is journalled first, so a
    failure (or the process being killed) is finished or held back by the
    next call rather than copied again.
    """
    if has_move:
        client.move(uids, entry.destination_mailbox)
        return
    record: dict[str, Any] = {
        **entry.identity._asdict(),
        "source_mailbox": entry.source_mailbox,
        "destination_mailbox": entry.destination_mailbox,
        "uidvalidity": entry.uidvalidity,
        "uids": list(uids),
        "stage": "copying",
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    journal.add(record)
    try:
        parsed = _copy_reporting_uids(client, uids, entry.destination_mailbox)
    except IMAPClientAbortError:
        raise  # connection broke: the copy may or may not have landed
    except IMAPClientError:
        # A tagged NO or BAD: the server refused, so nothing was copied
        # (COPY is all-or-nothing). No hold is needed.
        journal.replace(record, None)
        raise
    copied = {**record, "stage": "copied"}
    if parsed is not None:
        copied["dest_uidvalidity"], copied["dest_uids"] = parsed
    else:
        logger.warning(
            "COPY reply carried no COPYUID; an interrupted removal of this "
            "chunk could not be resumed and would be held back"
        )
    journal.replace(record, copied)
    _remove_copied(client, uids)
    journal.replace(copied, None)


def _copies_exist(client: Any, record: dict[str, Any], source_mailbox: str) -> bool:
    """Whether every copy a "copied" record made is still in its destination.

    The originals are only removed on this proof. Time may have passed since
    the copy: the copies could have been deleted as apparent duplicates, and
    removing the originals then would lose the mail. A copy that is itself
    flagged deleted does not count. Leaves the source mailbox selected for
    writing again.
    """
    dest_uids = [int(u) for u in record.get("dest_uids") or []]
    if not dest_uids or record.get("dest_uidvalidity") is None:
        return False
    try:
        try:
            selected = client.select_folder(
                record["destination_mailbox"], readonly=True
            )
        except IMAPClientAbortError:
            raise
        except IMAPClientError:
            return False  # destination renamed or removed: cannot prove it
        if _uidvalidity(selected) != record["dest_uidvalidity"]:
            return False
        found = client.search(
            ["UID", ",".join(str(u) for u in dest_uids), "UNDELETED"]
        )
        return {int(u) for u in found} >= set(dest_uids)
    finally:
        restored = client.select_folder(source_mailbox, readonly=False)
        if _uidvalidity(restored) != record["uidvalidity"]:
            raise SelectionStaleError(
                "The source mailbox changed during copy verification. Preview again."
            )


def _settle_interrupted(
    client: Any, journal: MoveJournal, entry: PreviewEntry
) -> tuple[list[int], set[int]]:
    """Deal with chunks an earlier call left part-way through.

    Returns the originals removed now (their copies were verified in the
    destination) and the UIDs that must be held back, either because the
    copy is in doubt or because its copies can no longer be found.
    """
    resumed: list[int] = []
    held: set[int] = set()
    for record in _records_for(journal, entry.identity, entry.source_mailbox):
        if record.get("uidvalidity") != entry.uidvalidity:
            # The mailbox was rebuilt; these UIDs name other messages now.
            journal.replace(record, None)
            continue
        present = _present(client, [int(u) for u in record.get("uids", [])])
        if not present:
            journal.replace(record, None)
        elif record.get("stage") == "copied" and _copies_exist(
            client, record, entry.source_mailbox
        ):
            _remove_copied(client, present)
            journal.replace(record, None)
            resumed.extend(present)
        else:
            held.update(present)
    return resumed, held


def _present(client: Any, uids: list[int]) -> list[int]:
    """Which of ``uids`` still exist in the selected mailbox."""
    # UID SEARCH, not FETCH: imapclient keys an unsolicited FETCH reply that
    # carries no UID by sequence number, which could pass for a wanted UID.
    wanted = set(uids)
    found = client.search(["UID", ",".join(str(u) for u in uids)])
    return sorted(int(u) for u in found if int(u) in wanted)


def _recount(
    connector: _Connector, entry: PreviewEntry, candidates: list[int]
) -> list[int] | None:
    """After a failure, ask a fresh connection what is still in the source.

    Returns None when that cannot be established either; the caller then
    keeps every candidate, which is safe because each chunk is re-checked
    for presence before any later move.
    """
    try:
        with connector._session() as client:
            selected = client.select_folder(entry.source_mailbox, readonly=True)
            if _uidvalidity(selected) != entry.uidvalidity:
                return None
            still: list[int] = []
            for chunk in _chunks(candidates, MOVE_CHUNK):
                still.extend(_present(client, chunk))
            return still
    except Exception:  # noqa: BLE001 - any failure here means "unknown"
        return None


def _write_receipt(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


@dataclass
class _Progress:
    """What one commit call got through before it stopped."""

    moved: list[int] = field(default_factory=list)
    held_back: list[int] = field(default_factory=list)
    pending: list[int] = field(default_factory=list)  # chunk in flight on error
    resumed: list[int] = field(default_factory=list)
    gone: int = 0
    done: int = 0  # UIDs of entry.remaining dealt with
    error: str | None = None


@contextlib.contextmanager
def _socket_timeout(client: Any) -> Iterator[None]:
    """Shorten the socket read timeout for a move, then put it back."""
    try:
        sock = client.socket()
        previous = sock.gettimeout()
        sock.settimeout(CHUNK_SOCKET_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - a client without a tunable socket
        yield
        return
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            sock.settimeout(previous)


def _run_chunks(
    connector: _Connector,
    store: PreviewStore,
    preview_id: str,
    entry: PreviewEntry,
    journal: MoveJournal,
    time_budget: float,
    clock: Callable[[], float],
) -> _Progress:
    """Work through the entry's chunks until done, out of time, or failing."""
    progress = _Progress()
    chunks = _chunks(entry.remaining[:MAX_PER_CALL], MOVE_CHUNK)
    started = clock()
    try:
        with connector._session() as client, _socket_timeout(client):
            has_move = _check_server(connector, client)
            selected = client.select_folder(entry.source_mailbox, readonly=False)
            if _uidvalidity(selected) != entry.uidvalidity:
                store.discard(preview_id)
                raise SelectionStaleError(
                    "the source mailbox was rebuilt since the preview "
                    "(UIDVALIDITY changed); nothing was moved. Preview again."
                )
            progress.resumed, held = _settle_interrupted(client, journal, entry)
            for index, chunk in enumerate(chunks):
                if index and clock() - started >= time_budget:
                    break
                progress.pending = chunk
                present = _present(client, chunk)
                skip = [u for u in present if u in held]
                present = [u for u in present if u not in held]
                if present:
                    _move_chunk(client, present, entry, has_move, journal)
                progress.moved.extend(present)
                progress.held_back.extend(skip)
                progress.gone += len(chunk) - len(present) - len(skip)
                progress.done += len(chunk)
                progress.pending = []
    except MailError:
        # Refusals decided before or between moves (stale mailbox, no MOVE
        # support). Typed, so the server reports them as such.
        raise
    except Exception as exc:  # noqa: BLE001 - reported to the caller
        progress.error = str(exc) or type(exc).__name__
    return progress


def commit(
    connector: _Connector,
    store: PreviewStore,
    *,
    preview_id: str,
    max_count: int,
    receipts_path: Path | None = None,
    journal_path: Path | None = None,
    time_budget: float = TIME_BUDGET_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Move the next slice of a previewed set. Repeat until ``complete``."""
    entry = store.get(preview_id)
    # One commit per preview at a time. A client that retries while an
    # earlier call is still running would otherwise interleave with it:
    # two copies of a chunk, or UIDs sliced off the list unprocessed.
    if not entry.lock.acquire(blocking=False):
        raise CommitInProgressError(
            "another commit call is still working on this preview; wait "
            "for it to finish, then preview again to see what is left"
        )
    try:
        return _commit_locked(
            connector, store, entry, preview_id, max_count,
            receipts_path, MoveJournal(journal_path), time_budget, clock,
        )
    finally:
        entry.lock.release()


def _commit_locked(
    connector: _Connector,
    store: PreviewStore,
    entry: PreviewEntry,
    preview_id: str,
    max_count: int,
    receipts_path: Path | None,
    journal: MoveJournal,
    time_budget: float,
    clock: Callable[[], float],
) -> dict[str, Any]:
    if entry.total > max_count:
        raise PreviewTooLargeError(
            f"the preview holds {entry.total} messages, above max_count="
            f"{max_count}; nothing was moved"
        )
    if _identity(connector) != entry.identity:
        store.discard(preview_id)
        raise SelectionStaleError(
            "the account UUID, host, port or login differs from "
            "the one previewed; nothing was moved. Preview again."
        )

    progress = _run_chunks(
        connector, store, preview_id, entry, journal, time_budget, clock,
    )
    pending = progress.pending
    untouched = entry.remaining[progress.done + len(pending):]
    unconfirmed: list[int] = []
    result: dict[str, Any] = {
        "moved": len(progress.moved),
        "already_gone": progress.gone,
        "total": entry.total,
    }
    if progress.resumed:
        result["resumed"] = len(progress.resumed)
    if progress.held_back:
        result["held_back_uncertain"] = len(progress.held_back)
    if progress.error is None:
        entry.remaining = untouched
        result["remaining"] = len(untouched)
        result["outcome"] = "more" if untouched else "complete"
    else:
        # The failed chunk may or may not have been applied by the server.
        still = _recount(connector, entry, pending)
        result["outcome"] = "failed"
        result["error"] = progress.error
        if still is None:
            entry.remaining = pending + untouched
            result["remaining"] = None
            result["remaining_unverified"] = True
        else:
            # Gone from the source without a confirmed reply: the server
            # most likely applied the chunk whose reply was lost.
            unconfirmed = sorted(set(pending) - set(still))
            entry.remaining = still + untouched
            result["remaining"] = len(entry.remaining)
            result["moved_unconfirmed"] = len(unconfirmed)

    if result["outcome"] == "complete":
        store.discard(preview_id)

    if receipts_path is not None and (
        progress.moved or progress.resumed or progress.error is not None
    ):
        try:
            _write_receipt(
                receipts_path,
                {
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "preview_id": preview_id,
                    "account": entry.account,
                    "source_mailbox": entry.source_mailbox,
                    "destination_mailbox": entry.destination_mailbox,
                    "selector": entry.selector,
                    "uidvalidity": entry.uidvalidity,
                    "source_uids": progress.moved,
                    "source_uids_unconfirmed": unconfirmed,
                    "source_uids_resumed": progress.resumed,
                    "moved": len(progress.moved),
                    "outcome": result["outcome"],
                },
            )
        except OSError as exc:
            result["receipt_error"] = str(exc)
    return result
