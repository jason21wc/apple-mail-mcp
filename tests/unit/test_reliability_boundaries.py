"""Regression tests for mutation boundaries; no live Mail or network I/O."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apple_mail_fast_mcp import security, server
from apple_mail_fast_mcp.drafts import DraftStateStore, SeedRecord
from apple_mail_fast_mcp.exceptions import MailDraftError
from apple_mail_fast_mcp.mail_connector import AppleMailConnector


@pytest.fixture
def draft_mail(monkeypatch, tmp_path):
    monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
    client = MagicMock()
    client.get_draft_state.return_value = {
        "to": ["a@example.com"], "cc": [], "bcc": [], "body": "original",
        "subject": "original", "attachment_names": [], "from_account": "TestAccount",
        "content_type": "text/plain", "in_reply_to": "",
    }
    client.create_draft.return_value = {"draft_id": "102"}
    client.delete_draft.return_value = True
    monkeypatch.setattr(server, "mail", client)
    monkeypatch.setattr(server, "check_rate_limit", lambda *a, **kw: None)
    return client


@pytest.mark.parametrize("change", [
    {"read_status": True}, {"flag_color": "orange"},
    {"destination_mailbox": "Archive"},
    {"destination_mailbox": "Archive", "read_status": True},
])
@pytest.mark.parametrize("source", [None, "", "  "])
async def test_test_mode_rejects_unconfined_dispatch(monkeypatch, change, source):
    monkeypatch.setenv("MAIL_TEST_MODE", "true")
    monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")
    monkeypatch.setattr(security, "_get_test_account_identifiers", lambda account: {account})
    connector = AppleMailConnector()
    monkeypatch.setattr(server, "mail", connector)
    with patch.object(connector, "_run_applescript") as transport:
        result = await server.update_message(
            ["101"], account="TestAccount", source_mailbox=source, **change
        )
    assert result["error_type"] == "safety_violation"
    transport.assert_not_called()


@pytest.mark.parametrize("change", [{"read_status": True}, {"flag_color": "orange"},
                                     {"destination_mailbox": "Archive", "read_status": True}])
async def test_scoped_fallback_script_stays_in_source(monkeypatch, change):
    monkeypatch.setenv("MAIL_TEST_MODE", "true")
    monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")
    monkeypatch.setattr(security, "_get_test_account_identifiers", lambda account: {account})
    connector = AppleMailConnector()
    monkeypatch.setattr(server, "mail", connector)
    monkeypatch.setattr(server, "check_rate_limit", lambda *a, **kw: None)
    with patch.object(connector, "_try_imap_fast_paths", return_value=None), \
         patch.object(connector, "_run_applescript", return_value="1") as transport:
        result = await server.update_message(
            ["101"], account="TestAccount", source_mailbox="INBOX", **change
        )
    assert result["success"]
    script = transport.call_args.args[0]
    assert 'account "TestAccount"' in script and '"INBOX"' in script
    assert 'repeat with acct in accounts' not in script


@pytest.mark.parametrize("enabled,confirm", [(False, True), (True, False), (None, True)])
async def test_rule_activation_requires_confirmation(draft_mail, monkeypatch, enabled, confirm):
    draft_mail.list_rules.return_value = [{"index": 1, "name": "Forward", "enabled": enabled}]
    gate = AsyncMock(return_value=None)
    monkeypatch.setattr(server, "_elicit_confirmation", gate)
    monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
    result = await server.update_rule(1, enabled=True)
    assert result["success"]
    assert gate.await_count == int(confirm)


async def test_rule_activation_cancellation_preserves_rule(draft_mail, monkeypatch):
    draft_mail.list_rules.return_value = [{"index": 1, "name": "Forward", "enabled": False}]
    monkeypatch.setattr(server, "_elicit_confirmation", AsyncMock(return_value={"success": False}))
    monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
    assert not (await server.update_rule(1, enabled=True))["success"]
    draft_mail.update_rule.assert_not_called()


async def test_create_failure_preserves_draft_and_seed(draft_mail):
    store = DraftStateStore()
    store.set_seed("101", SeedRecord("reply", "100", False))
    draft_mail.create_draft.side_effect = MailDraftError("cannot create")
    result = await server.update_draft("101", subject="new")
    assert not result["success"]
    draft_mail.delete_draft.assert_not_called()
    assert store.get_seed("101") is not None


async def test_replacement_created_before_cleanup_and_sender_preserved(draft_mail):
    result = await server.update_draft("101", subject="new")
    assert result["success"]
    calls = [call[0] for call in draft_mail.mock_calls]
    assert calls.index("create_draft") < calls.index("delete_draft")
    assert draft_mail.create_draft.call_args.kwargs["from_account"] == "TestAccount"
    assert draft_mail.create_draft.call_args.kwargs["require_stable_id"] is True


async def test_rejected_imap_login_never_creates_fallback_or_deletes_original(
    draft_mail, monkeypatch,
):
    from imapclient.exceptions import LoginError

    connector = AppleMailConnector()
    monkeypatch.setattr(server, "mail", connector)
    monkeypatch.setattr(connector, "get_draft_state", draft_mail.get_draft_state)
    store = DraftStateStore()
    with patch.object(connector, "_create_draft_via_imap",
                      side_effect=LoginError("rejected")) as append, \
         patch.object(connector, "_run_applescript") as transport, \
         patch.object(connector, "delete_draft") as delete:
        result = await server.update_draft("101", body="replacement")
    assert result["error_type"] == "draft_error"
    assert "Original draft retained" in result["error"]
    append.assert_called_once()
    transport.assert_not_called()
    delete.assert_not_called()
    assert not store.root.exists()


@pytest.mark.parametrize("seed,seed_id", [("new", None), ("reply", "seed@example.com"),
                                         ("forward", "seed@example.com")])
def test_stable_save_refuses_unavailable_clean_path(seed, seed_id):
    connector = AppleMailConnector()
    with patch.object(connector, "_try_clean_create_or_send", return_value=None), \
         patch.object(connector, "_run_applescript") as transport:
        with pytest.raises(MailDraftError, match="requires working IMAP"):
            connector.create_draft(
                seed=seed, seed_id=seed_id, to=["a@example.com"], subject="test",
                from_account="TestAccount", require_stable_id=True,
            )
    transport.assert_not_called()


def test_stable_save_accepts_imap_identity():
    connector = AppleMailConnector()
    expected = {"draft_id": "generated@example.com"}
    with patch.object(connector, "_try_clean_create_or_send", return_value=expected), \
         patch.object(connector, "_run_applescript") as transport:
        result = connector.create_draft(
            to=["a@example.com"], subject="test", from_account="TestAccount",
            require_stable_id=True,
        )
    assert result == expected
    transport.assert_not_called()


async def test_cleanup_failure_reports_both_ids(draft_mail):
    draft_mail.delete_draft.side_effect = OSError("busy")
    result = await server.update_draft("101", subject="new")
    assert result["success"] and result["partial"]
    assert result["draft_id"] == "102" and result["original_draft_id"] == "101"


async def test_partial_attachment_extraction_preserves_original(draft_mail, tmp_path):
    draft_mail.get_draft_state.return_value["attachment_names"] = ["one.txt", "two.txt"]
    attachment = tmp_path / "one.txt"
    attachment.write_text("one")
    draft_mail.extract_draft_attachments.return_value = [attachment]
    result = await server.update_draft("101", subject="new")
    assert result["error_type"] == "draft_error"
    draft_mail.create_draft.assert_not_called()
    draft_mail.delete_draft.assert_not_called()


@pytest.mark.parametrize("field,value", [("content_type", "text/html"),
                                         ("content_type", ""), ("from_account", "")])
async def test_unsupported_preservation_is_explicit(draft_mail, field, value):
    draft_mail.get_draft_state.return_value[field] = value
    result = await server.update_draft("101", subject="new")
    assert result["error_type"] == "preservation_unavailable"
    draft_mail.create_draft.assert_not_called()
    draft_mail.delete_draft.assert_not_called()


async def test_send_partial_result_survives_cleanup_failure(draft_mail, monkeypatch):
    delivery = {"accepted_recipients": ["a@example.com"],
                "refused_recipients": {"b@example.com": {"code": 550}}, "partial": True}
    draft_mail.create_draft.return_value = {"draft_id": "", "delivery": delivery}
    draft_mail.delete_draft.side_effect = OSError("busy")
    monkeypatch.setattr(server, "_run_send_now_gates", AsyncMock(return_value=None))
    result = await server.update_draft("101", send_now=True)
    assert result["success"] and result["delivery"] == delivery
    draft_mail.create_draft.assert_called_once()
    assert draft_mail.create_draft.call_args.kwargs["require_stable_id"] is False


@pytest.mark.parametrize("sender,expected", [("Test <test@example.com>", "TestAccount"),
                                            ("alias@example.com", ""), ("", "")])
def test_draft_state_refuses_unpreservable_alias(sender, expected):
    import json

    connector = AppleMailConnector()
    with patch.object(connector, "_run_applescript", return_value=json.dumps({
        "found": True, "draft_id": "101", "from_account": "TestAccount", "sender": sender,
        "content_type": "text/plain",
    })), patch.object(connector, "_resolve_account_to_sender", return_value="test@example.com"):
        state = connector.get_draft_state("101")
    assert state["from_account"] == expected


async def test_partial_send_keeps_original_without_cleanup(draft_mail, monkeypatch):
    delivery = {"partial": True, "accepted_recipients": ["a@example.com"],
                "refused_recipients": {"b@example.com": {"code": 550}}}
    draft_mail.create_draft.return_value = {"draft_id": "", "delivery": delivery}
    monkeypatch.setattr(server, "_run_send_now_gates", AsyncMock(return_value=None))
    result = await server.update_draft("101", send_now=True)
    assert result["partial"] and result["original_draft_id"] == "101"
    draft_mail.delete_draft.assert_not_called()
