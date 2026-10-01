"""Read-only diagnostic uses real identity resolution and never fetches mail."""

from unittest.mock import MagicMock, patch

import pytest
from imapclient.exceptions import LoginError

from apple_mail_fast_mcp.exceptions import (
    MailAccountNotFoundError,
    MailKeychainAccessDeniedError,
    MailKeychainEntryNotFoundError,
    MailKeychainError,
)
from apple_mail_fast_mcp.keychain import ImapCredential
from apple_mail_fast_mcp.mail_connector import AppleMailConnector

UID = "12345678-1234-1234-1234-123456789abc"


@pytest.fixture
def diagnostic(monkeypatch):
    connector = AppleMailConnector(imap_pool=MagicMock())
    monkeypatch.setattr(connector, "list_accounts", MagicMock(return_value=[
        {"name": "Work", "id": UID, "enabled": True},
    ]))
    monkeypatch.setattr(connector, "_resolve_imap_config", MagicMock(return_value=(
        "imap.example.invalid", 993, "login@example.invalid",
    )))
    with patch("apple_mail_fast_mcp.account_diagnostics.IMAPClient") as factory, \
            patch("apple_mail_fast_mcp.account_diagnostics.get_imap_credential",
                  return_value=ImapCredential("secret-fixture", "keychain")) as credential:
        client = factory.return_value
        client.capabilities.return_value = (b"IMAP4rev1", b"MOVE", b"X-SECRET=private")
        yield connector, client, credential, factory


@pytest.mark.parametrize("account", ["Work", UID])
def test_read_only_fresh_connection_does_not_change_breaker(diagnostic, account):
    connector, client, credential, factory = diagnostic
    connector._imap_failure_until[UID] = float("inf")
    result = connector.check_account(account, "Archive")
    assert result["success"] is True
    assert result["imap"]["status"] == "available"
    assert result["credential"] == {"source": "keychain", "identifier": "name"}
    assert result["breaker"]["open"] is True
    assert connector._imap_failure_until == {UID: float("inf")}
    credential.assert_called_once_with("Work", "login@example.invalid", timeout=30.0)
    client.select_folder.assert_called_once_with("Archive", readonly=True)
    assert result["imap"]["capabilities"] == ["IMAP4REV1", "MOVE"]
    factory.assert_called_once_with("imap.example.invalid", port=993, ssl=True, timeout=3.0)
    connector._imap_pool.session.assert_not_called()
    assert {call[0] for call in client.method_calls} <= {
        "login", "socket", "socket().settimeout", "capabilities", "select_folder", "logout",
    }
    assert "secret-fixture" not in str(result) and "private" not in str(result)
    connector.list_accounts.assert_called_once()


def test_auth_rejection_is_completed_unhealthy_without_alternate_credentials(diagnostic):
    connector, client, credential, _ = diagnostic
    client.login.side_effect = LoginError("secret-fixture server detail")
    client.logout.side_effect = OSError("cleanup")
    result = connector.check_account("Work")
    assert result["success"] is True
    assert result["imap"]["error_type"] == "imap_auth_failed"
    credential.assert_called_once()
    client.select_folder.assert_not_called()
    assert "secret-fixture" not in str(result)
    assert connector._imap_failure_until == {}


@pytest.mark.parametrize("account,mailbox", [("", "INBOX"), ("Work", "bad\nbox")])
def test_invalid_input_prevents_io(diagnostic, account, mailbox):
    connector, _, credential, factory = diagnostic
    with pytest.raises(ValueError):
        connector.check_account(account, mailbox)
    credential.assert_not_called()
    factory.assert_not_called()


def test_uuid_fallback_only_after_name_is_absent(diagnostic):
    connector, _, credential, _ = diagnostic
    credential.side_effect = [MailKeychainEntryNotFoundError("absent"), ImapCredential("fake", "environment")]
    result = connector.check_account(UID)
    assert [c.args[0] for c in credential.call_args_list] == ["Work", UID]
    assert result["credential"] == {"source": "environment", "identifier": "uuid"}


