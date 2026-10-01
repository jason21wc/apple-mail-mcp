"""Account identity and message identifier contracts at transport boundaries."""

import json
from unittest.mock import Mock

import pytest

from apple_mail_fast_mcp import imap_overrides, mail_connector
from apple_mail_fast_mcp.exceptions import (
    MailAccountNotFoundError,
    MailKeychainAccessDeniedError,
    MailKeychainEntryNotFoundError,
)
from apple_mail_fast_mcp.mail_connector import AppleMailConnector

pytestmark = pytest.mark.real_account_fallback
ACCOUNT_ID = "12345678-1234-1234-1234-123456789abc"


@pytest.fixture
def connector(monkeypatch):
    connector = AppleMailConnector()
    monkeypatch.setattr(
        connector,
        "list_accounts",
        Mock(
            return_value=[
                {
                    "name": "Work",
                    "id": ACCOUNT_ID,
                    "email_addresses": ["person@example.com"],
                }
            ]
        ),
    )
    monkeypatch.setattr(
        connector,
        "_run_applescript",
        Mock(
            return_value=json.dumps(
                {
                    "host": "mail.example.com",
                    "port": 993,
                    "user_name": "person@example.com",
                    "email_addresses": ["person@example.com"],
                }
            )
        ),
    )
    return connector


@pytest.mark.parametrize("account", ["Work", ACCOUNT_ID])
def test_names_and_uuids_choose_same_whole_override(connector, account):
    imap_overrides.set_server_override("Work", host="preferred.example.com", port=None)
    imap_overrides.set_server_override(ACCOUNT_ID, host="other.example.com", port=1993)
    imap_overrides.set_login_override("Work", "preferred@example.com")
    imap_overrides.set_login_override(ACCOUNT_ID, "other@example.com")
    assert connector._resolve_imap_config(account) == (
        "preferred.example.com",
        993,
        "preferred@example.com",
    )
    assert f'account id "{ACCOUNT_ID}"' in connector._run_applescript.call_args.args[0]


@pytest.mark.parametrize("account", ["Work", ACCOUNT_ID])
def test_password_name_wins_regardless_of_caller_form(connector, monkeypatch, account):
    password = Mock(return_value="test-password")
    monkeypatch.setattr(mail_connector, "get_imap_password", password)
    assert (
        connector._get_imap_password_with_fallback(account, "person@example.com") == "test-password"
    )
    password.assert_called_once_with("Work", "person@example.com")


def test_password_uuid_fallback_only_for_not_found(connector, monkeypatch):
    password = Mock(side_effect=[MailKeychainEntryNotFoundError("missing"), "test-password"])
    monkeypatch.setattr(mail_connector, "get_imap_password", password)
    assert (
        connector._get_imap_password_with_fallback(ACCOUNT_ID, "person@example.com")
        == "test-password"
    )
    assert [call.args[0] for call in password.call_args_list] == ["Work", ACCOUNT_ID]
    password.reset_mock(side_effect=True)
    password.side_effect = MailKeychainAccessDeniedError("denied")
    with pytest.raises(MailKeychainAccessDeniedError):
        connector._get_imap_password_with_fallback(ACCOUNT_ID, "person@example.com")
    password.assert_called_once()


def test_breaker_is_shared_across_name_and_uuid(connector):
    connector._log_imap_fallback("Work", OSError("offline"))
    assert set(connector._imap_failure_until) == {ACCOUNT_ID}
    assert connector._imap_breaker_open(ACCOUNT_ID)
    connector._imap_clear_breaker(ACCOUNT_ID)
    assert not connector._imap_breaker_open("Work")


def test_scope_reuses_snapshot_then_refreshes_after_exit(connector, monkeypatch):
    monkeypatch.setattr(mail_connector, "get_imap_password", Mock(return_value="test-password"))
    with connector._account_identity_scope():
        assert connector._resolve_account_identity("Work").id == ACCOUNT_ID
        connector._resolve_imap_config(ACCOUNT_ID)
        connector._get_imap_password_with_fallback("Work", "person@example.com")
        connector._imap_clear_breaker(ACCOUNT_ID)
        connector.list_accounts.assert_called_once()
    connector.list_accounts.return_value = [{"name": "Renamed", "id": ACCOUNT_ID}]
    assert connector._resolve_account_identity(ACCOUNT_ID).name == "Renamed"
    with pytest.raises(MailAccountNotFoundError):
        connector._resolve_account_identity("Work")


