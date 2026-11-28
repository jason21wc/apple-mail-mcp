"""Selective MIME decoding agrees with paired BODYSTRUCTURE attachment indices."""

import base64
from email.message import EmailMessage
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.message import MIMEMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from unittest.mock import patch

import pytest

from apple_mail_fast_mcp import draft_builder
from apple_mail_fast_mcp.draft_builder import extract_attachment_payloads
from apple_mail_fast_mcp.exceptions import (
    MailAttachmentIndexError,
    MailMessageReadIncompleteError,
)
from apple_mail_fast_mcp.imap_connector import _bodystructure_extract_attachments


def metadata_leaf(maintype, subtype, filename=None, disposition=None):
    params = (b"filename", filename.encode()) if filename else None
    fields = (maintype.encode(), subtype.encode(), None, None, None, b"7bit", 0)
    if maintype == "text":
        fields += (0,)
    elif (maintype, subtype) == ("message", "rfc822"):
        fields += ((None,) * 10, metadata_leaf("text", "plain"), 0)
    return fields + (None, (disposition.encode(), params) if disposition else None)


@pytest.fixture
def paired_message():
    """One document with inline, nested, Unicode, and encapsulated parts."""
    outer = MIMEMultipart("mixed")
    related = MIMEMultipart("related")
    related.attach(MIMEText('<img src="cid:logo">', "html"))
    image = MIMEImage(b"\x89PNG\x00fixture", _subtype="png")
    image.add_header("Content-ID", "<logo>")
    image.add_header("Content-Disposition", "inline", filename="logo.png")
    related.attach(image)
    invisible = MIMEImage(b"unnamed inline", _subtype="png")
    invisible.add_header("Content-Disposition", "inline")
    related.attach(invisible)
    outer.attach(related)

    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText("body"))
    mixed = MIMEMultipart("mixed")
    mixed.attach(MIMEText("nested body"))
    pdf = MIMEApplication(b"%PDF-1.7\x00nested", _subtype="pdf")
    pdf.add_header("Content-Disposition", "attachment", filename="résumé.pdf")
    mixed.attach(pdf)
    empty = MIMEApplication(b"", _subtype="octet-stream")
    empty.add_header("Content-Disposition", "attachment")
    mixed.attach(empty)
    alternative.attach(mixed)
    outer.attach(alternative)

    text = MIMEText("Hello 世界", _charset="utf-8")
    text.add_header("Content-Disposition", "attachment", filename="notes-世界.txt")
    outer.attach(text)
    inner = EmailMessage()
    inner["Subject"] = "Forwarded fixture"
    inner.set_content("Forwarded body")
    inner.add_attachment(b"inner PDF", maintype="application", subtype="pdf", filename="inner.pdf")
    # Freeze the nested MIME boundary before serializing either expectation.
    inner_bytes = inner.as_bytes()
    forwarded = MIMEMessage(inner)
    forwarded.add_header("Content-Disposition", "attachment", filename="forwarded.eml")
    outer.attach(forwarded)
    expected = [
        ("logo.png", "image", "png", b"\x89PNG\x00fixture"),
        ("résumé.pdf", "application", "pdf", b"%PDF-1.7\x00nested"),
        ("attachment", "application", "octet-stream", b""),
        ("notes-世界.txt", "text", "plain", "Hello 世界".encode()),
        ("forwarded.eml", "message", "rfc822", inner_bytes),
    ]
    structure = ([
        ([metadata_leaf("text", "html"),
          metadata_leaf("image", "png", "logo.png", "inline"),
          metadata_leaf("image", "png", disposition="inline")], b"related", None, None),
        ([metadata_leaf("text", "plain"),
          ([metadata_leaf("text", "plain"),
            metadata_leaf("application", "pdf", "résumé.pdf", "attachment"),
            metadata_leaf("application", "octet-stream", disposition="attachment")], b"mixed", None, None)], b"alternative", None, None),
        metadata_leaf("text", "plain", "notes-世界.txt", "attachment"),
        metadata_leaf("message", "rfc822", "forwarded.eml", "attachment"),
    ], b"mixed", None, None)
    return outer.as_bytes(), expected, structure


def test_default_and_selective_payloads_match_paired_metadata(paired_message):
    raw, expected, structure = paired_message
    metadata = _bodystructure_extract_attachments(structure)
    assert [(m["name"] or "attachment", m["mime_type"]) for m in metadata] == [
        (name, f"{maintype}/{subtype}") for name, maintype, subtype, _ in expected
    ]
    assert extract_attachment_payloads(raw) == expected
    for index in range(len(expected)):
        assert extract_attachment_payloads(raw, indices=[index]) == [expected[index]]
    # Unordered, repeated requests keep canonical MIME order and decode once.
    assert extract_attachment_payloads(raw, indices=[4, 1, 4, 0]) == [expected[i] for i in (0, 1, 4)]
    assert extract_attachment_payloads(raw, indices=iter([3, 2])) == expected[2:4]


def test_unselected_parts_never_reach_payload_decoder(paired_message):
    raw, expected, _ = paired_message
    original = draft_builder._attachment_payload_bytes

    def decode_selected(part):
        assert part.get_filename() == "résumé.pdf", "unselected payload was decoded"
        return original(part)

    with patch.object(draft_builder, "_attachment_payload_bytes", side_effect=decode_selected) as decode:
        assert extract_attachment_payloads(raw, indices=[1]) == [expected[1]]
        decode.assert_called_once()
    with patch.object(draft_builder, "_attachment_payload_bytes") as decode:
        assert extract_attachment_payloads(raw, indices=[]) == []
        decode.assert_not_called()


@pytest.mark.parametrize("indices", [[-1], [5], [0, 99], [True], [1.0], ["1"], [None]])
def test_invalid_selection_fails_before_any_payload_decode(paired_message, indices):
    raw, _, _ = paired_message
    with patch.object(draft_builder, "_attachment_payload_bytes") as decode:
        with pytest.raises(MailAttachmentIndexError):
            extract_attachment_payloads(raw, indices=indices)
        decode.assert_not_called()


def test_no_attachments_is_not_a_valid_index():
    raw = b"Subject: no attachments\n\nbody"
    assert extract_attachment_payloads(raw) == []
    assert extract_attachment_payloads(raw, indices=[]) == []
    with pytest.raises(MailAttachmentIndexError):
        extract_attachment_payloads(raw, indices=[0])


@pytest.mark.parametrize("encoding", ["base64", "quoted-printable"])
def test_malformed_encoded_forward_is_incomplete_but_unselected_can_be_skipped(encoding):
    payload = base64.b64encode(b"Subject: forwarded\n\nbody") if encoding == "base64" else b"Subject: forwarded=0A=0Abody"
    raw = (
        b'Content-Type: multipart/mixed; boundary="b"\n\n'
        b'--b\nContent-Type: application/octet-stream\nContent-Disposition: attachment; filename="empty.bin"\n\n'
        b'\n--b\nContent-Type: message/rfc822\nContent-Transfer-Encoding: ' + encoding.encode() +
        b'\nContent-Disposition: attachment; filename="bad.eml"\n\n' + payload + b'\n--b--\n'
    )
    assert extract_attachment_payloads(raw, indices=[0]) == [("empty.bin", "application", "octet-stream", b"")]
    with pytest.raises(MailMessageReadIncompleteError):
        extract_attachment_payloads(raw, indices=[1])
    with pytest.raises(MailMessageReadIncompleteError):
        extract_attachment_payloads(raw)
