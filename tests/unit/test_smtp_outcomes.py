"""SMTP submission uncertainty must not become permission to send again."""

import smtplib
from unittest.mock import patch

import pytest

from apple_mail_fast_mcp.smtp_sender import SmtpSender

RAW = b"From: sender@example.test\r\nMessage-ID: <candidate@example.test>\r\n\r\nbody"


@pytest.fixture(params=[465, 587])
def transport(request):
    port = request.param
    name = "SMTP_SSL" if port == 465 else "SMTP"
    with patch(f"apple_mail_fast_mcp.smtp_sender.smtplib.{name}") as factory:
        client = factory.return_value
        # Also exercise the old implementation during the RED step.
        client.__enter__.return_value = client
        client.send_message.return_value = {}
        yield SmtpSender("smtp.example.test", port, "login@example.test", "fake"), client


@pytest.mark.parametrize("failure", [OSError("lost DATA response"), smtplib.SMTPServerDisconnected("lost response")])
def test_ambiguous_submission_is_not_a_fallback_exception(transport, failure):
    sender, client = transport
    client.send_message.side_effect = failure
    client.quit.side_effect = OSError("cleanup failed")
    client.close.side_effect = OSError("close failed")
    with pytest.raises(Exception) as caught:
        sender.send(RAW, ["recipient@example.test"])
    error = caught.value
    assert type(error).__name__ == "MailSmtpSendOutcomeUnknownError"
    assert not isinstance(error, (smtplib.SMTPException, OSError))
    assert error.candidate_message_id == "candidate@example.test"
    assert error.requested == 1
    assert error.__cause__ is failure
    client.send_message.assert_called_once()
    client.close.assert_called_once()


@pytest.mark.parametrize("failure", [
    smtplib.SMTPSenderRefused(550, b"sender rejected", "sender@example.test"),
    smtplib.SMTPRecipientsRefused({"recipient@example.test": (550, b"no mailbox")}),
    smtplib.SMTPDataError(554, b"message rejected"),
])
def test_explicit_rejection_is_not_replayed_or_masked_by_cleanup(transport, failure):
    sender, client = transport
    client.send_message.side_effect = failure
    client.quit.side_effect = smtplib.SMTPResponseException(421, b"closing")
    with pytest.raises(Exception) as caught:
        sender.send(RAW, ["recipient@example.test"])
    assert type(caught.value).__name__ == "MailSmtpSendRejectedError"
    assert not isinstance(caught.value, (smtplib.SMTPException, OSError))
    assert caught.value.__cause__ is failure
    client.close.assert_called_once()


def test_auth_failure_remains_predispatch_and_cleanup_cannot_replace_it(transport):
    sender, client = transport
    failure = smtplib.SMTPAuthenticationError(535, b"auth rejected")
    client.login.side_effect = failure
    client.quit.side_effect = OSError("cleanup failed")
    with pytest.raises(smtplib.SMTPAuthenticationError) as caught:
        sender.send(RAW, ["recipient@example.test"])
    assert caught.value is failure
    client.send_message.assert_not_called()
    client.close.assert_called_once()


@pytest.mark.parametrize("refused", [{}, {"rejected@example.test": (550, b"no mailbox")}])
def test_acknowledged_result_survives_all_cleanup_failures(transport, refused):
    sender, client = transport
    client.send_message.return_value = refused
    client.quit.side_effect = OSError("cleanup failed")
    client.close.side_effect = OSError("close failed")
    assert sender.send(RAW, ["accepted@example.test", "rejected@example.test"]) == refused
    client.send_message.assert_called_once()
    client.close.assert_called_once()


def test_post_accept_debug_logging_cannot_make_send_retryable(transport):
    sender, client = transport
    with patch("apple_mail_fast_mcp.smtp_sender.logger.debug", side_effect=OSError("log failed")):
        assert sender.send(RAW, ["recipient@example.test"]) == {}
    client.send_message.assert_called_once()