@pytest.mark.parametrize("account", ["Work", ACCOUNT_ID])
def test_ambiguous_identity_rejected_before_transport(connector, monkeypatch, account):
    connector.list_accounts.return_value.append({"name": account, "id": "another-id"})
    password = Mock()
    monkeypatch.setattr(mail_connector, "get_imap_password", password)
    with pytest.raises(ValueError, match="ambiguous"):
        connector._resolve_imap_config(account)
    connector._run_applescript.assert_not_called()
    password.assert_not_called()


def test_numeric_content_uses_native_with_scope(connector, monkeypatch):
    native = Mock(return_value={"payload": b"ok"})
    imap = Mock()
    monkeypatch.setattr(connector, "_get_attachment_content_applescript", native)
    monkeypatch.setattr(connector, "_imap_get_attachment_content", imap)
    assert connector.get_attachment_content(
        "42", 0, account="Work", mailbox="Archive/Receipts"
    ) == {"payload": b"ok"}
    native.assert_called_once_with("42", 0, account="Work", mailbox="Archive/Receipts")
    imap.assert_not_called()


def test_numeric_disk_save_scopes_metadata_and_save_script(connector, monkeypatch, tmp_path):
    metadata = Mock(return_value=[{"name": "report.txt", "size": 2}])
    imap = Mock()
    monkeypatch.setattr(connector, "_get_attachments_applescript", metadata)
    monkeypatch.setattr(connector, "_imap_save_attachments", imap)
    connector._run_applescript.return_value = "1"
    assert (
        connector.save_attachments("42", tmp_path, account="Work", mailbox="Archive/Receipts")[
            "saved"
        ]
        == 1
    )
    imap.assert_not_called()
    metadata.assert_called_once_with("42", account="Work", mailbox="Archive/Receipts")
    script = connector._run_applescript.call_args.args[0]
    assert f'account id "{ACCOUNT_ID}"' in script
    assert 'my resolveMailbox(acc, "Archive/Receipts")' in script
    assert "on resolveMailbox" in script
    assert "whose (id is 42)" in script


def test_single_native_save_script_carries_scope(connector, tmp_path):
    connector._save_one_attachment_applescript(
        "42", 1, tmp_path / "a.txt", account="Work", mailbox="Archive/Receipts"
    )
    script = connector._run_applescript.call_args.args[0]
    assert f'account id "{ACCOUNT_ID}"' in script
    assert 'my resolveMailbox(acc, "Archive/Receipts")' in script
    assert "on resolveMailbox" in script
    assert "MAIL_READ_FAILED" in script


def test_partial_port_override_does_not_borrow_uuid_host(connector):
    imap_overrides.set_server_override("Work", host=None, port=1993)
    imap_overrides.set_server_override(ACCOUNT_ID, host="other.example.com", port=2993)
    assert connector._resolve_imap_config(ACCOUNT_ID) == (
        "mail.example.com",
        1993,
        "person@example.com",
    )


def test_uuid_overrides_used_when_name_has_no_record(connector):
    imap_overrides.set_server_override(ACCOUNT_ID, host="override.example.com", port=1993)
    imap_overrides.set_login_override(ACCOUNT_ID, "override@example.com")
    assert connector._resolve_imap_config("Work") == (
        "override.example.com",
        1993,
        "override@example.com",
    )


def test_scope_resets_after_failure_and_nested_calls(connector):
    with pytest.raises(RuntimeError):
        with connector._account_identity_scope():
            with connector._account_identity_scope():
                connector._resolve_account_identity("Work")
            connector._resolve_account_identity(ACCOUNT_ID)
            connector.list_accounts.assert_called_once()
            raise RuntimeError("operation failed")
    connector.list_accounts.return_value = [{"name": "Renamed", "id": ACCOUNT_ID}]
    assert connector._resolve_account_identity(ACCOUNT_ID).name == "Renamed"
    assert connector.list_accounts.call_count == 2


