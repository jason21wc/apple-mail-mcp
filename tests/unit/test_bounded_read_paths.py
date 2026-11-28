"""Production raw read entry points cannot bypass the transfer guard."""

from unittest.mock import MagicMock, patch

import pytest

from apple_mail_fast_mcp.imap_connector import ImapConnector
from apple_mail_fast_mcp.raw_message import MailRawMessageLimitError


@pytest.mark.parametrize("method", ["get_message", "fetch_raw_message", "fetch_raw_draft"])
def test_every_raw_read_rejects_oversize_before_body(method, monkeypatch):
    monkeypatch.setenv("APPLE_MAIL_MCP_MAX_RAW_MESSAGE_BYTES", "10")
    with patch("apple_mail_fast_mcp.imap_connector.IMAPClient") as factory:
        client = factory.return_value
        client.search.return_value = [7]
        client.list_folders.return_value = [([b"\\Drafts"], b"/", "Drafts")]
        client.fetch.side_effect = lambda uids, keys: {
            7: {b"RFC822.SIZE": 11, b"ENVELOPE": MagicMock(), b"FLAGS": ()}
        }
        connector = ImapConnector("imap.example.test", 993, "test@example.test", "fake")
        with pytest.raises(MailRawMessageLimitError):
            getattr(connector, method)("fixture@example.test")
        assert all(not any(b"BODY" in key for key in call.args[1])
                   for call in client.fetch.call_args_list)


@pytest.mark.parametrize("kwargs", [{"include_content": False}, {"headers_only": True}])
def test_metadata_read_never_requests_payload(kwargs):
    with patch("apple_mail_fast_mcp.imap_connector.IMAPClient") as factory:
        client = factory.return_value
        client.search.return_value = [7]
        envelope = MagicMock()
        envelope.date = None
        envelope.from_ = envelope.to = envelope.cc = ()
        envelope.message_id = b"<fixture@example.test>"
        envelope.subject = b"fixture"
        client.fetch.return_value = {7: {b"ENVELOPE": envelope, b"FLAGS": ()}}
        connector = ImapConnector("imap.example.test", 993, "test@example.test", "fake")
        result = connector.get_message("fixture@example.test", **kwargs)
        assert result["content"] == ""
        client.fetch.assert_called_once_with([7], [b"ENVELOPE", b"FLAGS"])


def test_unsolicited_literal_in_envelope_fetch_is_bounded(monkeypatch):
    monkeypatch.setenv("APPLE_MAIL_MCP_MAX_RAW_MESSAGE_BYTES", "10")
    with patch("apple_mail_fast_mcp.imap_connector.IMAPClient") as factory:
        client = factory.return_value
        client.search.return_value = [7]
        read = client._imap.read

        def fetch(uids, keys):
            client._imap.read(11)
            pytest.fail("unsolicited body bypassed the metadata guard")
        client.fetch.side_effect = fetch
        connector = ImapConnector("imap.example.test", 993, "test@example.test", "fake")
        with pytest.raises(MailRawMessageLimitError):
            connector.get_message("fixture@example.test")
        read.assert_not_called()
        client.shutdown.assert_called_once()
