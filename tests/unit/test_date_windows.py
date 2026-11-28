"""Unit tests for ``date_windows`` (fork-local).

When a selection is larger than a tool's ceiling, the caller is handed date
ranges that each fit. The property that matters most: the ranges together
cover exactly the range the caller asked for, so no message can fall between
two of them or off either end.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from imapclient.exceptions import IMAPClientAbortError, IMAPClientError

from apple_mail_fast_mcp import date_windows as dw


def _counts(days: dict[str, int]) -> Counter[date]:
    return Counter({date.fromisoformat(k): v for k, v in days.items()})


def _client(dates: dict[int, datetime]) -> MagicMock:
    client = MagicMock()

    def fetch(chunk: list[int], parts: list[str]) -> dict[int, dict[bytes, Any]]:
        assert parts == ["INTERNALDATE"]
        return {u: {b"INTERNALDATE": dates[u]} for u in chunk if u in dates}

    client.fetch.side_effect = fetch
    return client


class TestPackWindows:
    def test_packs_consecutive_days_up_to_the_fill_target(self) -> None:
        counts = _counts(
            {"2026-01-01": 40, "2026-01-02": 40, "2026-01-03": 40, "2026-01-04": 40}
        )
        windows = dw.pack_windows(
            counts, ceiling=100, date_from="2026-01-01", date_to="2026-01-04"
        )
        # Target is 90: two days (80) fit, a third (120) does not.
        assert windows == [
            {"date_from": "2026-01-01", "date_to": "2026-01-02", "approx_matched": 80},
            {"date_from": "2026-01-03", "date_to": "2026-01-04", "approx_matched": 80},
        ]

    def test_windows_are_contiguous_across_days_with_no_mail(self) -> None:
        counts = _counts({"2026-01-01": 80, "2026-01-10": 80, "2026-02-01": 80})
        windows = dw.pack_windows(counts, ceiling=100, date_from=None, date_to=None)
        assert len(windows) == 3
        for earlier, later in zip(windows, windows[1:], strict=False):
            gap = date.fromisoformat(later["date_from"]) - date.fromisoformat(
                earlier["date_to"]
            )
            assert gap == timedelta(days=1)

    def test_outer_bounds_are_the_callers_not_the_observed_days(self) -> None:
        """Day buckets come from local time and the server compares its own
        date, so the newest or oldest message may sit a day outside what was
        observed. Keeping the caller's bounds means it is still covered."""
        counts = _counts({"2026-03-05": 80, "2026-03-06": 80})
        windows = dw.pack_windows(
            counts, ceiling=100, date_from="2026-03-01", date_to="2026-03-31"
        )
        assert windows[0]["date_from"] == "2026-03-01"
        assert windows[-1]["date_to"] == "2026-03-31"

    def test_open_bounds_stay_open(self) -> None:
        counts = _counts({"2026-03-05": 80, "2026-03-06": 80})
        windows = dw.pack_windows(counts, ceiling=100, date_from=None, date_to=None)
        assert windows[0]["date_from"] is None
        assert windows[0]["date_to"] == "2026-03-05"
        assert windows[-1]["date_from"] == "2026-03-06"
        assert windows[-1]["date_to"] is None

    def test_a_single_day_above_the_ceiling_is_flagged(self) -> None:
        counts = _counts({"2026-03-05": 10, "2026-03-06": 500, "2026-03-07": 10})
        windows = dw.pack_windows(counts, ceiling=100, date_from=None, date_to=None)
        big = [w for w in windows if w.get("over_limit")]
        assert big == [
            {
                "date_from": "2026-03-06",
                "date_to": "2026-03-06",
                "approx_matched": 500,
                "over_limit": True,
                "day": "2026-03-06",
            }
        ]
        assert sum(w["approx_matched"] for w in windows) == 520

    def test_an_over_limit_day_is_named_even_when_it_carries_open_bounds(self) -> None:
        """As the only range it takes the caller's (open) bounds, so the day
        itself has to be stated separately."""
        windows = dw.pack_windows(
            _counts({"2026-03-06": 500}), ceiling=100, date_from=None, date_to=None
        )
        assert windows == [
            {
                "date_from": None,
                "date_to": None,
                "approx_matched": 500,
                "over_limit": True,
                "day": "2026-03-06",
            }
        ]

    @pytest.mark.parametrize(
        ("observed", "date_from", "date_to"),
        [
            # A message bucketed the day after the caller's upper bound.
            ({"2026-05-14": 80, "2026-05-15": 80, "2026-05-16": 30}, None, "2026-05-15"),
            # ...and the day before the lower bound.
            ({"2026-04-30": 30, "2026-05-01": 80, "2026-05-02": 80}, "2026-05-01", None),
            ({"2026-04-30": 95, "2026-05-01": 95}, "2026-05-01", "2026-05-01"),
        ],
    )
    def test_days_outside_the_callers_bounds_never_invert_a_range(
        self, observed: dict[str, int], date_from: str | None, date_to: str | None
    ) -> None:
        windows = dw.pack_windows(
            _counts(observed), ceiling=100, date_from=date_from, date_to=date_to
        )
        assert sum(w["approx_matched"] for w in windows) == sum(observed.values())
        for window in windows:
            if window["date_from"] and window["date_to"]:
                assert window["date_from"] <= window["date_to"]
        assert windows[0]["date_from"] == date_from
        assert windows[-1]["date_to"] == date_to

    def test_ranges_with_caller_bounds_are_contiguous_and_ordered(self) -> None:
        counts = _counts({f"2026-02-{d:02d}": 30 for d in range(3, 27, 2)})
        windows = dw.pack_windows(
            counts, ceiling=100, date_from="2026-01-15", date_to="2026-03-15"
        )
        assert len(windows) > 2
        assert windows[0]["date_from"] == "2026-01-15"
        assert windows[-1]["date_to"] == "2026-03-15"
        for window in windows:
            assert window["date_from"] <= window["date_to"]
        for earlier, later in zip(windows, windows[1:], strict=False):
            gap = date.fromisoformat(later["date_from"]) - date.fromisoformat(
                earlier["date_to"]
            )
            assert gap == timedelta(days=1)

    def test_a_day_between_target_and_ceiling_gets_its_own_window(self) -> None:
        counts = _counts({"2026-03-05": 95, "2026-03-06": 20})
        windows = dw.pack_windows(counts, ceiling=100, date_from=None, date_to=None)
        assert [w["approx_matched"] for w in windows] == [95, 20]
        assert not any(w.get("over_limit") for w in windows)

    def test_every_message_is_counted_once(self) -> None:
        counts = _counts({f"2026-01-{d:02d}": d * 3 for d in range(1, 29)})
        windows = dw.pack_windows(counts, ceiling=120, date_from=None, date_to=None)
        assert sum(w["approx_matched"] for w in windows) == sum(counts.values())

    def test_no_days_gives_no_windows(self) -> None:
        assert dw.pack_windows(Counter(), ceiling=100, date_from=None, date_to=None) == []