def test_concurrent_operations_do_not_share_account_snapshots(connector, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, local

    state = local()
    barrier = Barrier(2)
    monkeypatch.setattr(
        connector, "list_accounts", lambda: [{"name": state.name, "id": ACCOUNT_ID}]
    )

    def resolve(name):
        state.name = name
        with connector._account_identity_scope():
            identity = connector._resolve_account_identity(ACCOUNT_ID)
            barrier.wait(timeout=5)
            return identity.name, connector._resolve_account_identity(ACCOUNT_ID).name

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(resolve, ["First", "Second"])) == [
            ("First", "First"),
            ("Second", "Second"),
        ]


@pytest.mark.parametrize("record", [{"name": "Work"}, {"name": "Work", "id": ""}])
def test_incomplete_account_identity_never_uses_transport(connector, record):
    connector.list_accounts.return_value = [record]
    with pytest.raises(ValueError, match="incomplete"):
        connector._resolve_imap_config("Work")
    connector._run_applescript.assert_not_called()


def test_unavailable_account_snapshot_does_not_guess(connector):
    connector.list_accounts.side_effect = OSError("Mail unavailable")
    with pytest.raises(OSError, match="Mail unavailable"):
        connector._resolve_imap_config("Work")
    connector._run_applescript.assert_not_called()


def test_native_content_preserves_scope_through_metadata_and_save(connector, monkeypatch):
    metadata = Mock(return_value=[{"name": "a.txt", "size": 2}])
    monkeypatch.setattr(connector, "_get_attachments_applescript", metadata)

    def save(message_id, index, dest, **scope):
        assert (message_id, index, scope) == (
            "42",
            1,
            {"account": "Work", "mailbox": "Archive/Receipts"},
        )
        dest.write_bytes(b"ok")

    monkeypatch.setattr(connector, "_save_one_attachment_applescript", save)
    result = connector.get_attachment_content("42", 0, account="Work", mailbox="Archive/Receipts")
    assert result["payload"] == b"ok"
    metadata.assert_called_once_with("42", account="Work", mailbox="Archive/Receipts")


def test_ambiguous_numeric_scope_rejected_before_native_save(connector, tmp_path):
    connector.list_accounts.return_value.append({"name": "Work", "id": "another-id"})
    with pytest.raises(ValueError, match="ambiguous"):
        connector.save_attachments("42", tmp_path, account="Work", mailbox="INBOX")
    connector._run_applescript.assert_not_called()


def test_unique_uuid_with_duplicate_name_cannot_use_shared_credentials(connector, monkeypatch):
    connector.list_accounts.return_value.append({"name": "Work", "id": "other-uuid"})
    password = Mock()
    monkeypatch.setattr(mail_connector, "get_imap_password", password)
    with pytest.raises(ValueError, match="ambiguous"):
        connector._get_imap_password_with_fallback(ACCOUNT_ID, "person@example.com")
    password.assert_not_called()


@pytest.mark.parametrize("operation", ["get_message", "create_draft", "_get_thread_with_status"])
def test_public_transport_entries_share_real_snapshot_and_refresh(
    connector, monkeypatch, operation
):
    imap = Mock()
    imap.get_message.return_value = {"id": "fixture@example.com"}
    imap.resolve_anchor.return_value = {"rfc_message_id": "fixture@example.com", "references": []}
    imap.find_thread_members.return_value = [{"id": "fixture@example.com"}]
    monkeypatch.setattr(mail_connector, "ImapConnector", Mock(return_value=imap))
    password = Mock(return_value="test-password")
    monkeypatch.setattr(mail_connector, "get_imap_password", password)
    for name in ("Work", "Renamed"):
        connector.list_accounts.return_value = [
            {"name": name, "id": ACCOUNT_ID, "email_addresses": ["person@example.com"]}
        ]
        connector.list_accounts.reset_mock()
        if operation == "create_draft":
            connector.create_draft(
                from_account=ACCOUNT_ID, to=["recipient@example.com"], subject="Fixture"
            )
        else:
            getattr(connector, operation)(
                "fixture@example.com", account=ACCOUNT_ID, mailbox="INBOX"
            )
        connector.list_accounts.assert_called_once()
        assert password.call_args.args[0] == name


