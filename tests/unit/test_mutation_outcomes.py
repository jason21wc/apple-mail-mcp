"""No-replay boundaries for mutations whose server acknowledgment is lost."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from imapclient.exceptions import IMAPClientError

from apple_mail_fast_mcp.exceptions import MailMutationOutcomeUnknownError
from apple_mail_fast_mcp.imap_connector import ImapConnectionPool, ImapConnector
from apple_mail_fast_mcp.mail_connector import AppleMailConnector


@pytest.fixture
def client():
    with patch("apple_mail_fast_mcp.imap_connector.IMAPClient") as factory:
        backend = factory.return_value
        backend.capabilities.return_value = [b"MOVE", b"UIDPLUS"]
        backend.search.return_value = [7, 8]
        backend.list_folders.return_value = [
            ([b"\\Trash"], b"/", "Trash"), ([b"\\Drafts"], b"/", "Drafts"),
            ([b"\\Sent"], b"/", "Sent"),
        ]
        yield backend


@pytest.fixture
def account_snapshot(monkeypatch):
    """Exercise real account resolution while keeping discovery offline."""
    records = [{
        "id": "11111111-1111-1111-1111-111111111111", "name": "TestAccount",
        "email_addresses": ["user@example.test"], "enabled": True,
    }]
    monkeypatch.setattr(AppleMailConnector, "list_accounts", lambda self: records)


@pytest.mark.parametrize("operation", ["move", "delete"])
@pytest.mark.parametrize("error_type", [OSError, IMAPClientError])
@pytest.mark.parametrize("phase,confirmed", [
    ("move", []), ("copy", []), ("store_deleted", ["copy"]),
    ("uid_expunge", ["copy", "store_deleted"]),
])
def test_dispatched_failure_is_not_a_fallback_error(client, operation, phase, confirmed, error_type):
    if phase != "move":
        client.capabilities.return_value = [b"UIDPLUS"]
    method = "add_flags" if phase == "store_deleted" else phase
    getattr(client, method).side_effect = error_type("lost acknowledgment")
    connector = ImapConnector("imap.example.test", 993, "user@example.test", "fake")
    with pytest.raises(Exception) as caught:
        if operation == "move":
            connector.move_messages(["a@example.test", "b@example.test"], "INBOX", "Archive")
        else:
            connector.delete_messages(["a@example.test", "b@example.test"], "INBOX")
    error = caught.value
    assert type(error).__name__ == "MailMutationOutcomeUnknownError"
    assert error.phase == phase
    assert list(error.confirmed_steps) == confirmed
    assert error.requested == 2
    assert isinstance(error.__cause__, error_type)


@pytest.mark.parametrize("method", ["login", "select_folder", "search"])
def test_pre_dispatch_error_stays_eligible_for_fallback(client, method):
    failure = IMAPClientError("select failed")
    getattr(client, method).side_effect = failure
    connector = ImapConnector("imap.example.test", 993, "user@example.test", "fake")
    with pytest.raises(IMAPClientError) as caught:
        connector.move_messages(["a@example.test"], "INBOX", "Archive")
    assert caught.value is failure
    client.move.assert_not_called()


@pytest.mark.parametrize("method", ["append_draft", "append_sent_copy"])
def test_append_unknown_retains_candidate_identity(client, method):
    client.append.side_effect = OSError("lost APPEND acknowledgment")
    connector = ImapConnector("imap.example.test", 993, "user@example.test", "fake")
    with pytest.raises(Exception) as caught:
        getattr(connector, method)(b"Message-ID: <candidate@example.test>\r\n\r\nbody")
    assert type(caught.value).__name__ == "MailMutationOutcomeUnknownError"
    assert caught.value.phase == "append"
    assert caught.value.candidate_draft_id == (
        "candidate@example.test" if method == "append_draft" else None
    )


def test_logout_does_not_turn_confirmed_move_into_failure(client):
    client.logout.side_effect = OSError("logout failed")
    connector = ImapConnector("imap.example.test", 993, "user@example.test", "fake")
    assert connector.move_messages(["a@example.test"], "INBOX", "Archive") == 2
    client.move.assert_called_once()


def test_logout_preserves_primary_failure(client):
    failure = IMAPClientError("search failed")
    client.search.side_effect = failure
    client.logout.side_effect = OSError("logout failed")
    connector = ImapConnector("imap.example.test", 993, "user@example.test", "fake")
    with pytest.raises(IMAPClientError) as caught:
        connector.move_messages(["a@example.test"], "INBOX", "Archive")
    assert caught.value is failure


def test_confirmed_append_survives_cleanup_failure(client):
    client.logout.side_effect = OSError("logout failed")
    connector = ImapConnector("imap.example.test", 993, "user@example.test", "fake")
    assert connector.append_draft(b"Message-ID: <candidate@example.test>\r\n\r\nbody") == "Drafts"
    client.append.assert_called_once()


def test_append_setup_failure_remains_a_plain_transport_error(client):
    failure = IMAPClientError("LIST failed")
    client.list_folders.side_effect = failure
    connector = ImapConnector("imap.example.test", 993, "user@example.test", "fake")
    with pytest.raises(IMAPClientError) as caught:
        connector.append_draft(b"Message-ID: <candidate@example.test>\r\n\r\nbody")
    assert caught.value is failure
    client.append.assert_not_called()


def test_pool_invalidates_raw_failure_before_error_conversion(client):
    pool = ImapConnectionPool()
    client.move.side_effect = OSError("lost acknowledgment")
    connector = ImapConnector("imap.example.test", 993, "user@example.test", "fake", pool=pool)
    with pytest.raises(Exception) as caught:
        connector.move_messages(["a@example.test"], "INBOX", "Archive")
    assert type(caught.value).__name__ == "MailMutationOutcomeUnknownError"
    assert not pool._cache
    client.logout.assert_called_once()


@pytest.mark.parametrize("operation", ["move", "delete"])
def test_orchestrator_does_not_replay_uncertain_mutation(client, account_snapshot, operation):
    connector = AppleMailConnector()
    client.move.side_effect = OSError("lost acknowledgment")
    with patch.object(connector, "_resolve_imap_config", return_value=(
        "imap.example.test", 993, "user@example.test",
    )), patch.object(connector, "_get_imap_password_with_fallback", return_value="fake"), \
            patch.object(connector, "_run_applescript") as native:
        with pytest.raises(MailMutationOutcomeUnknownError):
            if operation == "move":
                connector.update_message(
                    ["a@example.test"], destination_mailbox="Archive",
                    account="TestAccount", source_mailbox="INBOX",
                )
            else:
                connector.delete_messages(
                    ["a@example.test"], account="TestAccount", source_mailbox="INBOX",
                )
    native.assert_not_called()


@pytest.mark.parametrize("seed", ["new", "reply", "forward"])
def test_draft_fallback_does_not_consume_uncertain_append(account_snapshot, seed):
    connector = AppleMailConnector()
    error = MailMutationOutcomeUnknownError(
        operation="append_draft", phase="append", requested=1,
        candidate_draft_id="candidate@example.test",
    )
    args = {
        "seed": seed, "send_now": False, "from_account": "TestAccount", "to": ["a@example.test"],
        "cc": None, "bcc": None, "subject": "test", "body": "body", "attachment_paths": None,
    }
    method = "_create_draft_via_imap" if seed == "new" else "_create_reply_forward_draft_via_imap"
    with patch.object(connector, method, side_effect=error):
        with pytest.raises(MailMutationOutcomeUnknownError) as caught:
            if seed == "new":
                connector._try_imap_compose_draft(**args)
            else:
                connector._try_imap_reply_forward_draft(
                    **args, seed_id="original@example.test", seed_mailbox="INBOX", reply_all=False,
                )
    assert caught.value is error


@pytest.mark.parametrize("tool", ["update_message", "delete_messages", "create_draft", "update_draft"])
async def test_mcp_outcome_is_explicit_and_original_draft_retained(monkeypatch, tmp_path, tool):
    from apple_mail_fast_mcp import server

    monkeypatch.setenv("MAIL_TEST_MODE", "false")
    monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
    error = MailMutationOutcomeUnknownError(
        operation="append_draft" if "draft" in tool else "move", phase="append",
        requested=1, candidate_draft_id="candidate@example.test", mailbox="Drafts",
    )
    backend = MagicMock(spec=AppleMailConnector)
    backend.get_draft_state.return_value = {
        "to": ["a@example.test"], "body": "old body", "subject": "old",
        "content_type": "text/plain", "from_account": "TestAccount",
    }
    backend.create_draft.side_effect = error
    backend.update_message.side_effect = error
    backend.delete_messages.side_effect = error
    monkeypatch.setattr(server, "mail", backend)
    monkeypatch.setattr(server, "check_rate_limit", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_elicit_confirmation", AsyncMock(return_value=None))
    monkeypatch.setattr(server, "_validate_sender_selection", lambda *args, **kwargs: None)
    if tool == "update_message":
        result = await server.update_message(["1"], read_status=True)
    elif tool == "delete_messages":
        result = await server.delete_messages(["1"])
    elif tool == "create_draft":
        result = await server.create_draft(to=["a@example.test"], subject="test")
    else:
        result = await server.update_draft("old-1", body="replacement", attachment_paths=[])
        assert result["original_draft_id"] == "old-1"
        backend.delete_draft.assert_not_called()
    assert result["success"] is False
    assert result["error_type"] == "mutation_outcome_unknown"
    assert result["partial"] is True and result["retry_safe"] is False
    assert result["candidate_draft_id"] == "candidate@example.test"
    assert "count" not in result and "updated" not in result


@pytest.mark.parametrize("primary_failure", [False, True])
def test_cleanup_logging_failure_never_masks_result(client, monkeypatch, primary_failure):
    from apple_mail_fast_mcp import imap_connector
    failure = IMAPClientError("search failed")
    if primary_failure:
        client.search.side_effect = failure
    client.logout.side_effect = OSError("logout failed")
    monkeypatch.setattr(imap_connector.logger, "warning", MagicMock(side_effect=OSError("log unavailable")))
    connector = ImapConnector("imap.example.test", 993, "user@example.test", "fake")
    if primary_failure:
        with pytest.raises(IMAPClientError) as caught:
            connector.move_messages(["a@example.test"], "INBOX", "Archive")
        assert caught.value is failure
    else:
        assert connector.move_messages(["a@example.test"], "INBOX", "Archive") == 2