class TestReadDayCounts:
    def test_counts_by_day_in_chunks(self) -> None:
        dates = {
            u: datetime(2026, 1, 1 + (u % 3), 12, 0) for u in range(1, 2501)
        }
        client = _client(dates)
        counts = dw.read_day_counts(client, sorted(dates))
        assert counts is not None
        assert sum(counts.values()) == 2500
        assert set(counts) == {date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)}
        assert client.fetch.call_count == 3  # 1,000 per fetch

    def test_too_many_uids_is_not_attempted(self) -> None:
        client = _client({})
        assert dw.read_day_counts(client, list(range(1, 12)), uid_limit=10) is None
        client.fetch.assert_not_called()

    def test_out_of_time_gives_up(self) -> None:
        dates = {u: datetime(2026, 1, 1) for u in range(1, 3001)}
        ticks = iter([0.0, 1.0, 99.0, 100.0, 101.0])
        counts = dw.read_day_counts(
            _client(dates), sorted(dates), budget=15.0, clock=lambda: next(ticks)
        )
        assert counts is None

    def test_a_server_error_gives_up_instead_of_raising(self) -> None:
        client = MagicMock()
        client.fetch.side_effect = IMAPClientError("no")
        assert dw.read_day_counts(client, [1, 2, 3]) is None

    @pytest.mark.parametrize(
        "failure",
        [IMAPClientAbortError("gone"), TimeoutError("timed out"), OSError("reset")],
    )
    def test_a_broken_connection_is_raised_not_swallowed(
        self, failure: Exception
    ) -> None:
        """Swallowing it would leave the dead connection in the pool for the
        next call to trip over."""
        client = MagicMock()
        client.fetch.side_effect = failure
        with pytest.raises(type(failure)):
            dw.read_day_counts(client, [1, 2, 3])

    def test_each_read_is_capped_by_what_is_left_of_the_budget(self) -> None:
        dates = {u: datetime(2026, 1, 1) for u in range(1, 1501)}
        client = _client(dates)
        sock = client.socket.return_value
        sock.gettimeout.return_value = 30.0
        ticks = iter([0.0, 5.0, 11.0])
        dw.read_day_counts(
            client, sorted(dates), budget=15.0, clock=lambda: next(ticks)
        )
        applied = [call.args[0] for call in sock.settimeout.call_args_list]
        # 10 s left, restored; then 4 s left, restored.
        assert applied == [10.0, 30.0, 4.0, 30.0]

    def test_a_message_without_a_date_is_left_out(self) -> None:
        client = MagicMock()
        client.fetch.return_value = {
            1: {b"INTERNALDATE": datetime(2026, 1, 1)},
            2: {b"INTERNALDATE": None},
            3: {},
        }
        counts = dw.read_day_counts(client, [1, 2, 3])
        assert counts == Counter({date(2026, 1, 1): 1})