@pytest.mark.parametrize("name_source", ["current", "legacy", "missing"])
def test_name_credential_sources_finish_before_uuid_environment(
    connector, monkeypatch, name_source
):
    from apple_mail_fast_mcp import keychain

    monkeypatch.delenv("APPLE_MAIL_MCP_IMAP_PASSWORD_WORK", raising=False)
    monkeypatch.setenv(keychain._env_var_name(ACCOUNT_ID), "uuid-test-password")
    missing = MailKeychainEntryNotFoundError("missing")
    outcomes = {
        "current": ["name-test-password"],
        "legacy": [missing, "name-test-password"],
        "missing": [missing, missing],
    }
    lookup = Mock(side_effect=outcomes[name_source])
    monkeypatch.setattr(keychain, "_find_password", lookup)
    result = connector._get_imap_password_with_fallback(ACCOUNT_ID, "person@example.com")
    assert result == ("uuid-test-password" if name_source == "missing" else "name-test-password")
    expected = ["apple-mail-fast-mcp.imap.Work"]
    if name_source != "current":
        expected.append("apple-mail-mcp.imap.Work")
    assert [call.args[0] for call in lookup.call_args_list] == expected


@pytest.mark.parametrize("account", ["Work", ACCOUNT_ID])
def test_numeric_and_rfc_payload_paths_return_and_save_identical_bytes(
    connector, monkeypatch, tmp_path, account
):
    from email.message import EmailMessage

    payload = b"known attachment bytes\x00\xff"
    message = EmailMessage()
    message["Message-ID"] = "<fixture@example.com>"
    message.set_content("fixture")
    message.add_attachment(
        payload, maintype="application", subtype="octet-stream", filename="report.bin"
    )
    imap = Mock()
    imap.fetch_raw_message.return_value = message.as_bytes()
    imap_class = Mock(return_value=imap)
    monkeypatch.setattr(mail_connector, "ImapConnector", imap_class)
    monkeypatch.setattr(mail_connector, "get_imap_password", Mock(return_value="test-password"))
    monkeypatch.setattr(
        connector,
        "_get_attachments_applescript",
        Mock(
            return_value=[
                {
                    "name": "report.bin",
                    "size": len(payload),
                    "mime_type": "application/octet-stream",
                }
            ]
        ),
    )

    def save_native(_mid, _index, path, **scope):
        assert scope == {"account": account, "mailbox": "Archive/Receipts"}
        path.write_bytes(payload)

    monkeypatch.setattr(connector, "_save_one_attachment_applescript", save_native)
    numeric = connector.get_attachment_content("42", 0, account=account, mailbox="Archive/Receipts")
    imap_class.assert_not_called()
    rfc = connector.get_attachment_content(
        "fixture@example.com", 0, account=account, mailbox="Archive/Receipts"
    )
    assert (
        numeric
        == rfc
        == {
            "name": "report.bin",
            "mime_type": "application/octet-stream",
            "size": len(payload),
            "payload": payload,
        }
    )
    imap.fetch_raw_message.assert_called_once_with("fixture@example.com", "Archive/Receipts")

    native_dir = tmp_path / "native"
    native_dir.mkdir()
    config_result = connector._run_applescript.return_value

    def run_native(script):
        if "set pathList" in script:
            assert f'account id "{ACCOUNT_ID}"' in script
            assert 'my resolveMailbox(acc, "Archive/Receipts")' in script
            (native_dir / "report.bin").write_bytes(payload)
            return "1"
        return config_result

    connector._run_applescript.side_effect = run_native
    imap_class.reset_mock()
    native_result = connector.save_attachments(
        "42", native_dir, account=account, mailbox="Archive/Receipts"
    )
    imap_class.assert_not_called()
    rfc_dir = tmp_path / "imap"
    rfc_dir.mkdir()
    rfc_result = connector.save_attachments(
        "fixture@example.com", rfc_dir, account=account, mailbox="Archive/Receipts"
    )
    assert native_result == rfc_result == {"saved": 1, "rejected": []}
    assert (
        (native_dir / "report.bin").read_bytes() == (rfc_dir / "report.bin").read_bytes() == payload
    )
