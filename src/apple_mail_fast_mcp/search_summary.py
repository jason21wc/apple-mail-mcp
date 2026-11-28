"""Counts of what a mailbox holds, grouped, without returning the messages.

Fork-local. Checking what a large mailbox holds through ``search_messages``
returns a row per message, and its AppleScript fallback can leave Mail.app
unresponsive on a mailbox of tens of thousands. This reads only envelopes,
flags and received dates over IMAP, in bounded chunks under a time budget,
and returns counts grouped by sender address, sender domain, subject pattern
or day.

IMAP only, on purpose: there is no AppleScript fallback. Read-only: the
mailbox is opened read-only and nothing here changes a message.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol

from imapclient.exceptions import IMAPClientAbortError

from .imap_connector import (
    _build_search_criteria,
    _decode_mime_header,
    _format_sender,
    _iso_to_imap_date,
    _reject_control_chars,
    _search_charset,
)
from .query_moves import _socket_timeout
from .utils import address_domain, extract_address

GROUPINGS = ("address", "domain", "subject", "day")

FETCH_CHUNK = 100
"""UIDs per fetch, the size the query-move preview already uses."""

TIME_BUDGET_SECONDS = 20.0
"""No further fetch is started after this long. Sized well under the roughly
60 s after which a client was seen to give up on a call."""

MAX_SCAN = 20000
"""Most messages one call will read, whatever the caller asks for."""

SUBJECT_KEY_LIMIT = 120
"""Longest subject pattern returned as a group key."""

NO_SENDER = "(no sender)"
"""Group key for a message whose sender gives no address."""

_FLAG_SEEN = b"\\Seen"
_FLAG_FLAGGED = b"\\Flagged"

_REPLY_PREFIX = re.compile(r"^(?:\s*(?:re|fw|fwd)\s*:\s*)+", re.IGNORECASE)
# A day-month-year run such as 04May26 or 4-May-2026.
_DATE_TOKEN = re.compile(r"\b\d{1,2}[-\s]?[A-Za-z]{3}[-\s]?\d{2,4}\b")
_DIGITS = re.compile(r"\d+")
# Marks left side by side, or joined by the separators numbers come with.
_MARK_RUN = re.compile(r"#(?:[-/.:,#]*#)+")
_SPACES = re.compile(r"\s+")


class _Connector(Protocol):
    def _session(self) -> Any: ...


def normalize_subject(subject: str) -> str:
    """Reduce a subject to its pattern, so recurring mail groups together.

    Reply and forward prefixes are dropped and every number or date becomes
    ``#``: a report sent daily with the date in its subject is one group, not
    one group per day.
    """
    text = _REPLY_PREFIX.sub("", subject or "")
    text = _DATE_TOKEN.sub("#", text)
    text = _DIGITS.sub("#", text)
    text = _MARK_RUN.sub("#", text)
    text = _SPACES.sub(" ", text).strip()
    if not text:
        return "(no subject)"
    return text[:SUBJECT_KEY_LIMIT]


def parse_day(value: str | None, name: str) -> date | None:
    """A YYYY-MM-DD filter as a date, or a ValueError naming the field."""
    if value is None:
        return None
    _iso_to_imap_date(value, name)  # the same check the search itself applies
    return date.fromisoformat(value)


def _require_text(value: str | None, name: str) -> None:
    """A filter that was supplied must say something (never silently widen)."""
    if value is not None and not value.strip():
        raise ValueError(f"{name} must not be blank; omit it instead")


def _group_key(by: str, envelope: Any, received: Any) -> str:
    if by == "subject":
        return normalize_subject(_decode_mime_header(getattr(envelope, "subject", None)))
    if by == "day":
        when = received if isinstance(received, datetime) else getattr(envelope, "date", None)
        return when.date().isoformat() if isinstance(when, datetime) else "(no date)"
    address = extract_address(_format_sender(envelope)) if envelope is not None else ""
    if by == "domain":
        return address_domain(address) or NO_SENDER
    return address or NO_SENDER


def _ranked(counts: Counter[str], by: str) -> list[tuple[str, int]]:
    if by == "day":
        return sorted(counts.items(), key=lambda kv: kv[0], reverse=True)
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


class _Interrupted(Exception):
    """The connection broke part-way through; what was counted is kept."""


@dataclass
class _Tally:
    """Running counts for one summary."""

    uids: list[int] = field(default_factory=list)  # every match, newest first
    counts: Counter[str] = field(default_factory=Counter)
    senders: Counter[str] = field(default_factory=Counter)
    attempted: int = 0  # UIDs asked for, whether or not the server answered
    scanned: int = 0  # messages counted
    too_old: int = 0  # read, but received before ``received_since``
    unread: int = 0
    flagged: int = 0
    interrupted: bool = False

    def add(self, by: str, data: Any, received_since: datetime | None) -> None:
        # No ENVELOPE means this is not the reply that was asked for: a
        # message gone since the search, or an unsolicited flags update that
        # imapclient files under a sequence number.
        envelope = data.get(b"ENVELOPE") if data else None
        if envelope is None:
            return
        received = data.get(b"INTERNALDATE")
        if (
            received_since is not None
            and isinstance(received, datetime)
            and received < received_since
        ):
            self.too_old += 1
            return
        self.scanned += 1
        flags = data.get(b"FLAGS") or ()
        self.unread += _FLAG_SEEN not in flags
        self.flagged += _FLAG_FLAGGED in flags
        self.counts[_group_key(by, envelope, received)] += 1
        address = extract_address(_format_sender(envelope))
        if address:
            self.senders[address] += 1


def summarize(
    connector: _Connector,
    *,
    mailbox: str,
    by: str = "address",
    sender_contains: str | None = None,
    subject_contains: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    received_since: datetime | None = None,
    scan_limit: int = MAX_SCAN,
    group_limit: int = 50,
    budget: float = TIME_BUDGET_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Count matching messages by group. Reads the newest first.

    ``total`` is how many messages matched; ``scanned`` is how many were
    read. They differ (``window_fully_covered`` false) when the match is
    above ``scan_limit`` or the time budget ran out: the groups then
    describe the newest ``scanned`` messages only.

    ``received_since`` leaves out messages received before that local time;
    it is how an hours-based window is applied, since the server's date
    filters work in whole days.
    """
    if by not in GROUPINGS:
        raise ValueError(f"by must be one of {', '.join(GROUPINGS)}; got {by!r}")
    if not 1 <= scan_limit <= MAX_SCAN:
        raise ValueError(f"scan_limit must be between 1 and {MAX_SCAN}")
    if group_limit < 1:
        raise ValueError("the group limit must be at least 1")
    _reject_control_chars(mailbox, "mailbox")
    _require_text(sender_contains, "sender_contains")
    _require_text(subject_contains, "subject_contains")
    criteria = _build_search_criteria(
        sender_contains, subject_contains, None, None,
        date_from=date_from, date_to=date_to,
    )
    charset = _search_charset(criteria)

    tally = _Tally()
    started = clock()
    try:
        with connector._session() as client, _socket_timeout(client):
            client.select_folder(mailbox, readonly=True)
            found = client.search(criteria, charset) if charset else client.search(criteria)
            tally.uids = sorted((int(u) for u in found), reverse=True)
            wanted = tally.uids[:scan_limit]
            for start in range(0, len(wanted), FETCH_CHUNK):
                if clock() - started >= budget:
                    break
                chunk = wanted[start:start + FETCH_CHUNK]
                try:
                    fetched = client.fetch(chunk, ["ENVELOPE", "FLAGS", "INTERNALDATE"])
                except (IMAPClientAbortError, OSError) as exc:
                    if not tally.attempted:
                        raise
                    # Keep what was counted, but leave the session by an
                    # exception so the broken connection is dropped.
                    raise _Interrupted from exc
                tally.attempted += len(chunk)
                for uid in chunk:
                    tally.add(by, fetched.get(uid), received_since)
    except _Interrupted:
        tally.interrupted = True

    counts, senders = tally.counts, tally.senders
    uids, scanned, too_old = tally.uids, tally.scanned, tally.too_old
    unread, flagged = tally.unread, tally.flagged
    ranked = _ranked(counts, by)
    shown, rest = ranked[:group_limit], ranked[group_limit:]
    result: dict[str, Any] = {} if not tally.interrupted else {"interrupted": True}
    return {
        **result,
        # Always by address, whatever the grouping: it costs nothing extra
        # and saves a second pass over the mailbox.
        "top_senders": [
            {"address": address, "count": count}
            for address, count in _ranked(senders, "address")[:group_limit]
        ],
        # With ``received_since`` and a partial scan this is an upper bound:
        # messages not read cannot be checked against the hour.
        "total": len(uids) - too_old,
        "scanned": scanned,
        "window_fully_covered": tally.attempted >= len(uids),
        "unread": unread,
        "flagged": flagged,
        "groups": [{"key": key, "count": count} for key, count in shown],
        "other_groups": len(rest),
        "other_count": sum(count for _, count in rest),
    }