class TestSuggest:
    def test_returns_windows_for_a_selection_over_the_ceiling(self) -> None:
        dates = {u: datetime(2026, 1, 1 + (u % 4), 8, 0) for u in range(1, 401)}
        out = dw.suggest(
            _client(dates), sorted(dates), ceiling=250,
            date_from=None, date_to="2026-05-16",
        )
        assert "windows_unavailable" not in out
        windows = out["suggested_windows"]
        assert sum(w["approx_matched"] for w in windows) == 400
        assert all(w["approx_matched"] <= 250 for w in windows)
        assert windows[-1]["date_to"] == "2026-05-16"

    def test_says_so_when_windows_cannot_be_worked_out(self) -> None:
        client = MagicMock()
        client.fetch.side_effect = IMAPClientError("no")
        out = dw.suggest(client, [1, 2, 3], ceiling=2, date_from=None, date_to=None)
        assert out == {"windows_unavailable": True}

    @pytest.mark.parametrize("local", [datetime(2026, 5, 17, 1, 0), datetime(2026, 4, 30, 23, 0)])
    def test_a_message_bucketed_outside_the_range_is_still_covered(
        self, local: datetime
    ) -> None:
        """The server matched it inside the caller's dates, but its local-time
        day is one outside them (a time-zone skew near midnight). It must be
        counted in a range, and the ranges must stay inside the bounds."""
        dates = {u: datetime(2026, 5, 3 + (u % 10), 12, 0) for u in range(1, 200)}
        dates[200] = local
        out = dw.suggest(
            _client(dates), sorted(dates), ceiling=60,
            date_from="2026-05-01", date_to="2026-05-16",
        )
        windows = out["suggested_windows"]
        assert sum(w["approx_matched"] for w in windows) == 200
        assert windows[0]["date_from"] == "2026-05-01"
        assert windows[-1]["date_to"] == "2026-05-16"
        for window in windows:
            assert "2026-05-01" <= window["date_from"] <= window["date_to"] <= "2026-05-16"

    def test_too_few_readable_dates_offers_no_ranges(self) -> None:
        """Ranges built from a fraction of the messages would each look
        smaller than they are."""
        dates = {u: datetime(2026, 1, 1 + (u % 4)) for u in range(1, 81)}
        out = dw.suggest(
            _client(dates), list(range(1, 101)), ceiling=50,
            date_from=None, date_to=None,
        )
        assert out == {"windows_unavailable": True}

    def test_a_single_range_that_fits_is_not_offered(self) -> None:
        """It would be the caller's own range back again."""
        dates = {u: datetime(2026, 1, 1) for u in range(1, 96)}
        out = dw.suggest(
            _client(dates), list(range(1, 102)), ceiling=110,
            date_from=None, date_to=None,
        )
        assert out == {"windows_unavailable": True}

    def test_a_broken_connection_propagates(self) -> None:
        client = MagicMock()
        client.fetch.side_effect = IMAPClientAbortError("gone")
        with pytest.raises(IMAPClientAbortError):
            dw.suggest(client, [1, 2, 3], ceiling=2, date_from=None, date_to=None)
