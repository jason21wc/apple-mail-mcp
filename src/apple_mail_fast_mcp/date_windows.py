"""Date ranges that split a selection too large for a tool's ceiling.

Fork-local. A query tool that refuses a selection above its ceiling used to
leave the caller to split the date range by hand. This works the split out:
it reads each matched message's received day and packs consecutive days into
ranges that are each expected to fit.

The ranges together cover exactly the range the caller asked for. Interior
boundaries are consecutive days, and the first and last range keep the
caller's own bounds (open if the caller left them open), so no message can
fall between two ranges or off either end.

The counts are estimates. Days are bucketed from the local-time value
imapclient returns, while the server compares its own stored date, so a
message near midnight may be counted under a neighbouring day. A range that
turns out too large when it is previewed is simply split again.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from typing import Any

from imapclient.exceptions import IMAPClientAbortError, IMAPClientError

logger = logging.getLogger(__name__)

INTERNALDATE_CHUNK = 1000
"""UIDs per fetch. INTERNALDATE alone is a few dozen bytes per message."""

BUDGET_SECONDS = 15.0
"""No further fetch is started after this long, and no single read may run
past what is left of it."""

UID_LIMIT = 60000
"""Above this many matches the days are not read at all."""

FILL = 0.9
"""How full a range is packed, as a share of the ceiling. The slack absorbs
messages that the server counts under a neighbouring day."""

MIN_DATED_SHARE = 0.9
"""Ranges are offered only when at least this share of the messages gave a
date. With fewer, the counts would understate the ranges too far to trust."""


@contextlib.contextmanager
def _read_timeout(client: Any, seconds: float) -> Iterator[None]:
    """Cap one socket read, so a stalled fetch cannot outlast the budget."""
    try:
        sock = client.socket()
        previous = sock.gettimeout()
        sock.settimeout(max(1.0, seconds))
    except Exception:  # noqa: BLE001 - a client without a tunable socket
        yield
        return
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            sock.settimeout(previous)


def read_day_counts(
    client: Any,
    uids: list[int],
    *,
    budget: float = BUDGET_SECONDS,
    uid_limit: int = UID_LIMIT,
    clock: Callable[[], float] = time.monotonic,
) -> Counter[date] | None:
    """How many of ``uids`` were received on each day, or None if unknown.

    Reads from the mailbox the client has selected. None means the days
    could not be established (too many messages, out of time, or the server
    refused); the caller then offers no ranges rather than partial ones.

    A broken connection (a timeout, an abort) is raised, not swallowed: the
    caller must let it leave the session, so the dead connection is dropped
    instead of being handed to the next call.
    """
    if len(uids) > uid_limit:
        return None
    counts: Counter[date] = Counter()
    started = clock()
    for start in range(0, len(uids), INTERNALDATE_CHUNK):
        left = budget - (clock() - started)
        if left <= 0:
            return None
        chunk = uids[start:start + INTERNALDATE_CHUNK]
        try:
            with _read_timeout(client, left):
                fetched = client.fetch(chunk, ["INTERNALDATE"])
        except IMAPClientAbortError:
            raise
        except IMAPClientError as exc:
            # A tagged NO or BAD: the connection itself is still good.
            logger.warning("server refused the received-date read: %s", exc)
            return None
        for data in fetched.values():
            received = data.get(b"INTERNALDATE")
            if isinstance(received, datetime):
                counts[received.date()] += 1
    return counts


def _clamp(day: date, lower: date | None, upper: date | None) -> date:
    if lower is not None and day < lower:
        return lower
    if upper is not None and day > upper:
        return upper
    return day


def pack_windows(
    day_counts: Counter[date],
    *,
    ceiling: int,
    date_from: str | None,
    date_to: str | None,
    fill: float = FILL,
) -> list[dict[str, Any]]:
    """Pack consecutive days into ranges of at most ``ceiling * fill``.

    A single day above the ceiling becomes a range of its own, marked
    ``over_limit`` and naming that ``day``: dates cannot split it, so it
    needs another filter.

    A day observed outside the caller's bounds (a message near midnight,
    bucketed under the neighbouring local day) is counted under the nearest
    day inside them, so no range ever starts after it ends.
    """
    lower = date.fromisoformat(date_from) if date_from else None
    upper = date.fromisoformat(date_to) if date_to else None
    counts: Counter[date] = Counter()
    for day, count in day_counts.items():
        counts[_clamp(day, lower, upper)] += count
    days = sorted(counts)
    if not days:
        return []
    target = max(1, int(ceiling * fill))

    groups: list[tuple[date, date, int]] = []  # first day, last day, count
    first = last = days[0]
    total = counts[first]
    for day in days[1:]:
        count = counts[day]
        if total + count > target:
            groups.append((first, last, total))
            first, total = day, 0
        last = day
        total += count
    groups.append((first, last, total))

    windows: list[dict[str, Any]] = []
    for position, (first_day, last_day, count) in enumerate(groups):
        # Each range starts the day after the previous one ended, so days
        # with no mail between two groups still belong to a range.
        start = lower if position == 0 else groups[position - 1][1] + timedelta(days=1)
        end = upper if position == len(groups) - 1 else last_day
        window: dict[str, Any] = {
            "date_from": start.isoformat() if start else None,
            "date_to": end.isoformat() if end else None,
            "approx_matched": count,
        }
        if count > ceiling:
            # Only a single day can exceed the ceiling on its own.
            window.update(over_limit=True, day=first_day.isoformat())
        windows.append(window)
    return windows


def suggest(
    client: Any,
    uids: list[int],
    *,
    ceiling: int,
    date_from: str | None,
    date_to: str | None,
) -> dict[str, Any]:
    """Fields to add to an over-the-ceiling answer: ranges, or why not.

    Raises what ``read_day_counts`` raises on a broken connection.
    """
    unavailable: dict[str, Any] = {"windows_unavailable": True}
    counts = read_day_counts(client, uids)
    if not counts or sum(counts.values()) < len(uids) * MIN_DATED_SHARE:
        return unavailable
    windows = pack_windows(
        counts, ceiling=ceiling, date_from=date_from, date_to=date_to
    )
    if len(windows) == 1 and not windows[0].get("over_limit"):
        # One range is the caller's own range back again: following it
        # would only repeat the call that got here.
        return unavailable
    return {"suggested_windows": windows}