@pytest.fixture
def connector(monkeypatch):
    from unittest.mock import MagicMock

    from apple_mail_fast_mcp.mail_connector import AppleMailConnector
    connector = AppleMailConnector()
    monkeypatch.setattr(connector, "list_accounts", lambda: [{
        "id": "11111111-1111-1111-1111-111111111111", "name": "Fixture",
        "email_addresses": ["sender@example.test"], "enabled": True,
    }])
    monkeypatch.setattr(connector, "_resolve_smtp_config", lambda account: ("smtp.example.test", 587, "sender@example.test"))
    monkeypatch.setattr(connector, "_resolve_imap_config", lambda account: ("imap.example.test", 993, "sender@example.test"))
    monkeypatch.setattr(connector, "_get_imap_password_with_fallback", lambda *a: "fake")
    monkeypatch.setattr(connector, "_build_reply_forward_mime", lambda **k: ("candidate@example.test", RAW, ["recipient@example.test"]))
    monkeypatch.setattr(connector, "_run_applescript", MagicMock(return_value="SENT"))
    monkeypatch.setattr(connector, "_save_sent_copy", MagicMock())
    monkeypatch.setattr(connector, "delete_draft", MagicMock(return_value=True))
    monkeypatch.setattr(connector, "get_draft_state", lambda _: {
        "to": ["recipient@example.test"], "subject": "fixture", "body": "body",
        "content_type": "text/plain", "from_account": "Fixture", "sender_email": "sender@example.test",
        "attachment_names": [], "in_reply_to": "",
    })
    return connector


@pytest.mark.parametrize("seed", ["new", "reply", "forward"])
@pytest.mark.parametrize("failure", [OSError("lost acknowledgment"), smtplib.SMTPDataError(554, b"private rejection detail")])
def test_create_send_never_replays_dispatched_failure(connector, seed, failure):
    with patch("apple_mail_fast_mcp.smtp_sender.smtplib.SMTP") as smtp:
        smtp.return_value.send_message.side_effect = failure
        with pytest.raises(Exception) as caught:
            connector.create_draft(seed=seed, seed_id=None if seed == "new" else "seed@example.test",
                to=["recipient@example.test"], subject="fixture", from_account="Fixture", send_now=True)
        assert type(caught.value).__name__ in {"MailSmtpSendOutcomeUnknownError", "MailSmtpSendRejectedError"}
        smtp.return_value.send_message.assert_called_once()
    connector._run_applescript.assert_not_called()
    connector._save_sent_copy.assert_not_called()


@pytest.mark.parametrize("tool", ["create", "update"])
@pytest.mark.parametrize("failure,expected", [
    (OSError("lost acknowledgment"), "mutation_outcome_unknown"),
    (smtplib.SMTPSenderRefused(550, b"private rejection detail", "sender@example.test"), "smtp_rejected"),
    (smtplib.SMTPRecipientsRefused({"recipient@example.test": (550, b"private rejection detail")}), "smtp_rejected"),
    (smtplib.SMTPDataError(554, b"private rejection detail"), "smtp_rejected"),
])
async def test_mcp_send_outcomes_are_typed_and_original_retained(connector, monkeypatch, tmp_path, tool, failure, expected):
    from unittest.mock import AsyncMock

    from apple_mail_fast_mcp import server
    monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
    monkeypatch.setenv("MAIL_TEST_MODE", "false")
    monkeypatch.setattr(server, "mail", connector)
    monkeypatch.setattr(server, "check_rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_run_send_now_gates", AsyncMock(return_value=None))
    with patch("apple_mail_fast_mcp.smtp_sender.smtplib.SMTP") as smtp:
        smtp.return_value.send_message.side_effect = failure
        if tool == "create":
            result = await server.create_draft(to=["recipient@example.test"], subject="fixture", from_account="Fixture", send_now=True)
        else:
            result = await server.update_draft("old-41", body="replacement", send_now=True)
            assert result["original_draft_id"] == "old-41"
        smtp.return_value.send_message.assert_called_once()
    assert result["success"] is False
    assert result["error_type"] == expected
    assert result["operation"] == "smtp_send"
    assert result["retry_safe"] is False
    assert result["requested"] == 1
    assert "private rejection detail" not in str(result)
    if expected == "mutation_outcome_unknown":
        assert result["candidate_message_id"]
        assert result["partial"] is True
    connector._run_applescript.assert_not_called()
    connector.delete_draft.assert_not_called()


@pytest.mark.parametrize("provider", ["gmail", "custom"])
def test_sent_copy_logging_failure_cannot_replay_accepted_send(connector, monkeypatch, provider):
    from apple_mail_fast_mcp.mail_connector import AppleMailConnector
    monkeypatch.setattr(connector, "_save_sent_copy", AppleMailConnector._save_sent_copy.__get__(connector))
    if provider == "gmail":
        monkeypatch.setattr(connector, "_resolve_smtp_config", lambda account: ("smtp.gmail.com", 587, "sender@example.test"))
    with patch("apple_mail_fast_mcp.smtp_sender.smtplib.SMTP") as smtp, \
         patch("apple_mail_fast_mcp.mail_connector.ImapConnector") as imap, \
         patch("apple_mail_fast_mcp.mail_connector.logger.debug", side_effect=OSError("log offline")), \
         patch("apple_mail_fast_mcp.mail_connector.logger.warning", side_effect=OSError("log offline")):
        smtp.return_value.send_message.return_value = {}
        imap.return_value.append_sent_copy.side_effect = OSError("copy unavailable")
        result = connector.create_draft(to=["recipient@example.test"], subject="fixture", from_account="Fixture", send_now=True)
    assert result["delivery"]["accepted_recipients"] == ["recipient@example.test"]
    connector._run_applescript.assert_not_called()
    smtp.return_value.send_message.assert_called_once()
    if provider == "gmail":
        imap.assert_not_called()


