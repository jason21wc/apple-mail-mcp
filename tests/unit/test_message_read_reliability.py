"""Read failures must not masquerade as missing messages or attachments."""

from unittest.mock import MagicMock, patch

import pytest

from apple_mail_fast_mcp.exceptions import (
    MailAppleScriptError,
    MailMessageNotFoundError,
)
from apple_mail_fast_mcp.mail_connector import AppleMailConnector


@pytest.fixture
def connector() -> AppleMailConnector:
    return AppleMailConnector()


@pytest.mark.parametrize("account,mailbox", [("iCloud", "INBOX"), ("iCloud", None), (None, "Archive")])
def test_numeric_message_keeps_scope_and_never_queries_imap(
    connector: AppleMailConnector, account: str | None, mailbox: str | None,
) -> None:
    with patch.object(connector, "_imap_get_message") as imap, patch.object(
        connector, "_get_message_applescript", return_value={"id": "123"},
    ) as applescript:
        result = connector.get_message("123", account=account, mailbox=mailbox)

    assert result == {"id": "123"}
    imap.assert_not_called()
    applescript.assert_called_once_with(
        "123", True, False, account=account, mailbox=mailbox,
    )


@pytest.mark.parametrize("account,mailbox", [("iCloud", "INBOX"), ("iCloud", None), (None, "Archive")])
def test_numeric_attachment_metadata_keeps_scope_and_never_queries_imap(
    connector: AppleMailConnector, account: str | None, mailbox: str | None,
) -> None:
    with patch.object(connector, "_imap_get_attachments") as imap, patch.object(
        connector, "_get_attachments_applescript", return_value=[],
    ) as applescript:
        assert connector.get_attachments("123", account=account, mailbox=mailbox) == []
    imap.assert_not_called()
    applescript.assert_called_once_with("123", account=account, mailbox=mailbox)


def test_attachment_metadata_scoped_lookup_precedes_read(connector: AppleMailConnector) -> None:
    with patch.object(connector, "_run_applescript", return_value="[]") as run:
        connector._get_attachments_applescript("123", account="iCloud", mailbox="Archive")
    script = run.call_args.args[0]
    body = script[script.index("with timeout of"):]
    assert 'set lookupAccounts to {account "iCloud"}' in body
    assert 'my resolveMailbox(acc, "Archive")' in body
    assert body.index('error "Can\'t get message: not found"') < body.index("my readAttachmentMetadata(msg)")


@pytest.mark.parametrize("stage,native_error", [
    ("attachment file size", "Can't get message"),
    ("message lookup", "Can't get messages of mailbox"),
])
def test_read_error_is_not_missing(
    connector: AppleMailConnector, stage: str, native_error: str,
) -> None:
    failure = MagicMock(
        returncode=1, stdout="",
        stderr=f"MAIL_READ_FAILED: {stage}; AppleScript error -10000; {native_error}",
    )
    with patch("apple_mail_fast_mcp.mail_connector.subprocess.run", return_value=failure):
        with pytest.raises(MailAppleScriptError, match=stage):
            connector._run_applescript("mock script")


def test_verified_missing_message_retains_exception(connector: AppleMailConnector) -> None:
    failure = MagicMock(returncode=1, stdout="", stderr="Can't get message: not found")
    with patch("apple_mail_fast_mcp.mail_connector.subprocess.run", return_value=failure):
        with pytest.raises(MailMessageNotFoundError):
            connector._run_applescript("mock script")


def test_message_lookup_finishes_before_reading_metadata(connector: AppleMailConnector) -> None:
    with patch.object(connector, "_run_applescript", return_value='{"id":"123"}') as run:
        connector._get_message_applescript("123", False, True)
    script = run.call_args.args[0]
    body = script[script.index("with timeout of"):]
    lookup_end = body.index('error "Can\'t get message: not found"')
    attachment_read = body.index("my readAttachmentMetadata(msg)")
    assert lookup_end < attachment_read
    lookup_query = body.index("set matchedMessages to")
    lookup_error = body.index('error "MAIL_READ_FAILED: message lookup;')
    assert body.index("try") < lookup_query < lookup_error < body.index("end try") < lookup_end
    assert "try" not in body[lookup_end:attachment_read]


@pytest.mark.parametrize("account,mailbox", [("iCloud", "Nested/Archive"), ("iCloud", None), (None, "Archive")])
def test_numeric_applescript_lookup_preserves_hints(
    connector: AppleMailConnector, account: str | None, mailbox: str | None,
) -> None:
    with patch.object(connector, "_run_applescript", return_value='{"id":"123"}') as run:
        connector._get_message_applescript("123", False, account=account, mailbox=mailbox)
    script = run.call_args.args[0]
    body = script[script.index("with timeout of"):]
    assert "whose (id is 123)" in body
    if account:
        assert 'account "iCloud"' in body
        assert "set lookupAccounts to accounts" not in body
    if mailbox:
        assert mailbox in body


def test_refused_rfc_scan_is_an_error_not_a_missing_message(connector: AppleMailConnector) -> None:
    with patch.object(connector, "_run_applescript") as run:
        with pytest.raises(MailAppleScriptError, match="IMAP"):
            connector._get_message_applescript("message@example.test", False)
    run.assert_not_called()


@pytest.mark.parametrize("operation", ["message", "search", "attachments", "selection"])
def test_attachment_readers_share_explicit_failure_handling(
    connector: AppleMailConnector, operation: str,
) -> None:
    payload = '{"id":"123"}' if operation == "message" else "[]"
    with patch.object(connector, "_run_applescript", return_value=payload) as run:
        if operation == "message":
            connector._get_message_applescript("123", False, True)
        elif operation == "search":
            connector._search_messages_applescript("iCloud", "INBOX", include_attachments=True)
        elif operation == "attachments":
            connector._get_attachments_applescript("123")
        else:
            connector.get_selected_messages(False, True)
    script = run.call_args.args[0]
    assert "my readAttachmentMetadata(msg)" in script
    assert "MAIL_READ_FAILED:" in script
    for stage in ("enumeration", "name", "MIME type", "file size", "downloaded"):
        assert f'"attachment {stage}"' in script
    assert "error errMsg" not in script  # Do not disclose message content via errors.
