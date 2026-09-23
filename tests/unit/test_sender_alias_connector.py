"""Configured sender identity stays separate from transport authentication."""

import json
from email import policy
from email.parser import BytesParser
from unittest.mock import MagicMock, Mock

import pytest

from apple_mail_fast_mcp.draft_builder import build_draft_mime
from apple_mail_fast_mcp.mail_connector import AppleMailConnector


@pytest.fixture
def connector(monkeypatch):
    client = AppleMailConnector()
    monkeypatch.setattr(
        client,
        "list_accounts",
        Mock(
            return_value=[
                {
                    "id": "account-uuid",
                    "name": "Personal",
                    "full_name": "Alice",
                    "email_addresses": ["primary@example.com", "Alias@example.com"],
                    "enabled": True,
                }
            ]
        ),
    )
    monkeypatch.setattr(client, "_run_applescript", Mock(return_value="123"))
    monkeypatch.setattr(client, "_sync_account_drafts", Mock())
    monkeypatch.setattr(
        client,
        "_resolve_imap_config",
        Mock(return_value=("imap.example.com", 993, "login@example.com")),
    )
    monkeypatch.setattr(
        client,
        "_resolve_smtp_config",
        Mock(return_value=("smtp.example.com", 587, "login@example.com")),
    )
    monkeypatch.setattr(
        client, "_get_imap_password_with_fallback", Mock(return_value="fake-password")
    )
    monkeypatch.setattr(client, "_save_sent_copy", Mock())
    return client


@pytest.mark.parametrize("account", ["Personal", "account-uuid"])
def test_resolve_configured_alias_uses_canonical_spelling(connector, account):
    assert (
        connector._resolve_account_to_sender(account, "ALIAS@EXAMPLE.COM")
        == "Alice <Alias@example.com>"
    )
    assert connector._resolve_account_to_sender(account) == "Alice <primary@example.com>"


@pytest.mark.parametrize(
    "sender",
    [
        "",
        "other@example.com",
        "Alice <Alias@example.com>",
        "Alias@example.com,other@example.com",
        "Alias@example.com\r\nBcc: other@example.com",
        "Alias@example.com\x00",
        " Alias@example.com",
    ],
)
def test_invalid_alias_rejected_before_any_write(connector, monkeypatch, sender):
    clean = Mock()
    monkeypatch.setattr(connector, "_try_clean_create_or_send", clean)
    with pytest.raises(ValueError):
        connector.create_draft(
            to=["recipient@example.com"], subject="Hi", from_account="Personal", sender_email=sender
        )
    clean.assert_not_called()
    connector._run_applescript.assert_not_called()


def test_alias_requires_explicit_account(connector):
    with pytest.raises(ValueError, match="from_account"):
        connector.create_draft(
            to=["recipient@example.com"], subject="Hi", sender_email="Alias@example.com"
        )
    connector.list_accounts.assert_not_called()
    connector._run_applescript.assert_not_called()


@pytest.mark.parametrize("seed", ["new", "reply", "forward"])
@pytest.mark.parametrize("send_now", [False, True])
def test_alias_reaches_transport_without_changing_login(connector, monkeypatch, seed, send_now):
    imap = Mock()
    _, original = build_draft_mime(
        sender="Other <other@example.com>",
        to=["primary@example.com", "Alias@example.com", "login@example.com", "third@example.com"],
        subject="Original",
        body="Original body",
    )
    imap.fetch_raw_message.return_value = original
    imap_class = Mock(return_value=imap)
    smtp = Mock(return_value={"partial": False})
    monkeypatch.setattr("apple_mail_fast_mcp.mail_connector.ImapConnector", imap_class)
    monkeypatch.setattr(connector, "_smtp_send", smtp)
    result = connector.create_draft(
        seed=seed,
        seed_id="original@example.com" if seed != "new" else None,
        to=["recipient@example.com"] if seed != "reply" else None,
        subject="Hi",
        body="Hello",
        from_account="Personal",
        sender_email="alias@example.com",
        send_now=send_now,
        reply_all=seed == "reply",
    )
    raw = smtp.call_args.args[1] if send_now else imap.append_draft.call_args.args[0]
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert str(message["From"]) == "Alice <Alias@example.com>"
    assert result["sender_email"] == "Alias@example.com"
    assert result["from_account"] == "Personal"
    if seed == "reply":
        addresses = [a.addr_spec for header in ("To", "Cc") for a in message[header].addresses]
        assert set(addresses) == {"other@example.com", "third@example.com"}
    if not send_now or seed != "new":
        assert imap_class.call_args.args[2] == "login@example.com"
    if send_now:
        assert smtp.call_args.args[0] == "Personal"
        assert smtp.call_args.kwargs["smtp_config"][2] == "login@example.com"


@pytest.mark.parametrize("seed", ["new", "reply", "forward"])
def test_alias_survives_applescript_fallback(connector, monkeypatch, seed):
    monkeypatch.setattr(connector, "_try_clean_create_or_send", Mock(return_value=None))
    result = connector.create_draft(
        seed=seed,
        seed_id="42" if seed != "new" else None,
        to=["recipient@example.com"],
        subject="Hi",
        from_account="Personal",
        sender_email="alias@example.com",
    )
    assert (
        'set sender of theMessage to "Alice <Alias@example.com>"'
        in connector._run_applescript.call_args.args[0]
    )
    assert result["sender_email"] == "Alias@example.com"


