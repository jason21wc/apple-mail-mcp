"""Server-layer tests for preview_move_by_query / commit_move_by_query.

``query_moves`` itself is covered in test_query_moves.py. These check what
the tool wrappers add: gates, confirmation, and structured errors.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.server.elicitation import AcceptedElicitation, DeclinedElicitation
from imapclient.exceptions import IMAPClientError

from apple_mail_fast_mcp import query_moves, security, server
from apple_mail_fast_mcp.exceptions import MailKeychainEntryNotFoundError
from tests.unit.test_query_moves import FakeConnector, _client


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> FakeConnector:
    connector = FakeConnector(_client([1, 2, 3]))
    monkeypatch.setattr(server, "_query_move_store", query_moves.PreviewStore())
    monkeypatch.setattr(query_moves, "connector_for", lambda _mail, _account: connector)
    monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
    monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
    return connector


def _preview(**overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "account": "iCloud",
        "source_mailbox": "News",
        "destination_mailbox": "_To Delete",
        "sender_contains": "news@example.com",
        "date_to": "2026-05-16",
    }
    args.update(overrides)
    return server.preview_move_by_query(**args)


class TestPreviewTool:
    def test_success_is_marked_untrusted(self, backend: FakeConnector) -> None:
        out = _preview()
        assert out["success"] is True
        assert out["matched"] == 3
        assert out["preview_id"]
        assert out["content_is_untrusted"] is True
        backend.client.move.assert_not_called()

    def test_blank_filter_is_a_validation_error(self, backend: FakeConnector) -> None:
        out = _preview(sender_contains="")
        assert out["success"] is False
        assert out["error_type"] == "validation_error"
        backend.client.search.assert_not_called()

    def test_no_filters_is_a_validation_error(self, backend: FakeConnector) -> None:
        out = server.preview_move_by_query("iCloud", "News", "_To Delete")
        assert out["error_type"] == "validation_error"

    def test_missing_imap_credentials_fail_closed(
        self, backend: FakeConnector, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_creds(_mail: Any, _account: str) -> Any:
            raise MailKeychainEntryNotFoundError("no entry")

        monkeypatch.setattr(query_moves, "connector_for", no_creds)
        out = _preview()
        assert out["success"] is False
        assert out["error_type"] == "imap_required"

    def test_imap_failure_is_reported_as_imap_error(self, backend: FakeConnector) -> None:
        backend.client.search.side_effect = IMAPClientError("boom")
        out = _preview()
        assert out["success"] is False
        assert out["error_type"] == "imap_error"

    def test_gmail_is_refused(self, backend: FakeConnector) -> None:
        backend.client.capabilities.return_value = (b"MOVE", b"X-GM-EXT-1")
        assert _preview()["error_type"] == "unsupported_account"

    def test_test_mode_blocks_a_real_account(
        self, backend: FakeConnector, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")
        monkeypatch.setattr(security, "_get_test_account_identifiers", lambda a: {a})
        out = _preview()
        assert out["success"] is False
        assert out["error_type"] == "safety_violation"
        backend.client.search.assert_not_called()

    def test_ceiling_env_override(
        self, backend: FakeConnector, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APPLE_MAIL_MCP_MAX_QUERY_MOVE", "2")
        out = _preview()
        assert out["over_limit"] is True
        assert out["preview_id"] is None


class TestCommitTool:
    async def test_moves_and_writes_receipt(
        self, backend: FakeConnector, tmp_path: Any
    ) -> None:
        pid = _preview()["preview_id"]
        out = await server.commit_move_by_query(pid, max_count=10)
        assert out["success"] is True
        assert (out["moved"], out["outcome"]) == (3, "complete")
        assert (tmp_path / "query_move_receipts.jsonl").exists()

    async def test_unknown_preview_id(self, backend: FakeConnector) -> None:
        out = await server.commit_move_by_query("nope", max_count=10)
        assert out["success"] is False
        assert out["error_type"] == "preview_not_found"

    async def test_max_count_exceeded(self, backend: FakeConnector) -> None:
        pid = _preview()["preview_id"]
        out = await server.commit_move_by_query(pid, max_count=2)
        assert out["error_type"] == "max_count_exceeded"
        backend.client.move.assert_not_called()

    async def test_failed_outcome_is_not_success(self, backend: FakeConnector) -> None:
        pid = _preview()["preview_id"]
        backend.client.move.side_effect = IMAPClientError("timeout")
        out = await server.commit_move_by_query(pid, max_count=10)
        assert out["success"] is False
        assert out["outcome"] == "failed"
        assert out["error_type"] == "imap_error"
        assert out["remaining"] == 3

    async def test_trash_destination_requires_confirmation(
        self, backend: FakeConnector
    ) -> None:
        pid = _preview(destination_mailbox="Deleted Messages")["preview_id"]

        blocked = await server.commit_move_by_query(pid, max_count=10, ctx=None)
        assert blocked["error_type"] == "confirmation_required"

        declined = MagicMock()
        declined.elicit = AsyncMock(return_value=DeclinedElicitation())
        out = await server.commit_move_by_query(pid, max_count=10, ctx=declined)
        assert out["error_type"] == "cancelled"
        backend.client.move.assert_not_called()

        accepted = MagicMock()
        accepted.elicit = AsyncMock(return_value=AcceptedElicitation(data=True))
        out = await server.commit_move_by_query(pid, max_count=10, ctx=accepted)
        assert out["moved"] == 3

    async def test_trash_prompt_is_not_shown_when_max_count_refuses(
        self, backend: FakeConnector
    ) -> None:
        pid = _preview(destination_mailbox="Trash")["preview_id"]
        ctx = MagicMock()
        ctx.elicit = AsyncMock()
        out = await server.commit_move_by_query(pid, max_count=1, ctx=ctx)
        assert out["error_type"] == "max_count_exceeded"
        ctx.elicit.assert_not_called()

    async def test_non_trash_destination_does_not_elicit(
        self, backend: FakeConnector
    ) -> None:
        pid = _preview()["preview_id"]
        ctx = MagicMock()
        ctx.elicit = AsyncMock()
        await server.commit_move_by_query(pid, max_count=10, ctx=ctx)
        ctx.elicit.assert_not_called()

    async def test_test_mode_blocks_commit_for_a_real_account(
        self, backend: FakeConnector, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        pid = _preview()["preview_id"]
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")
        monkeypatch.setattr(security, "_get_test_account_identifiers", lambda a: {a})
        out = await server.commit_move_by_query(pid, max_count=10)
        assert out["error_type"] == "safety_violation"
        backend.client.move.assert_not_called()