@pytest.mark.parametrize("tool", ["create", "update"])
async def test_postaccept_audit_logging_cannot_turn_success_into_failure(connector, monkeypatch, tmp_path, tool):
    from unittest.mock import AsyncMock, MagicMock

    from apple_mail_fast_mcp import server
    monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
    monkeypatch.setenv("MAIL_TEST_MODE", "false")
    monkeypatch.setattr(server, "mail", connector)
    monkeypatch.setattr(server, "check_rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_run_send_now_gates", AsyncMock(return_value=None))
    monkeypatch.setattr(server.operation_logger, "log_operation", MagicMock(side_effect=OSError("audit unavailable")))
    with patch("apple_mail_fast_mcp.smtp_sender.smtplib.SMTP") as smtp:
        smtp.return_value.send_message.return_value = {}
        if tool == "create":
            result = await server.create_draft(to=["recipient@example.test"], subject="fixture", from_account="Fixture", send_now=True)
        else:
            result = await server.update_draft("old-41", body="replacement", send_now=True)
        smtp.return_value.send_message.assert_called_once()
    assert result["success"] is True
    assert result["delivery"]["accepted_recipients"] == ["recipient@example.test"]
    assert result["audit_log_failed"] is True
    connector._run_applescript.assert_not_called()


def test_postaccept_breaker_failure_cannot_trigger_native_replay(connector, monkeypatch):
    from unittest.mock import MagicMock
    monkeypatch.setattr(connector, "_imap_clear_breaker", MagicMock(side_effect=OSError("bookkeeping unavailable")))
    with patch("apple_mail_fast_mcp.smtp_sender.smtplib.SMTP") as smtp:
        smtp.return_value.send_message.return_value = {}
        result = connector.create_draft(to=["recipient@example.test"], subject="fixture", from_account="Fixture", send_now=True)
    assert result["delivery"]["accepted_recipients"] == ["recipient@example.test"]
    connector._run_applescript.assert_not_called()
    smtp.return_value.send_message.assert_called_once()


async def test_partial_submission_keeps_original_even_when_audit_fails(connector, monkeypatch, tmp_path):
    from unittest.mock import AsyncMock, MagicMock

    from apple_mail_fast_mcp import server
    monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
    monkeypatch.setenv("MAIL_TEST_MODE", "false")
    monkeypatch.setattr(server, "mail", connector)
    monkeypatch.setattr(server, "check_rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_run_send_now_gates", AsyncMock(return_value=None))
    monkeypatch.setattr(server.operation_logger, "log_operation", MagicMock(side_effect=OSError("audit unavailable")))
    with patch("apple_mail_fast_mcp.smtp_sender.smtplib.SMTP") as smtp:
        smtp.return_value.send_message.return_value = {"refused@example.test": (550, b"mailbox unavailable")}
        result = await server.update_draft("old-41", to=["recipient@example.test", "refused@example.test"],
                                          body="replacement", send_now=True)
    assert result["success"] is True and result["partial"] is True
    assert result["original_draft_id"] == "old-41"
    assert result["audit_log_failed"] is True
    assert result["delivery"]["accepted_recipients"] == ["recipient@example.test"]
    assert result["delivery"]["refused_recipients"]["refused@example.test"] == {"code": 550}
    assert len(result["warnings"]) == 2
    connector.delete_draft.assert_not_called()
    connector._run_applescript.assert_not_called()
    smtp.return_value.send_message.assert_called_once()


def test_transport_cleanup_logging_preserves_rejection(transport):
    sender, client = transport
    failure = smtplib.SMTPDataError(554, b"rejected")
    client.send_message.side_effect = failure
    client.quit.side_effect = OSError("cleanup failed")
    with patch("apple_mail_fast_mcp.smtp_sender.logger.warning", side_effect=OSError("log failed")):
        with pytest.raises(Exception) as caught:
            sender.send(RAW, ["recipient@example.test"])
    assert type(caught.value).__name__ == "MailSmtpSendRejectedError"
    assert caught.value.__cause__ is failure
    client.close.assert_called_once()
