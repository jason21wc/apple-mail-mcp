"""Attachment selection and raw read failures remain bounded through MCP."""
from email.message import EmailMessage
from unittest.mock import MagicMock, patch

import pytest

from apple_mail_fast_mcp import draft_builder, server
from apple_mail_fast_mcp.mail_connector import AppleMailConnector
from apple_mail_fast_mcp.raw_message import MailRawMessageLimitError, MailRawMessageReadError


@pytest.fixture
def raw():
    message = EmailMessage()
    message.set_content("body")
    for name, payload in [("same.txt", b"aaaaa"), ("unused.bin", b"unused"), ("same.txt", b"ccc")]:
        message.add_attachment(payload, maintype="application", subtype="octet-stream", filename=name)
    return message.as_bytes()


@pytest.fixture
def connector(monkeypatch, raw):
    connector = AppleMailConnector()
    monkeypatch.setattr(connector, "list_accounts", lambda: [{
        "id": "11111111-1111-1111-1111-111111111111", "name": "Fixture",
        "email_addresses": ["fixture@example.test"], "enabled": True,
    }])
    monkeypatch.setattr(connector, "_resolve_imap_config", lambda account: ("imap.example.test", 993, "fixture@example.test"))
    monkeypatch.setattr(connector, "_get_imap_password_with_fallback", lambda *a: "fake")
    monkeypatch.setattr(connector, "_run_applescript", MagicMock())
    with patch("apple_mail_fast_mcp.mail_connector.ImapConnector") as imap:
        imap.return_value.fetch_raw_message.return_value = raw
        imap.return_value.fetch_raw_draft.return_value = raw
        yield connector, imap.return_value


def test_inline_decodes_only_requested_original_index(connector):
    connector, _ = connector
    with patch.object(draft_builder, "_attachment_payload_bytes", wraps=draft_builder._attachment_payload_bytes) as decode:
        result = connector.get_attachment_content("fixture@example.test", 2, account="Fixture", mailbox="INBOX")
    assert result["payload"] == b"ccc"
    assert decode.call_count == 1
    assert decode.call_args.args[0].get_filename() == "same.txt"
    connector._run_applescript.assert_not_called()


def test_save_decodes_selected_parts_once_preserving_index_collision_names(connector, tmp_path):
    connector, _ = connector
    with patch.object(draft_builder, "_attachment_payload_bytes", wraps=draft_builder._attachment_payload_bytes) as decode:
        result = connector.save_attachments("fixture@example.test", tmp_path, [2, 0], account="Fixture", mailbox="INBOX")
    assert result == {"saved": 2, "rejected": []}
    assert (tmp_path / "same.txt").read_bytes() == b"ccc"
    assert (tmp_path / "0_same.txt").read_bytes() == b"aaaaa"
    assert decode.call_count == 2
    assert not (tmp_path / "unused.bin").exists()


def test_save_selection_order_controls_aggregate_cap(connector, tmp_path):
    connector, _ = connector
    connector.max_attachment_bytes = 10
    connector.max_total_attachment_bytes = 6
    result = connector.save_attachments("fixture@example.test", tmp_path, [2, 0], account="Fixture", mailbox="INBOX")
    assert result == {"saved": 1, "rejected": [{"name": "same.txt", "size": 5, "reason": "aggregate_cap"}]}
    assert (tmp_path / "same.txt").read_bytes() == b"ccc"


@pytest.mark.parametrize("indices", [[], [-1, 20]])
def test_empty_save_selection_decodes_nothing(connector, tmp_path, indices):
    connector, _ = connector
    with patch.object(draft_builder, "_attachment_payload_bytes") as decode:
        assert connector.save_attachments("fixture@example.test", tmp_path, indices, account="Fixture", mailbox="INBOX") == {"saved": 0, "rejected": []}
    decode.assert_not_called()


@pytest.mark.parametrize("failure", [MailRawMessageLimitError("budget exceeded"), MailRawMessageReadError("incomplete transfer")])
@pytest.mark.parametrize("operation", ["inline", "save", "draft_preserve"])
def test_raw_failure_never_reaches_native_or_decode(connector, tmp_path, failure, operation):
    connector, imap = connector
    imap.fetch_raw_message.side_effect = failure
    imap.fetch_raw_draft.side_effect = failure
    with patch.object(draft_builder, "_attachment_payload_bytes") as decode:
        with pytest.raises(type(failure)) as caught:
            if operation == "inline":
                connector.get_attachment_content("fixture@example.test", 0, account="Fixture", mailbox="INBOX")
            elif operation == "save":
                connector.save_attachments("fixture@example.test", tmp_path, [0], account="Fixture", mailbox="INBOX")
            else:
                connector.extract_draft_attachments("fixture@example.test", ["same.txt"], tmp_path, account="Fixture")
    assert caught.value is failure
    connector._run_applescript.assert_not_called()
    decode.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("error_type,expected", [(MailRawMessageLimitError, "raw_message_too_large"),
                                               (MailRawMessageReadError, "message_read_incomplete")])