@pytest.mark.parametrize("failure,expected,calls", [
    (MailKeychainAccessDeniedError("private"), "keychain_access_denied", 1),
    (MailKeychainEntryNotFoundError("private"), "imap_not_configured", 2),
    (MailKeychainError("private"), "keychain_unavailable", 1),
])
def test_credential_failure_is_typed_and_does_not_connect(diagnostic, failure, expected, calls):
    connector, _, credential, factory = diagnostic
    credential.side_effect = failure
    result = connector.check_account("Work")
    assert result["success"] is True
    assert result["credential"]["error_type"] == expected
    assert credential.call_count == calls
    factory.assert_not_called()
    assert "private" not in str(result)


@pytest.mark.parametrize("phase", ["connect", "capabilities", "mailbox"])
def test_network_failures_retain_completed_components(diagnostic, phase):
    connector, client, _, factory = diagnostic
    failure = OSError("private server detail")
    if phase == "connect":
        factory.side_effect = failure
    else:
        getattr(client, "select_folder" if phase == "mailbox" else phase).side_effect = failure
    result = connector.check_account("Work")
    assert result["success"] is True
    assert result["imap"]["phase"] == phase
    if phase == "mailbox":
        assert result["imap"]["authentication"] == "passed"
        assert result["mailbox"]["error_type"] == "mailbox_unavailable"
    else:
        assert result["imap"]["error_type"] == "imap_connection_failed"
    assert "private server detail" not in str(result)


def test_configuration_failure_and_missing_endpoint_are_unhealthy(diagnostic):
    connector, _, credential, factory = diagnostic
    connector._resolve_imap_config.side_effect = RuntimeError("private detail")
    result = connector.check_account("Work")
    assert result["success"] and result["mail"]["status"] == "unavailable"
    connector._resolve_imap_config.side_effect = None
    connector._resolve_imap_config.return_value = ("", 0, "")
    result = connector.check_account("Work")
    assert result["success"] and result["imap"]["error_type"] == "imap_not_configured"
    credential.assert_not_called()
    factory.assert_not_called()


def test_ambiguous_or_missing_accounts_do_not_connect(diagnostic):
    connector, _, credential, factory = diagnostic
    with pytest.raises(MailAccountNotFoundError):
        connector.check_account("Missing")
    connector.list_accounts.return_value.append({"name": "Work", "id": "other"})
    with pytest.raises(ValueError, match="ambiguous"):
        connector.check_account("Work")
    credential.assert_not_called()
    factory.assert_not_called()


def test_unexpected_probe_error_is_redacted(diagnostic):
    connector, client, _, _ = diagnostic
    client.capabilities.side_effect = RuntimeError("private detail")
    result = connector.check_account("Work")
    assert result["success"] and result["imap"]["error_type"] == "diagnostic_error"
    assert "private detail" not in str(result)


@pytest.mark.parametrize("failure,error_type", [
    (MailAccountNotFoundError("private"), "account_not_found"),
    (ValueError("ambiguous"), "validation_error"),
    (RuntimeError("private"), "account_diagnostic_unavailable"),
])
def test_mcp_diagnostic_failure_mapping(monkeypatch, failure, error_type):
    from apple_mail_fast_mcp import server

    monkeypatch.setenv("MAIL_TEST_MODE", "false")
    backend = MagicMock(spec=AppleMailConnector)
    backend.check_account.side_effect = failure
    monkeypatch.setattr(server, "mail", backend)
    monkeypatch.setattr(server, "check_rate_limit", lambda *args, **kwargs: None)
    result = server.check_account("Work")
    assert result["success"] is False and result["error_type"] == error_type
    assert "private" not in str(result)


def test_mcp_test_mode_and_rate_limit_stop_before_probe(monkeypatch):
    from apple_mail_fast_mcp import server

    backend = MagicMock(spec=AppleMailConnector)
    monkeypatch.setattr(server, "mail", backend)
    monkeypatch.setenv("MAIL_TEST_MODE", "true")
    monkeypatch.delenv("MAIL_TEST_ACCOUNT", raising=False)
    assert server.check_account("Work")["success"] is False
    backend.check_account.assert_not_called()
    monkeypatch.setenv("MAIL_TEST_MODE", "false")
    limited = {"success": False, "error_type": "rate_limited"}
    monkeypatch.setattr(server, "check_rate_limit", lambda *args, **kwargs: limited)
    assert server.check_account("Work") == limited
    backend.check_account.assert_not_called()
