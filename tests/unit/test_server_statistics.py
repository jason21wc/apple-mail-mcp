"""Server-layer tests for the grouped, IMAP-only side of ``get_statistics``.

The summary itself is covered in test_search_summary.py, and the original
sender roll-up in test_server.py. These check the routing between the two:
large scans and the subject/day groupings never reach the AppleScript-capable
search path.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from imapclient.exceptions import IMAPClientError

from apple_mail_fast_mcp import query_moves, server
from apple_mail_fast_mcp.exceptions import MailKeychainEntryNotFoundError
from tests.unit.test_search_summary import FakeConnector, _client, _reports


@pytest.fixture
def mail(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    mock.search_messages.return_value = []
    monkeypatch.setattr(server, "mail", mock)
    monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
    return mock


@pytest.fixture
def imap(monkeypatch: pytest.MonkeyPatch, mail: MagicMock) -> MagicMock:
    client = _client(_reports(8))
    connector = FakeConnector(client)
    monkeypatch.setattr(query_moves, "connector_for", lambda _mail, _account: connector)
    return client


def _stats(**overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {"account": "iCloud", "mailbox": "_Fosse Export"}
    args.update(overrides)
    return server.get_statistics(**args)


class TestGroupedStatistics:
    def test_by_subject_counts_patterns_without_rows(
        self, mail: MagicMock, imap: MagicMock
    ) -> None:
        out = _stats(by="subject", date_from="2026-05-01", date_to="2026-05-07")
        assert out["success"] is True
        stats = out["statistics"]
        assert stats["by"] == "subject"
        assert stats["total"] == 8
        assert stats["scanned"] == 8
        assert stats["window_fully_covered"] is True
        assert len(stats["groups"]) == 4
        assert stats["groups"][0]["count"] == 2
        assert stats["unread"] == 4
        assert stats["read_ratio"] == 0.5
        assert "messages" not in out and "messages" not in stats
        # Subjects are mail content.
        assert out["content_is_untrusted"] is True
        mail.search_messages.assert_not_called()

    def test_by_day(self, mail: MagicMock, imap: MagicMock) -> None:
        out = _stats(by="day", date_from="2026-05-01")
        assert out["statistics"]["groups"] == [
            {"key": "2026-05-03", "count": 4},
            {"key": "2026-05-02", "count": 4},
        ]

    def test_top_senders_is_still_reported(
        self, mail: MagicMock, imap: MagicMock
    ) -> None:
        out = _stats(by="subject", date_from="2026-05-01")
        assert out["statistics"]["top_senders"] == [
            {"address": "lp@denbt1.marriott.com", "count": 8}
        ]

    def test_large_scan_by_address_uses_imap_only(
        self, mail: MagicMock, imap: MagicMock
    ) -> None:
        out = _stats(by="domain", scan_limit=5000, date_from="2026-05-01")
        stats = out["statistics"]
        assert stats["top_senders"] == [{"domain": "denbt1.marriott.com", "count": 8}]
        assert stats["groups"] == [{"key": "denbt1.marriott.com", "count": 8}]
        mail.search_messages.assert_not_called()

    def test_filters_reach_the_server_search(
        self, mail: MagicMock, imap: MagicMock
    ) -> None:
        _stats(
            by="subject", sender_contains="lp@denbt1.marriott.com",
            subject_contains="Daily", date_from="2026-05-01", date_to="2026-05-07",
        )
        assert imap.search.call_args.args[0] == [
            "FROM", "lp@denbt1.marriott.com", "SUBJECT", "Daily",
            "SINCE", "01-May-2026", "BEFORE", "08-May-2026",
        ]

    def test_group_count_follows_top_senders_limit(
        self, mail: MagicMock, imap: MagicMock
    ) -> None:
        stats = _stats(by="subject", date_from="2026-05-01", top_senders_limit=1)[
            "statistics"
        ]
        assert len(stats["groups"]) == 1
        assert stats["other_groups"] == 3
        assert stats["other_count"] == 6

    def test_no_dates_means_the_last_thirty_days(
        self, mail: MagicMock, imap: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self._spy(monkeypatch)
        before = datetime.now() - timedelta(hours=720)
        _stats(by="subject")
        after = datetime.now() - timedelta(hours=720)
        # The exact hour is applied to the received dates as they are read...
        assert before <= seen["received_since"] <= after
        # ...and the server is asked from a day early, so a time-zone
        # difference in its day boundary cannot drop a message.
        assert seen["date_from"] == (before.date() - timedelta(days=1)).isoformat()

    @staticmethod
    def _spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        seen: dict[str, Any] = {}
        real = server.search_summary.summarize

        def spy(connector: Any, **kwargs: Any) -> dict[str, Any]:
            seen.update(kwargs)
            return real(connector, **kwargs)

        monkeypatch.setattr(server.search_summary, "summarize", spy)
        return seen

    def test_hours_do_not_widen_a_later_date_from(
        self, mail: MagicMock, imap: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen = self._spy(monkeypatch)
        later = (datetime.now() - timedelta(days=2)).date().isoformat()
        _stats(by="day", date_from=later, received_within_hours=720)
        assert seen["date_from"] == later
        assert seen["received_since"] is not None

    def test_hours_with_a_malformed_date_is_rejected_not_replaced(
        self, mail: MagicMock, imap: MagicMock
    ) -> None:
        out = _stats(by="day", date_from="05/01/2026", received_within_hours=48)
        assert out["error_type"] == "validation_error"
        imap.search.assert_not_called()

    def test_scan_limit_boundary_decides_the_path(
        self, mail: MagicMock, imap: MagicMock
    ) -> None:
        _stats(scan_limit=500, date_from="2026-05-01")
        mail.search_messages.assert_called_once()
        imap.search.assert_not_called()
        _stats(scan_limit=501, date_from="2026-05-01")
        mail.search_messages.assert_called_once()  # not called again
        imap.search.assert_called_once()

    def test_domain_grouping_leaves_senderless_mail_out_of_top_senders(
        self, mail: MagicMock, imap: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = server.search_summary.summarize

        def with_senderless(connector: Any, **kwargs: Any) -> dict[str, Any]:
            out = real(connector, **kwargs)
            out["groups"].append({"key": server.search_summary.NO_SENDER, "count": 1})
            return out

        monkeypatch.setattr(server.search_summary, "summarize", with_senderless)
        stats = _stats(by="domain", scan_limit=600, date_from="2026-05-01")["statistics"]
        assert stats["top_senders"] == [{"domain": "denbt1.marriott.com", "count": 8}]
        assert len(stats["groups"]) == 2

    def test_a_connection_lost_part_way_is_reported_with_the_counts(
        self, mail: MagicMock, imap: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = server.search_summary.summarize

        def interrupted(connector: Any, **kwargs: Any) -> dict[str, Any]:
            return {**real(connector, **kwargs), "interrupted": True}

        monkeypatch.setattr(server.search_summary, "summarize", interrupted)
        out = _stats(by="subject", date_from="2026-05-01")
        assert out["success"] is True
        assert out["statistics"]["interrupted"] is True

    def test_explicit_dates_are_not_cut_to_thirty_days(
        self, mail: MagicMock, imap: MagicMock
    ) -> None:
        """Asking for a 2022 range used to return nothing, because the
        default 30-day window was applied on top of it."""
        _stats(by="subject", date_from="2022-06-01", date_to="2022-06-30")
        assert imap.search.call_args.args[0] == [
            "SINCE", "01-Jun-2022", "BEFORE", "01-Jul-2022",
        ]

    def test_without_imap_it_fails_instead_of_falling_back(
        self, mail: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_imap(_mail: Any, _account: str) -> Any:
            raise MailKeychainEntryNotFoundError("no password stored")

        monkeypatch.setattr(query_moves, "connector_for", no_imap)
        out = _stats(by="subject", date_from="2026-05-01")
        assert out["success"] is False
        assert out["error_type"] == "imap_required"
        mail.search_messages.assert_not_called()

    def test_large_scan_without_imap_also_fails(
        self, mail: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_imap(_mail: Any, _account: str) -> Any:
            raise MailKeychainEntryNotFoundError("no password stored")

        monkeypatch.setattr(query_moves, "connector_for", no_imap)
        out = _stats(scan_limit=501)
        assert out["error_type"] == "imap_required"
        mail.search_messages.assert_not_called()

    def test_a_network_failure_is_reported_as_imap_error(
        self, mail: MagicMock, imap: MagicMock
    ) -> None:
        imap.search.side_effect = IMAPClientError("server said no")
        out = _stats(by="day", date_from="2026-05-01")
        assert out["success"] is False
        assert out["error_type"] == "imap_error"

    @pytest.mark.parametrize(
        "overrides",
        [
            {"by": "colour"},
            {"by": "subject", "scan_limit": 20001},
            {"by": "subject", "scan_limit": 0},
            {"by": "subject", "sender_contains": " "},
            {"by": "subject", "date_from": "05/01/2026"},
            {"by": "subject", "received_within_hours": 0},
            {"by": "subject", "date_to": "tomorrow"},
            # The original path validates the same way.
            {"scan_limit": 0},
            {"scan_limit": -5},
        ],
    )
    def test_bad_arguments_are_validation_errors(
        self, mail: MagicMock, imap: MagicMock, overrides: dict[str, Any]
    ) -> None:
        out = _stats(**overrides)
        assert out["success"] is False
        assert out["error_type"] == "validation_error"
        imap.search.assert_not_called()
        mail.search_messages.assert_not_called()


class TestOriginalPathUnchanged:
    def test_default_call_still_uses_the_search_path(
        self, mail: MagicMock, imap: MagicMock
    ) -> None:
        mail.search_messages.return_value = [
            {"sender": "a@example.com", "read_status": True, "flagged": False}
        ]
        out = _stats(mailbox="INBOX")
        assert out["success"] is True
        assert out["statistics"]["top_senders"] == [
            {"address": "a@example.com", "count": 1}
        ]
        kwargs = mail.search_messages.call_args.kwargs
        assert kwargs["received_within_hours"] == 720
        assert kwargs["limit"] == 500
        imap.search.assert_not_called()

    def test_explicit_dates_drop_the_default_window(self, mail: MagicMock) -> None:
        _stats(date_from="2022-06-01", date_to="2022-06-30")
        kwargs = mail.search_messages.call_args.kwargs
        assert kwargs["received_within_hours"] is None
        assert kwargs["date_from"] == "2022-06-01"

    def test_explicit_hours_still_compose_with_dates(self, mail: MagicMock) -> None:
        _stats(date_from="2026-05-01", received_within_hours=48)
        assert mail.search_messages.call_args.kwargs["received_within_hours"] == 48

    def test_filters_are_passed_through(self, mail: MagicMock) -> None:
        _stats(sender_contains="a@example.com", subject_contains="Invoice")
        kwargs = mail.search_messages.call_args.kwargs
        assert kwargs["sender_contains"] == "a@example.com"
        assert kwargs["subject_contains"] == "Invoice"
