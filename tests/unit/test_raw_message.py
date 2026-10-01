"""Transfer budget enforcement before literals are read and between ranges."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from apple_mail_fast_mcp.raw_message import (
    MailRawMessageLimitError,
    MailRawMessageReadError,
    fetch_bounded_message,
    raw_message_budget,
)


def backend(raw, *, reported_size=None):
    client = MagicMock()
    pending = [b""]
    client._imap = SimpleNamespace(read=lambda n: pending[0][:n])

    def fetch(uids, keys):
        assert uids == [7]
        if keys == [b"RFC822.SIZE"]:
            return {7: {b"RFC822.SIZE": len(raw) if reported_size is None else reported_size}}
        key = keys[0].decode()
        start, count = map(int, key.split("<")[1].rstrip(">").split("."))
        data = raw[start:start + count]
        # Exercise the pre-read literal guard even though no socket is used.
        pending[0] = data
        data = client._imap.read(len(data))
        return {7: {f"BODY[]<{start}>".encode(): data}}
    client.fetch.side_effect = fetch
    return client


def test_partial_ranges_preserve_bytes_and_do_not_set_seen():
    client = backend(b"1234567890")
    assert fetch_bounded_message(client, 7, budget=10, chunk_size=4) == b"1234567890"
    assert [c.args[1][0] for c in client.fetch.call_args_list] == [
        b"RFC822.SIZE", b"BODY.PEEK[]<0.4>", b"BODY.PEEK[]<4.4>",
        b"BODY.PEEK[]<8.2>", b"BODY.PEEK[]<10.1>",
    ]
    client.shutdown.assert_not_called()


@pytest.mark.parametrize("size", [None, True, -1, "10"])
def test_invalid_size_does_not_download_payload(size):
    client = backend(b"x")
    client.fetch.side_effect = None
    client.fetch.return_value = {7: {b"RFC822.SIZE": size}}
    with pytest.raises(MailRawMessageReadError):
        fetch_bounded_message(client, 7, budget=5)
    client.fetch.assert_called_once()


def test_oversize_preflight_stops_before_body():
    client = backend(b"123456")
    with pytest.raises(MailRawMessageLimitError):
        fetch_bounded_message(client, 7, budget=5)
    client.fetch.assert_called_once()


@pytest.mark.parametrize("reported", [2, 6])
def test_inaccurate_size_is_not_trusted(reported):
    client = backend(b"1234", reported_size=reported)
    with pytest.raises(MailRawMessageReadError):
        fetch_bounded_message(client, 7, budget=10, chunk_size=2)


def test_oversized_literal_is_rejected_before_underlying_read():
    client = backend(b"123")
    read = MagicMock(return_value=b"x" * 20)
    client._imap.read = read

    def malicious(uids, keys):
        if keys == [b"RFC822.SIZE"]:
            return {7: {b"RFC822.SIZE": 3}}
        client._imap.read(20)
        pytest.fail("oversized literal guard did not interrupt")
    client.fetch.side_effect = malicious
    with pytest.raises(MailRawMessageLimitError):
        fetch_bounded_message(client, 7, budget=5)
    read.assert_not_called()
    assert client._imap.read is read
    client.shutdown.assert_called_once()


def test_exact_budget_does_not_read_extra_byte_when_size_underreports():
    client = backend(b"123456", reported_size=5)
    with pytest.raises(MailRawMessageLimitError):
        fetch_bounded_message(client, 7, budget=5)


@pytest.mark.parametrize("value", ["", "0", "-1", "1.5", "not-a-number"])
def test_invalid_budget_is_explicit(monkeypatch, value):
    monkeypatch.setenv("APPLE_MAIL_MCP_MAX_RAW_MESSAGE_BYTES", value)
    with pytest.raises(ValueError):
        raw_message_budget()


def test_default_and_custom_budget(monkeypatch):
    monkeypatch.delenv("APPLE_MAIL_MCP_MAX_RAW_MESSAGE_BYTES", raising=False)
    assert raw_message_budget() == 500 * 1024 * 1024
    monkeypatch.setenv("APPLE_MAIL_MCP_MAX_RAW_MESSAGE_BYTES", "1234")
    assert raw_message_budget() == 1234


def test_stdlib_literal_parser_hits_guard_before_reading_large_literal():
    import imaplib
    import re

    stream = imaplib.IMAP4.__new__(imaplib.IMAP4)
    stream._mode_ascii()
    stream.tagre = re.compile(br"(?P<tag>A[0-9]+) (?P<type>[A-Z]+) (?P<data>.*)")
    stream.debug = 0
    stream.untagged_responses = {}
    stream._get_line = MagicMock(return_value=b"* 1 FETCH (UID 7 BODY[]<0> {1000000}")
    original_read = MagicMock()
    stream.read = original_read
    client = MagicMock()
    client._imap = stream

    def fetch(uids, keys):
        if keys == [b"RFC822.SIZE"]:
            return {7: {b"RFC822.SIZE": 3}}
        stream._get_response()
        pytest.fail("stdlib read guard failed")
    client.fetch.side_effect = fetch
    with pytest.raises(MailRawMessageLimitError):
        fetch_bounded_message(client, 7, budget=10)
    original_read.assert_not_called()
    client.shutdown.assert_called_once()


@pytest.mark.parametrize("row", [{}, {8: {b"BODY[]<0>": b"x"}}, {7: {b"BODY[]": b"x"}},
                                 {7: {b"BODY[]<0>": b"too-long"}}])
def test_wrong_uid_ignored_range_or_missing_body_is_not_false_absence(row):
    client = backend(b"x")
    client.fetch.side_effect = [{7: {b"RFC822.SIZE": 1}}, row]
    with pytest.raises((MailRawMessageReadError, MailRawMessageLimitError)):
        fetch_bounded_message(client, 7, budget=5)


def test_short_literal_is_incomplete_before_parser_can_fall_back():
    client = backend(b"abc")
    client._imap.read = lambda size: b"x" * max(0, size - 1)
    with pytest.raises(MailRawMessageReadError):
        fetch_bounded_message(client, 7, budget=5)
    client.shutdown.assert_called_once()


@pytest.mark.parametrize("failure", [OSError("lost bytes"), __import__("imaplib").IMAP4.abort("truncated")])
def test_guarded_protocol_failure_is_not_native_fallback_eligible(failure):
    from apple_mail_fast_mcp.mail_connector import _IMAP_FALLBACK_EXCS

    client = backend(b"abc")
    client.fetch.side_effect = [{7: {b"RFC822.SIZE": 3}}, failure]
    with pytest.raises(MailRawMessageReadError) as caught:
        fetch_bounded_message(client, 7, budget=5)
    assert caught.value.__cause__ is failure
    assert not isinstance(caught.value, _IMAP_FALLBACK_EXCS)
    client.shutdown.assert_called_once()


def test_metadata_and_payload_share_literal_allowance():
    from apple_mail_fast_mcp.raw_message import LiteralBudget, guarded_fetch

    client = backend(b"123456")
    client._imap.read = lambda n: b"x" * n
    transfer = LiteralBudget(10)
    body_fetch = client.fetch.side_effect

    def fetch(uids, keys):
        if keys == [b"ENVELOPE"]:
            client._imap.read(5)
            return {7: {b"ENVELOPE": "metadata"}}
        return body_fetch(uids, keys)
    client.fetch.side_effect = fetch
    guarded_fetch(client, 7, [b"ENVELOPE"], 10, transfer)
    assert transfer.remaining == 5
    with pytest.raises(MailRawMessageLimitError):
        fetch_bounded_message(client, 7, budget=10, transfer=transfer)
    assert client.fetch.call_count == 2  # metadata and size, no body


def test_real_parser_distinguishes_quoted_single_byte_from_counted_literal():
    from io import BytesIO

    from imapclient.response_parser import parse_fetch_response

    from apple_mail_fast_mcp.raw_message import LiteralBudget, guarded_fetch

    client = MagicMock()
    client._imap = SimpleNamespace(read=BytesIO(b"x").read)
    transfer = LiteralBudget(2)

    def fetch(uids, keys):
        literal = client._imap.read(1)
        return parse_fetch_response([
            (b'1 (UID 7 BODY[1]<0> {1}', literal),
            b' BODY[2]<0> "x")',
        ])
    client.fetch.side_effect = fetch
    rows = guarded_fetch(client, 7, [b"BODY.PEEK[1]<0.1>"], 2, transfer)
    assert rows[7][b"BODY[1]<0>"] == rows[7][b"BODY[2]<0>"] == b"x"
    assert transfer.remaining == 0


def test_quoted_last_range_cannot_allow_extra_eof_literal():
    from apple_mail_fast_mcp.raw_message import LiteralBudget

    client = backend(b"abcdx", reported_size=4)
    transfer = LiteralBudget(4)  # two metadata bytes already charged from six
    original = client.fetch.side_effect

    def fetch(uids, keys):
        if keys == [b"BODY.PEEK[]<2.2>"]:
            return {7: {b"BODY[]<2>": b"cd"}}  # quoted, no literal read
        return original(uids, keys)
    client.fetch.side_effect = fetch
    with pytest.raises(MailRawMessageLimitError):
        fetch_bounded_message(client, 7, budget=6, chunk_size=2, transfer=transfer)
    assert transfer.remaining == 0


def section_backend(body):
    client = backend(body)
    original = client.fetch.side_effect
    def fetch(uids, keys):
        key = keys[0]
        section = key.split(b"[", 1)[1].split(b"]", 1)[0]
        rows = original(uids, [key.replace(b"[" + section + b"]", b"[]")])
        return {uid: {k.replace(b"[]", b"[" + section + b"]"): v for k, v in row.items()}
                for uid, row in rows.items()}
    client.fetch.side_effect = fetch
    return client


def test_numbered_sections_use_bounded_ranges_and_share_transfer_budget():
    from apple_mail_fast_mcp.raw_message import LiteralBudget, fetch_bounded_section

    client = section_backend(b"abcdef")
    budget = LiteralBudget(10)
    assert fetch_bounded_section(client, 7, "2.1", 6, budget=10, chunk_size=2, transfer=budget) == b"abcdef"
    assert budget.remaining == 4
    assert [c.args[1][0] for c in client.fetch.call_args_list] == [
        b"BODY.PEEK[2.1]<0.2>", b"BODY.PEEK[2.1]<2.2>", b"BODY.PEEK[2.1]<4.2>", b"BODY.PEEK[2.1]<6.1>",
    ]
    client.fetch.reset_mock()
    with pytest.raises(MailRawMessageLimitError):
        fetch_bounded_section(client, 7, "3", 6, budget=10, transfer=budget)
    client.fetch.assert_not_called()


@pytest.mark.parametrize("advertised", [0, 2, 7])
def test_selected_section_inaccurate_size_is_an_error(advertised):
    from apple_mail_fast_mcp.raw_message import LiteralBudget, fetch_bounded_section

    client = section_backend(b"abcdef")
    with pytest.raises(MailRawMessageReadError):
        fetch_bounded_section(client, 7, "1", advertised, budget=10, chunk_size=2, transfer=LiteralBudget(10))


def test_selected_section_exact_budget_rejects_nonempty_eof_before_read():
    from apple_mail_fast_mcp.raw_message import LiteralBudget, fetch_bounded_section

    client = section_backend(b"abcdef")
    with pytest.raises(MailRawMessageLimitError):
        fetch_bounded_section(client, 7, "1", 5, budget=5, transfer=LiteralBudget(5))
    client.shutdown.assert_called_once()


@pytest.mark.parametrize("section", ["", "0", "1.0", "1.MIME", "1]<0.999>", "1\r\nNOOP"])
def test_invalid_section_is_rejected_before_fetch(section):
    from apple_mail_fast_mcp.raw_message import LiteralBudget, fetch_bounded_section

    client = section_backend(b"x")
    with pytest.raises(ValueError):
        fetch_bounded_section(client, 7, section, 1, budget=10, transfer=LiteralBudget(10))
    client.fetch.assert_not_called()


def test_quoted_selected_responses_share_the_per_fetch_allowance():
    from apple_mail_fast_mcp.raw_message import LiteralBudget, guarded_fetch

    client = MagicMock()
    client._imap = SimpleNamespace(read=MagicMock())
    client.fetch.return_value = {7: {b"BODY[1]<0>": b"a", b"BODY[2]<0>": b"b"}}
    with pytest.raises(MailRawMessageLimitError):
        guarded_fetch(client, 7, [b"BODY.PEEK[1]<0.1>"], 1, LiteralBudget(10))
    client.shutdown.assert_called_once()
