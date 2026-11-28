"""Strict IMAP part descriptors paired with canonical full-MIME decoding."""

import base64
from email import message_from_bytes, policy
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest
from imapclient.response_types import BodyData

from apple_mail_fast_mcp.attachment_parts import (
    decode_attachment_part,
    describe_attachment_parts,
)
from apple_mail_fast_mcp.draft_builder import extract_attachment_payloads
from apple_mail_fast_mcp.exceptions import MailMessageReadIncompleteError


def leaf(
    maintype=b"APPLICATION",
    subtype=b"PDF",
    *,
    params=None,
    disposition=b"attachment",
    filename=b"report.pdf",
    encoding=b"BASE64",
    body=b"cGRm",
    extras=(),
):
    fields = (maintype, subtype, params, None, None, encoding, len(body))
    if maintype == b"TEXT":
        fields += (1,)
    elif (maintype, subtype) == (b"MESSAGE", b"RFC822"):
        fields += ((None,) * 10, leaf(maintype=b"TEXT", subtype=b"PLAIN"), 1)
    disp = (disposition, (b"filename", filename) if filename else None) if disposition else None
    return fields + (None, disp) + extras


@pytest.fixture
def paired_parts():
    root = MIMEMultipart("mixed")
    root.attach(MIMEText("body"))
    related = MIMEMultipart("related")
    related.attach(MIMEText('<img src="cid:logo">', "html"))
    image = MIMEImage(b"\x89PNG\x00fixture", _subtype="png")
    image.set_param("name", "logo.png")
    image.add_header("Content-Disposition", "inline")
    related.attach(image)
    root.attach(related)
    alternative = MIMEMultipart("alternative")
    alternative.attach(MIMEText("nested body"))
    pdf = MIMEApplication(b"%PDF-1.7\x00fixture", _subtype="pdf")
    pdf.add_header("Content-Disposition", "attachment", filename="résumé.pdf")
    alternative.attach(pdf)
    root.attach(alternative)
    text = MIMEText("Hello 世界", _charset="utf-8")
    text.add_header("Content-Disposition", "attachment", filename="notes.txt")
    root.attach(text)
    ordinary = MIMEApplication(b"", _subtype="octet-stream")
    ordinary.add_header("Content-Disposition", "attachment", filename="empty.bin")
    root.attach(ordinary)
    bodies = [p.get_payload().encode("ascii") for p in [image, pdf, text, ordinary]]
    wire_structure = (
        leaf(b"TEXT", b"PLAIN", disposition=None, encoding=b"7BIT", body=b"body"),
        (
            leaf(b"TEXT", b"HTML", disposition=None),
            leaf(
                b"IMAGE",
                b"PNG",
                params=(b"name", b"logo.png"),
                disposition=b"inline",
                filename=None,
                body=bodies[0],
            ),
            b"RELATED",
            None,
            None,
        ),
        (
            leaf(b"TEXT", b"PLAIN", disposition=None),
            leaf(filename=None, body=bodies[1])[:8]
            + (
                (
                    b"attachment",
                    (b"filename*0*", b"utf-8''r%C3%A9", b"filename*1*", b"sum%C3%A9.pdf"),
                ),
            ),
            b"ALTERNATIVE",
            None,
            None,
        ),
        leaf(b"TEXT", b"PLAIN", filename=b"notes.txt", body=bodies[2]),
        leaf(subtype=b"OCTET-STREAM", filename=b"empty.bin", body=bodies[3]),
        b"MIXED",
        None,
        None,
    )
    return root.as_bytes(), BodyData.create(wire_structure), bodies


def test_paired_metadata_sections_names_order_and_payload_bytes(paired_parts):
    raw, structure, bodies = paired_parts
    canonical = extract_attachment_payloads(raw)
    parts = describe_attachment_parts(structure)
    assert [p.section for p in parts] == ["2.2", "3.2", "4", "5"]
    assert [p.filename for p in parts] == [a[0] for a in canonical]
    assert [p.metadata for p in parts] == [
        {"name": a[0], "mime_type": f"{a[1]}/{a[2]}", "size": len(body), "downloaded": False}
        for a, body in zip(canonical, bodies, strict=True)
    ]
    assert all(p.optimized for p in parts)
    assert [decode_attachment_part(p, b) for p, b in zip(parts, bodies, strict=True)] == [
        a[3] for a in canonical
    ]


def test_root_single_part_and_disposition_filename_precedence():
    (part,) = describe_attachment_parts(BodyData.create(leaf(params=(b"name", b"fallback.pdf"))))
    assert part.section == "1"
    assert part.filename == "report.pdf"
    assert part.encoding == "base64"
    assert part.encoded_size == 4


