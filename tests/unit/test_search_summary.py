"""Unit tests for ``search_summary`` (fork-local).

Counts grouped by sender, subject pattern or day, read over IMAP in bounded
chunks, so asking what a large mailbox holds does not return thousands of
rows and never goes near the AppleScript path.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from imapclient.exceptions import IMAPClientAbortError
from imapclient.response_types import Address, Envelope

from apple_mail_fast_mcp import search_summary as ss


def _envelope(sender: str, subject: str) -> Envelope:
    mailbox, host = sender.split("@")
    addr = Address(None, None, mailbox.encode(), host.encode())
    return Envelope(
        datetime(2026, 5, 5, 0, 3), subject.encode(), (addr,), (addr,), (addr,),
        None, None, None, None, b"<id@x>",
    )


class FakeConnector:
    def __init__(self, client: MagicMock) -> None:
        self.client = client

    @contextmanager
    def _session(self) -> Iterator[MagicMock]:
        yield self.client


def _client(messages: dict[int, dict[str, Any]]) -> MagicMock:
    """``messages`` maps UID to sender, subject, received and flags."""
    client = MagicMock()
    client.select_folder.return_value = {b"UIDVALIDITY": 7}
    client.search.return_value = sorted(messages)

    def fetch(chunk: list[int], parts: list[str]) -> dict[int, dict[bytes, Any]]:
        out: dict[int, dict[bytes, Any]] = {}
        for uid in chunk:
            row = messages.get(uid)
            if row is None:
                continue
            out[uid] = {
                b"ENVELOPE": _envelope(row["sender"], row["subject"]),
                b"INTERNALDATE": row["received"],
                b"FLAGS": row.get("flags", ()),
            }
        return out

    client.fetch.side_effect = fetch
    return client


def _reports(count: int = 8) -> dict[int, dict[str, Any]]:
    kinds = [
        "Daily Closing Report Summary",
        "Credit Card Activity",
        "Daily Segmentation Report",
        "Detail Aging Report",
    ]
    out: dict[int, dict[str, Any]] = {}
    for uid in range(1, count + 1):
        day = 1 + (uid - 1) // 4
        out[uid] = {
            "sender": "lp@denbt1.marriott.com",
            "subject": f"{kinds[(uid - 1) % 4]} {day:02d}May26 from denbt1.marriott.com",
            "received": datetime(2026, 5, day + 1, 0, 5),
            "flags": (b"\\Seen",) if uid % 2 else (),
        }
    return out


def _summarize(client: MagicMock, **overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {"mailbox": "_Fosse Export", "by": "subject"}
    args.update(overrides)
    return ss.summarize(FakeConnector(client), **args)


class TestNormalizeSubject:
    @pytest.mark.parametrize(
        ("subject", "expected"),
        [
            (
                "Daily Closing Report Summary 04May26 from denbt1.marriott.com",
                "Daily Closing Report Summary # from denbt#.marriott.com",
            ),
            ("Re: RE: Fwd: Invoice 10442 due", "Invoice # due"),
            ("Statement  for   2026-05-04", "Statement for #"),
            ("Order #123-4567890-1234567 shipped", "Order # shipped"),
            ("", "(no subject)"),
            ("   ", "(no subject)"),
        ],
    )
    def test_varying_parts_collapse(self, subject: str, expected: str) -> None:
        assert ss.normalize_subject(subject) == expected

    def test_same_report_on_different_days_is_one_group(self) -> None:
        a = ss.normalize_subject("Credit Card Activity 04May26 from denbt1.marriott.com")
        b = ss.normalize_subject("Credit Card Activity 31Dec25 from denbt1.marriott.com")
        assert a == b

    def test_long_subject_is_cut(self) -> None:
        assert len(ss.normalize_subject("x" * 500)) <= ss.SUBJECT_KEY_LIMIT


class TestSummarize:
    def test_groups_by_subject_pattern(self) -> None:
        out = _summarize(_client(_reports(8)))
        assert out["total"] == 8
        assert out["scanned"] == 8
        assert out["window_fully_covered"] is True
        assert out["groups"] == [
            {"key": "Credit Card Activity # from denbt#.marriott.com", "count": 2},
            {"key": "Daily Closing Report Summary # from denbt#.marriott.com", "count": 2},
            {"key": "Daily Segmentation Report # from denbt#.marriott.com", "count": 2},
            {"key": "Detail Aging Report # from denbt#.marriott.com", "count": 2},
        ]
        assert out["other_groups"] == 0
        assert out["other_count"] == 0
        assert "messages" not in out

    def test_groups_by_day_newest_first_from_received_date(self) -> None:
        out = _summarize(_client(_reports(8)), by="day")
        assert out["groups"] == [
            {"key": "2026-05-03", "count": 4},
            {"key": "2026-05-02", "count": 4},
        ]

    def test_groups_by_address_and_domain(self) -> None:
        messages = _reports(4)
        messages[5] = {
            "sender": "news@example.com", "subject": "Hi",
            "received": datetime(2026, 5, 2), "flags": (),
        }
        by_address = _summarize(_client(messages), by="address")
        assert by_address["groups"][0] == {"key": "lp@denbt1.marriott.com", "count": 4}
        by_domain = _summarize(_client(messages), by="domain")
        assert by_domain["groups"] == [
            {"key": "denbt1.marriott.com", "count": 4},
            {"key": "example.com", "count": 1},
        ]

    def test_counts_unread_and_flagged(self) -> None:
        messages = _reports(4)
        messages[1]["flags"] = (b"\\Seen", b"\\Flagged")
        out = _summarize(_client(messages))
        assert out["unread"] == 2
        assert out["flagged"] == 1

    def test_group_limit_sums_the_rest(self) -> None:
        out = _summarize(_client(_reports(8)), group_limit=1)
        assert len(out["groups"]) == 1
        assert out["other_groups"] == 3
        assert out["other_count"] == 6

    def test_scan_limit_reads_the_newest_and_says_it_is_partial(self) -> None:
        client = _client(_reports(8))
        out = _summarize(client, by="day", scan_limit=4)
        assert out["total"] == 8
        assert out["scanned"] == 4
        assert out["window_fully_covered"] is False
        assert out["groups"] == [{"key": "2026-05-03", "count": 4}]

    def test_reads_in_chunks(self) -> None:
        messages = {
            u: {"sender": "a@x.com", "subject": "s", "received": datetime(2026, 1, 1)}
            for u in range(1, 251)
        }
        client = _client(messages)
        out = _summarize(client)
        assert out["scanned"] == 250
        assert client.fetch.call_count == 3  # 100 per fetch

    def test_out_of_time_stops_and_says_so(self) -> None:
        messages = {
            u: {"sender": "a@x.com", "subject": "s", "received": datetime(2026, 1, 1)}
            for u in range(1, 301)
        }
        ticks = iter([0.0, 1.0, 30.0, 31.0, 32.0])
        out = _summarize(_client(messages), budget=20.0, clock=lambda: next(ticks))
        assert out["total"] == 300
        assert out["scanned"] == 100
        assert out["window_fully_covered"] is False

    def test_filters_become_search_criteria(self) -> None:
        client = _client(_reports(4))
        _summarize(
            client, sender_contains="lp@denbt1.marriott.com",
            subject_contains="Daily", date_from="2026-05-01", date_to="2026-05-07",
        )
        criteria = client.search.call_args.args[0]
        assert criteria == [
            "FROM", "lp@denbt1.marriott.com", "SUBJECT", "Daily",
            "SINCE", "01-May-2026", "BEFORE", "08-May-2026",
        ]

    @pytest.mark.parametrize("field", ["sender_contains", "subject_contains"])
    def test_blank_filter_is_rejected_not_ignored(self, field: str) -> None:
        client = _client(_reports(4))
        with pytest.raises(ValueError, match=field):
            _summarize(client, **{field: "  "})
        client.search.assert_not_called()

    def test_unknown_grouping_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="by"):
            _summarize(_client(_reports(4)), by="colour")

    def test_scan_limit_bounds_are_enforced(self) -> None:
        with pytest.raises(ValueError, match="scan_limit"):
            _summarize(_client(_reports(4)), scan_limit=0)
        with pytest.raises(ValueError, match="scan_limit"):
            _summarize(_client(_reports(4)), scan_limit=ss.MAX_SCAN + 1)

    def test_received_since_leaves_out_older_messages(self) -> None:
        out = _summarize(
            _client(_reports(8)), by="day", received_since=datetime(2026, 5, 3)
        )
        assert out["groups"] == [{"key": "2026-05-03", "count": 4}]
        assert out["total"] == 4

    def test_received_since_with_a_partial_scan_gives_an_upper_bound(self) -> None:
        """Messages that were not read cannot be checked against the hour."""
        out = _summarize(
            _client(_reports(8)), by="day",
            received_since=datetime(2026, 5, 3), scan_limit=6,
        )
        # Newest six read: four kept, two too old. Two older ones never read.
        assert out["scanned"] == 4
        assert out["total"] == 6
        assert out["window_fully_covered"] is False

    def test_a_message_gone_since_the_search_does_not_look_like_a_partial_scan(
        self,
    ) -> None:
        messages = _reports(8)
        client = _client(messages)
        client.search.return_value = [*sorted(messages), 99]  # 99 was expunged
        out = _summarize(client)
        assert out["scanned"] == 8
        assert out["window_fully_covered"] is True

    def test_a_reply_without_an_envelope_is_not_counted(self) -> None:
        """imapclient files an unsolicited flags update under a sequence
        number, which can collide with a UID that was asked for."""
        messages = _reports(4)
        client = _client(messages)
        real_fetch = client.fetch.side_effect

        def fetch(chunk: list[int], parts: list[str]) -> dict[int, dict[bytes, Any]]:
            out = real_fetch(chunk, parts)
            out[3] = {b"FLAGS": (b"\\Seen",)}  # no ENVELOPE: not our reply
            return out

        client.fetch.side_effect = fetch
        out = _summarize(client)
        assert out["scanned"] == 3
        assert sum(g["count"] for g in out["groups"]) == 3
        assert "(no subject)" not in [g["key"] for g in out["groups"]]

    def test_a_connection_lost_part_way_keeps_what_was_counted(self) -> None:
        messages = {
            u: {"sender": "a@x.com", "subject": "s", "received": datetime(2026, 1, 1)}
            for u in range(1, 251)
        }
        client = _client(messages)
        real_fetch = client.fetch.side_effect
        calls = {"n": 0}

        def fetch(chunk: list[int], parts: list[str]) -> dict[int, dict[bytes, Any]]:
            calls["n"] += 1
            if calls["n"] == 2:
                raise TimeoutError("timed out")
            return real_fetch(chunk, parts)

        client.fetch.side_effect = fetch
        left_with: list[BaseException | None] = []

        class Recording(FakeConnector):
            @contextmanager
            def _session(self) -> Iterator[MagicMock]:
                try:
                    yield self.client
                except BaseException as exc:
                    left_with.append(exc)
                    raise
                else:
                    left_with.append(None)

        out = ss.summarize(Recording(client), mailbox="Big", by="address")
        assert out["interrupted"] is True
        assert out["total"] == 250
        assert out["scanned"] == 100
        assert out["window_fully_covered"] is False
        assert out["groups"] == [{"key": "a@x.com", "count": 100}]
        # It left the session as an exception, so the pool drops the
        # broken connection instead of reusing it.
        assert left_with and left_with[0] is not None

    def test_a_timeout_before_anything_was_read_is_raised(self) -> None:
        client = _client(_reports(4))
        client.fetch.side_effect = TimeoutError("timed out")
        with pytest.raises(TimeoutError):
            _summarize(client)

    def test_non_ascii_filter_is_sent_with_a_charset(self) -> None:
        client = _client(_reports(4))
        _summarize(client, subject_contains="Zürich")
        assert client.search.call_args.args[1] == "UTF-8"

    def test_parse_day(self) -> None:
        assert ss.parse_day(None, "date_from") is None
        assert ss.parse_day("2026-05-01", "date_from").isoformat() == "2026-05-01"
        with pytest.raises(ValueError, match="date_from"):
            ss.parse_day("05/01/2026", "date_from")

    def test_nothing_matched(self) -> None:
        client = _client({})
        out = _summarize(client)
        assert out["total"] == 0
        assert out["groups"] == []
        assert out["window_fully_covered"] is True
        client.fetch.assert_not_called()

    def test_mailbox_is_opened_read_only_and_nothing_is_changed(self) -> None:
        client = _client(_reports(4))
        _summarize(client)
        client.select_folder.assert_called_once_with("_Fosse Export", readonly=True)
        for name in ("move", "copy", "add_flags", "remove_flags", "expunge",
                     "uid_expunge", "delete_messages", "set_flags"):
            getattr(client, name).assert_not_called()

    def test_a_broken_connection_is_raised(self) -> None:
        client = _client(_reports(4))
        client.fetch.side_effect = IMAPClientAbortError("gone")
        with pytest.raises(IMAPClientAbortError):
            _summarize(client)

    def test_control_characters_in_the_mailbox_name_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="control character"):
            _summarize(_client(_reports(4)), mailbox="In\r\nbox")