@pytest.mark.parametrize("tool", ["messages", "inline", "save", "create", "update"])
async def test_mcp_retains_typed_raw_failures(monkeypatch, tmp_path, error_type, expected, tool):
    error = error_type("bounded retrieval refused")
    backend = MagicMock(spec=AppleMailConnector)
    backend.get_message.side_effect = error
    backend.get_attachment_content.side_effect = error
    backend.save_attachments.side_effect = error
    backend.create_draft.side_effect = error
    backend.get_draft_state.return_value = {"to": ["recipient@example.test"], "subject": "fixture",
        "body": "body", "content_type": "text/plain", "from_account": "Fixture", "attachment_names": ["same.txt"]}
    backend.extract_draft_attachments.side_effect = error
    monkeypatch.setattr(server, "mail", backend)
    monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MAIL_TEST_MODE", "false")
    monkeypatch.setattr(server, "check_rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_validate_sender_selection", lambda *a, **k: None)
    if tool == "messages":
        result = server.get_messages(["fixture@example.test"], account="Fixture", mailbox="INBOX")
    elif tool == "inline":
        result = server.get_attachment_content("fixture@example.test", 0, account="Fixture", mailbox="INBOX")
    elif tool == "save":
        result = server.save_attachments("fixture@example.test", str(tmp_path), [0], account="Fixture", mailbox="INBOX")
    elif tool == "create":
        result = await server.create_draft(to=["recipient@example.test"], subject="fixture", from_account="Fixture")
    else:
        result = await server.update_draft("old-41", body="replacement")
        assert result["original_draft_id"] == "old-41"
        backend.create_draft.assert_not_called()
        backend.delete_draft.assert_not_called()
    assert result["success"] is False
    assert result["error_type"] == expected
    assert "saved" not in result and "not_found" not in result


def test_original_raw_error_survives_temporary_cleanup_failure(monkeypatch, tmp_path):
    error = MailRawMessageLimitError("budget exceeded")
    backend = MagicMock()
    backend.extract_draft_attachments.side_effect = error
    monkeypatch.setattr(server, "mail", backend)
    with patch.object(server.tempfile, "TemporaryDirectory") as temporary:
        temporary.return_value.name = str(tmp_path)
        temporary.return_value.cleanup.side_effect = OSError("cleanup failed")
        with pytest.raises(MailRawMessageLimitError) as caught:
            server._resolve_draft_attachments("old-41", None, ["same.txt"], account="Fixture")
    assert caught.value is error


@pytest.mark.parametrize("failure,expected", [
    (MailRawMessageLimitError("budget exceeded"), "raw_message_too_large"),
    (MailRawMessageReadError("incomplete"), "message_read_incomplete"),
])
def test_named_save_read_failure_survives_cleanup(monkeypatch, tmp_path, failure, expected):
    backend = MagicMock()
    backend.save_attachments.side_effect = failure
    monkeypatch.setattr(server, "mail", backend)
    monkeypatch.setattr(server, "check_rate_limit", lambda *a, **k: None)
    with patch.object(server.tempfile, "TemporaryDirectory") as temporary:
        temporary.return_value.name = str(tmp_path)
        temporary.return_value.cleanup.side_effect = OSError("cleanup failed")
        result = server.save_attachments("fixture@example.test", str(tmp_path), [0],
                                         output_filename="report.bin")
    assert result["success"] is False and result["error_type"] == expected
    assert not (tmp_path / "report.bin").exists()


def test_named_save_success_survives_staging_cleanup_failure(monkeypatch, tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "original.bin").write_bytes(b"verified fixture")
    backend = MagicMock()
    backend.save_attachments.return_value = {"saved": 1, "rejected": []}
    monkeypatch.setattr(server, "mail", backend)
    monkeypatch.setattr(server, "check_rate_limit", lambda *a, **k: None)
    with patch.object(server.tempfile, "TemporaryDirectory") as temporary:
        temporary.return_value.name = str(staging)
        temporary.return_value.cleanup.side_effect = OSError("cleanup failed")
        result = server.save_attachments("fixture@example.test", str(tmp_path), [0],
                                         output_filename="report.bin")
    assert result["success"] is True and result["saved"] == 1
    assert (tmp_path / "report.bin").read_bytes() == b"verified fixture"


def test_named_save_keeps_no_clobber_and_selected_payload(connector, monkeypatch, tmp_path):
    connector, _ = connector
    monkeypatch.setattr(server, "mail", connector)
    monkeypatch.setattr(server, "check_rate_limit", lambda *a, **k: None)
    original = tmp_path / "report.bin"
    original.write_bytes(b"existing")
    result = server.save_attachments("fixture@example.test", str(tmp_path), [2],
                                     output_filename="report.bin", account="Fixture", mailbox="INBOX")
    assert result["error_type"] == "already_exists"
    assert original.read_bytes() == b"existing"
    assert list(tmp_path.iterdir()) == [original]


def test_predispatch_transport_failure_keeps_native_fallback(connector, monkeypatch):
    connector, imap = connector
    imap.fetch_raw_message.side_effect = OSError("connection failed before retrieval")
    native = MagicMock(return_value={"payload": b"fixture"})
    monkeypatch.setattr(connector, "_get_attachment_content_applescript", native)
    assert connector.get_attachment_content("fixture@example.test", 0, account="Fixture", mailbox="INBOX") == {"payload": b"fixture"}
    native.assert_called_once()


@pytest.mark.parametrize("seed", ["reply", "forward"])
@pytest.mark.parametrize("send_now", [False, True])
def test_reply_forward_raw_limit_cannot_create_or_send_native(connector, monkeypatch, seed, send_now):
    connector, imap = connector
    error = MailRawMessageLimitError("budget exceeded")
    imap.fetch_raw_message.side_effect = error
    monkeypatch.setattr(connector, "_resolve_smtp_config", lambda account: ("smtp.example.test", 587, "fixture@example.test"))
    with patch("apple_mail_fast_mcp.mail_connector.SmtpSender") as smtp:
        with pytest.raises(MailRawMessageLimitError) as caught:
            connector.create_draft(seed=seed, seed_id="seed@example.test", to=["recipient@example.test"],
                                   subject="fixture", body="body", from_account="Fixture", send_now=send_now)
        smtp.assert_not_called()
    assert caught.value is error
    imap.append_draft.assert_not_called()
    connector._run_applescript.assert_not_called()
