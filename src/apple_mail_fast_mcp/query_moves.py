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

IMAP only, and only where the server advertises MOVE. There is no AppleScript
fallback: if IMAP cannot do it, the call fails.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Protocol

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

MAX_PER_CALL = 1000
"""Most messages one commit call moves. Bounds the call's duration so a
client or proxy timeout cannot fire while the server is still moving mail."""

MOVE_CHUNK = 250
"""UIDs per IMAP MOVE command."""

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


class _Connector(Protocol):
    def _session(self) -> Any: ...

    @staticmethod
    def _has_capability(client: Any, name: bytes) -> bool: ...


@dataclass(frozen=True)
class Selector:
    """Which messages to act on. Filters are ANDed; exclusions are NOT FROM."""

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
    for term in selector.sender_not_contains:
        criteria.extend(["NOT", "FROM", term])
    return criteria


@dataclass
class PreviewEntry:
    account: str
    source_mailbox: str
    destination_mailbox: str
    uidvalidity: int | None
    total: int
    selector: dict[str, Any]
    expires_at: float
    remaining: list[int] = field(default_factory=list)


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


def connector_for(mail: Any, account: str) -> ImapConnector:
    """Build an IMAP connector for ``account`` the way the move fast path does.

    Mirrors ``AppleMailConnector._imap_move_messages``. Raises the Keychain
    and account-lookup errors unchanged; the server reports them, because
    these tools have no AppleScript fallback.
    """
    host, port, email = mail._resolve_imap_config(account)
    password = mail._get_imap_password_with_fallback(account, email)
    return ImapConnector(host, port, email, password, pool=mail._imap_pool)


def _chunks(items: list[int], size: int) -> list[list[int]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _uidvalidity(select_response: Any) -> int | None:
    if not isinstance(select_response, dict):
        return None
    value = select_response.get(b"UIDVALIDITY")
    return int(value) if value is not None else None


def _check_server(connector: _Connector, client: Any) -> None:
    if connector._has_capability(client, b"X-GM-EXT-1"):
        raise QueryMoveUnsupportedError(
            "query moves are not supported on Gmail accounts: Gmail folders "
            "are labels and need different handling"
        )
    if not connector._has_capability(client, b"MOVE"):
        raise MailImapMoveUnsupportedError(
            "the IMAP server does not advertise MOVE (RFC 6851); query moves "
            "require it"
        )


def _header_date(envelope: Any) -> date | None:
    value = getattr(envelope, "date", None)
    return value.date() if isinstance(value, datetime) else None


def _summarize(
    client: Any, uids: list[int], selector: Selector
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    dates: list[date] = []
    for chunk in _chunks(uids, FETCH_CHUNK):
        for data in client.fetch(chunk, ["ENVELOPE"]).values():
            envelope = data.get(b"ENVELOPE")
            if envelope is None:
                continue
            rows.append({"sender": _format_sender(envelope)})
            header = _header_date(envelope)
            if header is not None:
                dates.append(header)

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
        found = client.search(criteria, charset) if charset else client.search(criteria)
        uids = sorted(int(u) for u in found)

        result: dict[str, Any] = {"matched": len(uids), "preview_id": None}
        if not uids:
            return result
        if len(uids) > ceiling:
            result.update(over_limit=True, limit=ceiling)
            return result
        result.update(_summarize(client, uids, selector))

    result["preview_id"] = store.put(
        account=account,
        source_mailbox=source_mailbox,
        destination_mailbox=destination_mailbox,
        uidvalidity=uidvalidity,
        uids=uids,
        selector=selector.as_dict(),
    )
    result["expires_in_seconds"] = int(store.ttl)
    result["max_per_commit_call"] = MAX_PER_CALL
    return result


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
            client.select_folder(entry.source_mailbox, readonly=True)
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


def commit(
    connector: _Connector,
    store: PreviewStore,
    *,
    preview_id: str,
    max_count: int,
    receipts_path: Path | None = None,
) -> dict[str, Any]:
    """Move the next slice of a previewed set. Repeat until ``complete``."""
    entry = store.get(preview_id)
    if entry.total > max_count:
        raise PreviewTooLargeError(
            f"the preview holds {entry.total} messages, above max_count="
            f"{max_count}; nothing was moved"
        )

    batch = entry.remaining[:MAX_PER_CALL]
    untouched = entry.remaining[MAX_PER_CALL:]
    moved: list[int] = []
    unconfirmed: list[int] = []
    gone = 0
    error: str | None = None
    pending = list(batch)

    try:
        with connector._session() as client:
            _check_server(connector, client)
            selected = client.select_folder(entry.source_mailbox, readonly=False)
            if _uidvalidity(selected) != entry.uidvalidity:
                store.discard(preview_id)
                raise SelectionStaleError(
                    "the source mailbox was rebuilt since the preview "
                    "(UIDVALIDITY changed); nothing was moved. Preview again."
                )
            for chunk in _chunks(batch, MOVE_CHUNK):
                present = _present(client, chunk)
                if present:
                    client.move(present, entry.destination_mailbox)
                moved.extend(present)
                gone += len(chunk) - len(present)
                pending = pending[len(chunk):]
    except MailError:
        # Refusals decided before or between moves (stale mailbox, no MOVE
        # support). Typed, so the server reports them as such.
        raise
    except Exception as exc:  # noqa: BLE001 - reported to the caller below
        error = str(exc) or type(exc).__name__

    result: dict[str, Any] = {
        "moved": len(moved),
        "already_gone": gone,
        "total": entry.total,
    }
    if error is None:
        entry.remaining = untouched
        result["remaining"] = len(untouched)
        result["outcome"] = "more" if untouched else "complete"
    else:
        # The failed chunk may or may not have been applied by the server.
        still = _recount(connector, entry, pending)
        result["outcome"] = "failed"
        result["error"] = error
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

    if receipts_path is not None and (moved or error is not None):
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
                    "source_uids": moved,
                    "source_uids_unconfirmed": unconfirmed,
                    "moved": len(moved),
                    "outcome": result["outcome"],
                },
            )
        except OSError as exc:
            result["receipt_error"] = str(exc)
    return result
