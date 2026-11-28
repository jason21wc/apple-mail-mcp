"""Selected IMAP parts preserve canonical indices without unbounded fallback."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from apple_mail_fast_mcp.draft_builder import extract_attachment_payloads
from apple_mail_fast_mcp.exceptions import MailMessageReadIncompleteError
from apple_mail_fast_mcp.imap_connector import ImapConnector
from apple_mail_fast_mcp.raw_message import MailRawMessageLimitError, MailRawMessageReadError
from tests.unit.test_attachment_parts import paired_parts  # noqa: F401


def backend(raw, structure, bodies):
    client = MagicMock()
    pending = [b""]
    client._imap = SimpleNamespace(read=lambda n: pending[0][:n])
    client.search.return_value = [7]

    def fetch(uids, keys):
        assert uids == [7]
        if keys == [b"RFC822.SIZE"]:
            return {7: {b"RFC822.SIZE": len(raw)}}
        if keys == [b"BODYSTRUCTURE"]:
            return {7: {b"BODYSTRUCTURE": structure}}
        key = keys[0].decode()
        section = key.split("[", 1)[1].split("]", 1)[0]
        start, count = map(int, key.split("<")[1].rstrip(">").split("."))
        body = raw if not section else bodies[section]
        pending[0] = body[start:start + count]
        literal = client._imap.read(len(pending[0]))
        return {7: {f"BODY[{section}]<{start}>".encode(): literal}}
    client.fetch.side_effect = fetch
    return client


@pytest.fixture
def prepared(paired_parts):  # noqa: F811 — imported shared pytest fixture
    raw, structure, bodies = paired_parts
    client = backend(raw, structure, dict(zip(["2.2", "3.2", "4", "5"], bodies, strict=True)))
    with patch("apple_mail_fast_mcp.imap_connector.IMAPClient", return_value=client):
        yield ImapConnector("imap.example.test", 993, "fixture@example.test", "fake"), client, raw, structure


def body_requests(client):
    return [call.args[1][0] for call in client.fetch.call_args_list if call.args[1][0].startswith(b"BODY.PEEK")]


def test_selected_payload_uses_only_requested_sections_and_canonical_indices(prepared):
    connector, client, raw, _ = prepared
    total, payloads = connector.fetch_attachment_payloads("fixture@example.test", "INBOX", indices=[2, 0, 2, -1, 99])
    expected = extract_attachment_payloads(raw)
    assert total == 4
    assert payloads == {0: expected[0], 2: expected[2]}
    assert {key.split(b"]")[0] for key in body_requests(client)} == {b"BODY.PEEK[2.2", b"BODY.PEEK[4"}
    client.select_folder.assert_called_once_with("INBOX", readonly=True)


def test_all_parts_match_canonical_including_real_empty_file(prepared):
    connector, client, raw, _ = prepared
    count, payloads = connector.fetch_attachment_payloads("fixture@example.test")
    expected = extract_attachment_payloads(raw)
    assert count == len(expected)
    assert payloads == dict(enumerate(expected))
    assert b"BODY.PEEK[5]<0.1>" in body_requests(client)
    assert all(not key.startswith(b"BODY.PEEK[]") for key in body_requests(client))


@pytest.mark.parametrize("indices", [[], [-1, 99]])
def test_no_valid_selection_downloads_no_payload(prepared, indices):
    connector, client, _, _ = prepared
    assert connector.fetch_attachment_payloads("fixture@example.test", indices=indices) == (4, {})
    assert body_requests(client) == []


def test_ambiguous_structure_chooses_bounded_canonical_fallback_before_any_part(prepared):
    connector, client, raw, _ = prepared
    original = client.fetch.side_effect
    def fetch(uids, keys):
        if keys == [b"BODYSTRUCTURE"]:
            return {7: {b"BODYSTRUCTURE": None}}
        return original(uids, keys)
    client.fetch.side_effect = fetch
    total, payloads = connector.fetch_attachment_payloads("fixture@example.test", indices=[1])
    assert total == 4
    assert payloads == {1: extract_attachment_payloads(raw, indices=[1])[0]}
    assert all(key.startswith(b"BODY.PEEK[]") for key in body_requests(client))


def test_ambiguous_empty_selection_is_incomplete_without_body_read(prepared):
    connector, client, _, _ = prepared
    client.fetch.side_effect = [{7: {b"RFC822.SIZE": 100}}, {7: {b"BODYSTRUCTURE": None}}]
    with pytest.raises(MailMessageReadIncompleteError):
        connector.fetch_attachment_payloads("fixture@example.test", indices=[])
    assert body_requests(client) == []


def test_whole_message_budget_still_applies_to_one_small_part(prepared, monkeypatch):
    connector, client, raw, _ = prepared
    monkeypatch.setenv("APPLE_MAIL_MCP_MAX_RAW_MESSAGE_BYTES", str(len(raw) - 1))
    with pytest.raises(MailRawMessageLimitError):
        connector.fetch_attachment_payloads("fixture@example.test", indices=[3])
    client.fetch.assert_called_once_with([7], [b"RFC822.SIZE"])


def test_metadata_only_is_strict_and_never_downloads_body(prepared):
    connector, client, _, _ = prepared
    assert len(connector.get_attachments("fixture@example.test")) == 4
    assert body_requests(client) == []
    client.fetch.reset_mock()
    client.fetch.side_effect = [{7: {b"BODYSTRUCTURE": None}}]
    with pytest.raises(MailMessageReadIncompleteError):
        connector.get_attachments("fixture@example.test")
    client.fetch.assert_called_once_with([7], [b"BODYSTRUCTURE"])


@pytest.mark.parametrize("failure", ["decode", "transport", "wrong_uid", "wrong_section", "short", "long"])
def test_selected_read_failure_never_fetches_whole_message(prepared, failure):
    connector, client, _, _ = prepared
    original = client.fetch.side_effect
    def fetch(uids, keys):
        if keys[0].startswith(b"BODY.PEEK"):
            if failure == "transport":
                raise OSError("fixture disconnected")
            response = original(uids, keys)
            key, value = next(iter(response[7].items()))
            if failure == "decode":
                return {7: {key: b"!" * len(value)}}
            if failure == "wrong_uid":
                return {8: {key: value}}
            if failure == "wrong_section":
                return {7: {b"BODY[99]<0>": value}}
            if failure == "short":
                return {7: {key: value[:-1]}}
            if failure == "long":
                return {7: {key: value + b"x"}}
        return original(uids, keys)
    client.fetch.side_effect = fetch
    with pytest.raises((MailMessageReadIncompleteError, MailRawMessageLimitError)):
        connector.fetch_attachment_payloads("fixture@example.test", indices=[0])
    assert all(not key.startswith(b"BODY.PEEK[]") for key in body_requests(client))


def test_missing_size_is_not_a_fallback_trigger(prepared):
    connector, client, _, _ = prepared
    client.fetch.side_effect = [{7: {}}]
    with pytest.raises(MailRawMessageReadError):
        connector.fetch_attachment_payloads("fixture@example.test", indices=[0])
    client.fetch.assert_called_once()


def test_unsupported_encoding_chooses_fallback_before_any_selected_body(prepared):
    connector, client, raw, structure = prepared
    import copy
    changed = copy.deepcopy(structure)
    image = list(changed[0][1][0][1])
    image[5] = b"X-UNKNOWN"
    changed[0][1][0][1] = tuple(image)
    original = client.fetch.side_effect
    def fetch(uids, keys):
        if keys == [b"BODYSTRUCTURE"]:
            return {7: {b"BODYSTRUCTURE": changed}}
        return original(uids, keys)
    client.fetch.side_effect = fetch
    count, selected = connector.fetch_attachment_payloads("fixture@example.test", indices=[0, 2])
    assert count == 4
    canonical = extract_attachment_payloads(raw)
    assert selected == {0: canonical[0], 2: canonical[2]}
    assert all(key.startswith(b"BODY.PEEK[]") for key in body_requests(client))


def test_forwarded_message_falls_back_as_one_whole_attachment():
    from email.message import EmailMessage

    from imapclient.response_types import BodyData

    from tests.unit.test_attachment_parts import leaf

    inner = EmailMessage()
    inner.set_content("forwarded body")
    inner.add_attachment(b"nested", maintype="application", subtype="pdf", filename="inner.pdf")
    outer = EmailMessage()
    outer.set_content("body")
    outer.add_attachment(inner, filename="forwarded.eml")
    raw = outer.as_bytes()
    structure = BodyData.create((leaf(b"TEXT", b"PLAIN", disposition=None),
                                 leaf(b"MESSAGE", b"RFC822", filename=b"forwarded.eml", encoding=b"7BIT"),
                                 b"MIXED", None, None))
    client = backend(raw, structure, {})
    with patch("apple_mail_fast_mcp.imap_connector.IMAPClient", return_value=client):
        count, selected = ImapConnector("h", 993, "u", "p").fetch_attachment_payloads("fixture@example.test", indices=[0])
    assert count == 1
    assert selected == {0: extract_attachment_payloads(raw)[0]}
    assert all(key.startswith(b"BODY.PEEK[]") for key in body_requests(client))


def test_metadata_literals_and_all_selected_sections_share_one_budget(prepared, monkeypatch):
    connector, client, raw, _ = prepared
    monkeypatch.setenv("APPLE_MAIL_MCP_MAX_RAW_MESSAGE_BYTES", str(len(raw)))
    original = client.fetch.side_effect
    client._imap.read = lambda size: b"m" * size
    def fetch(uids, keys):
        if keys == [b"BODYSTRUCTURE"]:
            client._imap.read(len(raw) - 1)
        return original(uids, keys)
    client.fetch.side_effect = fetch
    with pytest.raises(MailRawMessageLimitError):
        connector.fetch_attachment_payloads("fixture@example.test", indices=[0, 1])
    assert body_requests(client) == []


def test_has_attachment_unknown_is_incomplete_and_inline_name_matches_metadata(prepared):
    from apple_mail_fast_mcp.imap_connector import _bodystructure_has_attachment

    _, _, _, structure = prepared
    image = structure[0][1][0][1]
    assert _bodystructure_has_attachment(image) is True
    with pytest.raises(MailMessageReadIncompleteError):
        _bodystructure_has_attachment(None)


@pytest.mark.parametrize("options", [{"has_attachment": True}, {"has_attachment": False}, {"include_attachments": True}])
@pytest.mark.parametrize("failure", ["malformed_structure", "transfer_limit"])
def test_search_preserves_typed_attachment_failures_through_mcp(monkeypatch, options, failure):
    from apple_mail_fast_mcp import server
    from apple_mail_fast_mcp.mail_connector import AppleMailConnector
    from tests.unit.test_imap_connector import _fake_envelope

    connector = AppleMailConnector()
    monkeypatch.setattr(connector, "list_accounts", lambda: [{
        "id": "11111111-1111-1111-1111-111111111111", "name": "Fixture",
        "email_addresses": ["fixture@example.test"], "enabled": True,
    }])
    monkeypatch.setattr(connector, "_resolve_imap_config", lambda account: ("imap.example.test", 993, "fixture@example.test"))
    monkeypatch.setattr(connector, "_get_imap_password_with_fallback", lambda *args: "fixture-password")
    native = MagicMock(side_effect=AssertionError("incomplete search must not fall back to native"))
    monkeypatch.setattr(connector, "_search_messages_applescript", native)
    monkeypatch.setattr(server, "mail", connector)
    monkeypatch.setattr(server, "check_rate_limit", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "check_test_mode_safety", lambda *args, **kwargs: None)
    client = MagicMock()
    client.search.return_value = [7]
    client.fetch.return_value = {7: {
        b"ENVELOPE": _fake_envelope(message_id=b"<fixture@example.test>"),
        b"FLAGS": (), b"BODYSTRUCTURE": (b"APPLICATION", b"PDF"),
    }}
    if failure == "transfer_limit":
        client.fetch.side_effect = MailRawMessageLimitError("fixture metadata limit")
    with patch("apple_mail_fast_mcp.imap_connector.IMAPClient", return_value=client):
        result = server.search_messages(account="Fixture", mailbox="INBOX", **options)
    assert result["success"] is False
    assert result["error_type"] == ("message_read_incomplete" if failure == "malformed_structure" else "raw_message_too_large")
    assert "messages" not in result and "count" not in result
    assert b"BODYSTRUCTURE" in client.fetch.call_args.args[1]
    native.assert_not_called()
    assert body_requests(client) == []