@pytest.mark.parametrize(
    "sender,account,email",
    [
        ("Alice <alias@example.com>", "Personal", "Alias@example.com"),
        ("unknown@example.com", "", None),
    ],
)
def test_draft_state_retains_only_configured_identity(connector, sender, account, email):
    connector._run_applescript.return_value = json.dumps(
        {"found": True, "draft_id": "123", "from_account": "Personal", "sender": sender}
    )
    state = connector.get_draft_state("123")
    assert state["from_account"] == account
    assert state.get("sender_email") == email
    assert "sender" not in state


def test_same_account_resolves_name_and_uuid(connector):
    assert connector._same_account("Personal", "account-uuid")
    assert not connector._same_account("Personal", "Other")


@pytest.mark.parametrize("seed", ["new", "reply", "forward"])
@pytest.mark.parametrize("send_now", [False, True])
def test_network_fallback_preserves_selected_alias(connector, monkeypatch, send_now, seed):
    connector._resolve_imap_config.side_effect = OSError("unavailable")
    connector._resolve_smtp_config.side_effect = OSError("unavailable")
    monkeypatch.setattr(connector, "_maybe_resolve_rfc_seed_id", Mock(return_value="42"))
    result = connector.create_draft(
        seed=seed,
        seed_id="original@example.com" if seed != "new" else None,
        to=["recipient@example.com"],
        subject="Hi",
        from_account="Personal",
        sender_email="Alias@example.com",
        send_now=send_now,
    )
    assert result["sender_email"] == "Alias@example.com"
    assert (
        'set sender of theMessage to "Alice <Alias@example.com>"'
        in connector._run_applescript.call_args.args[0]
    )


def test_sender_selection_does_not_leak_into_next_request(connector, monkeypatch):
    imap = Mock()
    monkeypatch.setattr("apple_mail_fast_mcp.mail_connector.ImapConnector", Mock(return_value=imap))
    connector.create_draft(
        to=["recipient@example.com"],
        subject="First",
        from_account="Personal",
        sender_email="Alias@example.com",
    )
    result = connector.create_draft(
        to=["recipient@example.com"], subject="Second", from_account="Personal"
    )
    raw = imap.append_draft.call_args.args[0]
    assert (
        str(BytesParser(policy=policy.default).parsebytes(raw)["From"])
        == "Alice <primary@example.com>"
    )
    assert result["sender_email"] == "primary@example.com"


@pytest.mark.parametrize("sender_email", [None, "Alias@example.com"])
def test_reply_all_keeps_explicit_self_recipient_overrides(connector, monkeypatch, sender_email):
    imap = Mock()
    _, original = build_draft_mime(
        sender="other@example.com", to=["primary@example.com"], subject="Hi", body="Original"
    )
    imap.fetch_raw_message.return_value = original
    monkeypatch.setattr("apple_mail_fast_mcp.mail_connector.ImapConnector", Mock(return_value=imap))
    connector.create_draft(
        seed="reply",
        seed_id="original@example.com",
        from_account="Personal",
        sender_email=sender_email,
        reply_all=True,
        to=["Alias@example.com"],
        cc=["primary@example.com"],
        subject="Hi",
    )
    message = BytesParser(policy=policy.default).parsebytes(imap.append_draft.call_args.args[0])
    assert message["To"].addresses[0].addr_spec == "Alias@example.com"
    assert message["Cc"].addresses[0].addr_spec == "primary@example.com"


def test_reply_all_excludes_aliases_without_explicit_sender(connector, monkeypatch):
    imap = Mock()
    _, original = build_draft_mime(
        sender="other@example.com",
        to=["ALIAS@example.com", "primary@example.com", "login@example.com"],
        cc=["third@example.com"],
        subject="Hi",
        body="Original",
    )
    imap.fetch_raw_message.return_value = original
    monkeypatch.setattr("apple_mail_fast_mcp.mail_connector.ImapConnector", Mock(return_value=imap))
    connector.create_draft(
        seed="reply", seed_id="original@example.com", from_account="Personal", reply_all=True
    )
    message = BytesParser(policy=policy.default).parsebytes(imap.append_draft.call_args.args[0])
    assert str(message["To"]) == "other@example.com"
    assert str(message["Cc"]) == "third@example.com"
    connector.list_accounts.assert_called_once()


def test_smtp_envelope_uses_alias_while_authentication_uses_login(connector, monkeypatch):
    smtp_class = MagicMock()
    smtp = smtp_class.return_value.__enter__.return_value
    smtp.send_message.return_value = {}
    monkeypatch.setattr("apple_mail_fast_mcp.smtp_sender.smtplib.SMTP", smtp_class)
    result = connector.create_draft(
        to=["recipient@example.com"],
        subject="Hi",
        from_account="Personal",
        sender_email="Alias@example.com",
        send_now=True,
    )
    smtp.login.assert_called_once_with("login@example.com", "fake-password")
    assert smtp.send_message.call_args.kwargs["from_addr"] == "Alias@example.com"
    assert result["sender_email"] == "Alias@example.com"
    connector.list_accounts.assert_called_once()
