"""Sender selection at the MCP boundary; all Mail and network I/O mocked."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from apple_mail_fast_mcp import server
from apple_mail_fast_mcp.mail_connector import AppleMailConnector


@pytest.fixture
def alias_mail(monkeypatch, tmp_path):
    monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
    connector = AppleMailConnector()
    monkeypatch.setattr(connector, "list_accounts", lambda: [
        {"id": "id-cloud", "name": "Cloud", "full_name": "Example User",
         "email_addresses": ["primary@example.com", "alias@example.com"]},
        {"id": "id-work", "name": "Work", "email_addresses": ["work@example.net"]},
    ])
    monkeypatch.setattr(connector, "get_draft_state", MagicMock(return_value={
        "from_account": "Cloud", "sender_email": "alias@example.com",
        "content_type": "text/plain", "subject": "Fixture", "body": "Original",
        "to": ["recipient@example.org"], "attachment_names": [], "in_reply_to": "",
    }))
    monkeypatch.setattr(connector, "create_draft", MagicMock(return_value={
        "draft_id": "replacement@example.com", "from_account": "Cloud",
        "sender_email": "alias@example.com",
    }))
    monkeypatch.setattr(connector, "delete_draft", MagicMock(return_value=True))
    monkeypatch.setattr(server, "mail", connector)
    return connector


async def test_create_alias_forwarding_and_result(alias_mail):
    result = await server.create_draft(
        to=["recipient@example.org"], subject="Fixture", from_account="Cloud",
        sender_email="alias@example.com",
    )
    assert result["success"]
    assert result["details"]["sender_email"] == "alias@example.com"
    assert alias_mail.create_draft.call_args.kwargs["sender_email"] == "alias@example.com"


@pytest.mark.parametrize("account,alias", [
    (None, "alias@example.com"), ("Cloud", "outsider@example.org"),
    ("Cloud", "alias@example.com\r\nBcc: outsider@example.org"), ("Cloud", ""),
])
async def test_invalid_sender_rejected_before_confirmation(alias_mail, monkeypatch, account, alias):
    gate = AsyncMock(return_value=None)
    monkeypatch.setattr(server, "_run_send_now_gates", gate)
    result = await server.create_draft(
        to=["recipient@example.org"], subject="Fixture", from_account=account,
        sender_email=alias, send_now=True,
    )
    assert result["error_type"] == "validation_error"
    gate.assert_not_awaited()
    alias_mail.create_draft.assert_not_called()


@pytest.mark.parametrize("account,expected", [
    (None, "alias@example.com"), ("Cloud", "alias@example.com"),
    ("id-cloud", "alias@example.com"), ("Work", None),
])
async def test_update_preserves_alias_unless_account_changes(alias_mail, account, expected):
    result = await server.update_draft("original@example.com", subject="Updated", from_account=account)
    assert result["success"]
    assert alias_mail.create_draft.call_args.kwargs["sender_email"] == expected
    assert result["details"]["sender_email"] == "alias@example.com"
    alias_mail.delete_draft.assert_called_once_with("original@example.com")


async def test_update_sender_override_is_validated_before_mutation(alias_mail, monkeypatch):
    gate = AsyncMock(return_value=None)
    monkeypatch.setattr(server, "_run_send_now_gates", gate)
    result = await server.update_draft(
        "original@example.com", sender_email="outsider@example.org", send_now=True,
    )
    assert result["error_type"] == "validation_error"
    gate.assert_not_awaited()
    alias_mail.create_draft.assert_not_called()
    alias_mail.delete_draft.assert_not_called()


@pytest.mark.parametrize("operation", ["create", "update"])
async def test_declined_send_shows_alias_and_does_not_mutate(alias_mail, monkeypatch, operation):
    gate = AsyncMock(return_value={"success": False, "error_type": "cancelled"})
    monkeypatch.setattr(server, "_run_send_now_gates", gate)
    if operation == "create":
        result = await server.create_draft(
            to=["recipient@example.org"], subject="Fixture", from_account="Cloud",
            sender_email="alias@example.com", send_now=True,
        )
    else:
        result = await server.update_draft("original@example.com", send_now=True)
    assert result["error_type"] == "cancelled"
    assert "From: Example User <alias@example.com>" in gate.call_args.kwargs["summary"]
    alias_mail.create_draft.assert_not_called()
    alias_mail.delete_draft.assert_not_called()


async def test_unknown_original_sender_retains_draft_before_confirmation(alias_mail, monkeypatch):
    alias_mail.get_draft_state.return_value.update(from_account="", sender_email="")
    gate = AsyncMock(return_value=None)
    monkeypatch.setattr(server, "_run_send_now_gates", gate)
    result = await server.update_draft("original@example.com", send_now=True)
    assert result["error_type"] == "preservation_unavailable"
    gate.assert_not_awaited()
    alias_mail.create_draft.assert_not_called()
    alias_mail.delete_draft.assert_not_called()