@pytest.mark.parametrize(
    "params, expected",
    [
        ((b"filename", "世界.txt".encode()), "世界.txt"),
        ((b"filename*", b"utf-8''%E4%B8%96%E7%95%8C.txt"), "世界.txt"),
        ((b"filename", b"=?utf-8?b?5LiW55WMLnR4dA==?="), "世界.txt"),
    ],
)
def test_unicode_filename_uses_email_policy(params, expected):
    fields = leaf()[:8] + ((b"attachment", params),)
    assert describe_attachment_parts(BodyData.create(fields))[0].filename == expected


def test_rfc822_is_one_attachment_and_requires_canonical_fallback():
    structure = BodyData.create(
        (
            leaf(),
            leaf(b"MESSAGE", b"RFC822", filename=b"forwarded.eml", encoding=b"7BIT"),
            b"MIXED",
            None,
            None,
        )
    )
    parts = describe_attachment_parts(structure)
    assert [p.section for p in parts] == ["1", "2"]
    assert parts[1].filename == "forwarded.eml"
    assert not parts[1].optimized
    with pytest.raises(MailMessageReadIncompleteError):
        decode_attachment_part(parts[1], b"cGRm")


def test_no_attachment_is_a_confirmed_empty_list():
    assert (
        describe_attachment_parts(BodyData.create(leaf(b"TEXT", b"PLAIN", disposition=None))) == []
    )


@pytest.mark.parametrize(
    "structure",
    [
        None,
        (),
        ([], b"MIXED", None, None),
        (b"APPLICATION",),
        leaf()[:7],  # BODY omitted extension fields: absence cannot prove no files.
        leaf()[:7] + ((b"attachment", (b"filename", b"wrong-offset.pdf")),),
        leaf()[:8] + ((b"attachment", (b"filename",)),),
        leaf()[:8] + ((b"attachment", (b"filename*1*", b"missing-prefix.pdf")),),
        leaf()[:8] + ((b"attachment", (b"filename", b"a", b"filename", b"b")),),
        leaf()[:8] + ((b"attachment", (b"filename", b"a", b"filename*", b"utf-8''b")),),
        leaf()[:8] + ((b"attachment", (b"filename*", b"utf-8''bad%XX")),),
        leaf()[:8] + ((b"attachment", (b"filename*", b"missing-prefix")),),
        leaf()[:8] + ((b"attachment", (b"filename*", b"unknown-charset''file")),),
        leaf()[:8] + ((b"attachment", (b"filename", b"header\nInjection")),),
        leaf()[:3] + (42,) + leaf()[4:],
        leaf()[:6] + (-1,) + leaf()[7:],
        ([leaf(), None], b"MIXED", None, None),
    ],
)
def test_ambiguous_structures_are_incomplete_not_empty(structure):
    with pytest.raises(MailMessageReadIncompleteError):
        describe_attachment_parts(structure)


def test_future_extensions_cannot_masquerade_as_disposition():
    # RFC language/location/future fields do not replace the actual disposition.
    structure = leaf(
        disposition=None, extras=(None, None, (b"attachment", (b"filename", b"fake.pdf")))
    )
    assert describe_attachment_parts(structure) == []


@pytest.mark.parametrize(
    "encoding, body",
    [
        (b"BASE64", b"!!!"),
        (b"BASE64", b"YQ"),
        (b"QUOTED-PRINTABLE", b"bad=XX"),
        (b"QUOTED-PRINTABLE", b"bad="),
        (b"7BIT", b"\xff"),
        (b"X-UNKNOWN", b"payload"),
    ],
)
def test_corrupt_or_unsupported_selected_payload_is_incomplete(encoding, body):
    (part,) = describe_attachment_parts(leaf(encoding=encoding, body=body))
    with pytest.raises(MailMessageReadIncompleteError):
        decode_attachment_part(part, body)


@pytest.mark.parametrize(
    "encoding, body",
    [
        (b"BASE64", base64.encodebytes(b"\x00binary\xff")),
        (b"QUOTED-PRINTABLE", b"Hello=20world=0A"),
        (b"7BIT", b"plain\r\n"),
        (b"8BIT", "世界".encode()),
        (b"BINARY", b"\x00\xff"),
        (b"BASE64", b""),
    ],
)
def test_selected_decoder_matches_full_email_policy(encoding, body):
    (part,) = describe_attachment_parts(leaf(encoding=encoding, body=body))
    raw = (
        b"Content-Type: application/pdf\r\nContent-Transfer-Encoding: "
        + encoding
        + b"\r\n\r\n"
        + body
    )
    expected = message_from_bytes(raw, policy=policy.default).get_payload(decode=True)
    assert decode_attachment_part(part, body) == expected
    with pytest.raises(MailMessageReadIncompleteError):
        decode_attachment_part(part, body + b"x")


def test_nameless_attachment_keeps_empty_metadata_name():
    (part,) = describe_attachment_parts(leaf(filename=None))
    assert part.filename == ""
    assert part.metadata["name"] == ""
    assert decode_attachment_part(part, b"cGRm") == b"pdf"
