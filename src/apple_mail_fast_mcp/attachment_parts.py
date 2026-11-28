"""Strict, I/O-free BODYSTRUCTURE descriptors and selected-body decoding.

RFC 3501 section 7.4.2 / grammar section 9 fixes disposition positions:
base single-part 8, text 9, message/rfc822 11 (zero-based). IMAPClient's
BodyData groups multipart children at index 0. Missing or malformed fields
cannot establish attachment absence; callers may use a bounded full-message
fallback on MailMessageReadIncompleteError. Encapsulated messages always use
that canonical fallback because section bytes need not match reserialization.
"""

from __future__ import annotations

import base64
import binascii
import codecs
import re
from dataclasses import dataclass
from email import message_from_bytes, policy
from typing import Any

from .exceptions import MailMessageReadIncompleteError

_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_CONTINUATION = re.compile(r"^([^*]+)\*(\d+)(\*)?$")
_ENCODINGS = {"7bit", "8bit", "binary", "base64", "quoted-printable"}


@dataclass(frozen=True)
class AttachmentPart:
    """Canonical attachment index entry; size is transfer-encoded octets."""

    section: str
    maintype: str
    subtype: str
    filename: str
    encoding: str
    encoded_size: int

    @property
    def optimized(self) -> bool:
        """Whether BODY[section] can safely replace canonical MIME extraction."""
        return self.maintype != "message" and self.encoding in _ENCODINGS

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "name": self.filename,
            "mime_type": f"{self.maintype}/{self.subtype}",
            "size": self.encoded_size,
            "downloaded": False,
        }


def _incomplete(detail: str) -> MailMessageReadIncompleteError:
    return MailMessageReadIncompleteError(f"Incomplete attachment BODYSTRUCTURE: {detail}")


def _token(value: Any) -> str:
    if not isinstance(value, bytes):
        raise _incomplete("expected MIME token")
    try:
        text = value.decode("ascii")
    except UnicodeError as exc:
        raise _incomplete("non-ASCII MIME token") from exc
    if not _TOKEN.fullmatch(text):
        raise _incomplete("invalid MIME token")
    return text.lower()


def _parameters(value: Any) -> str:
    """Recreate parameter headers for email.policy's RFC2231/name decoding."""
    if value is None:
        return ""
    if not isinstance(value, tuple) or not value or len(value) % 2:
        raise _incomplete("malformed MIME parameters")
    fields: dict[str, str] = {}
    sequences: dict[str, list[int]] = {}
    for key, raw in zip(value[::2], value[1::2], strict=True):
        name = _token(key)
        if name in fields or not isinstance(raw, bytes):
            raise _incomplete("duplicate or non-string MIME parameter")
        try:
            text = raw.decode("utf-8")
        except UnicodeError as exc:
            raise _incomplete("undecodable MIME parameter") from exc
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
            raise _incomplete("control character in MIME parameter")
        fields[name] = text
        match = _CONTINUATION.fullmatch(name)
        if match:
            sequences.setdefault(match[1], []).append(int(match[2]))
    for name in fields:
        if name + "*" in fields:
            raise _incomplete("ambiguous plain and extended MIME parameter")
    for name, indices in sequences.items():
        if sorted(indices) != list(range(len(indices))) or name in fields or name + "*" in fields:
            raise _incomplete("ambiguous RFC2231 parameter continuation")
    return "".join(_parameter_field(name, text) for name, text in fields.items())


def _parameter_field(name: str, value: str) -> str:
    if name.endswith("*"):
        # Extended values are RFC2231 tokens, not quoted strings. Verify escape
        # syntax before stdlib's deliberately permissive percent decoder.
        if not _TOKEN.fullmatch(value) or re.search(r"%(?![0-9A-Fa-f]{2})", value):
            raise _incomplete("malformed extended MIME parameter")
        if not _CONTINUATION.fullmatch(name) or name.endswith("*0*"):
            pieces = value.split("'", 2)
            if len(pieces) != 3:
                raise _incomplete("missing extended-parameter charset/language")
            try:
                codecs.lookup(pieces[0] or "ascii")
            except LookupError as exc:
                raise _incomplete("unknown filename charset") from exc
        return f"; {name}={value}"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'; {name}="{escaped}"'


def _filename(maintype: str, subtype: str, params: Any, disposition: Any) -> tuple[str, str]:
    content_type = f"{maintype}/{subtype}" + _parameters(params)
    kind = ""
    headers = f"Content-Type: {content_type}\r\n"
    if disposition is not None:
        if not isinstance(disposition, tuple) or len(disposition) != 2:
            raise _incomplete("malformed disposition")
        kind = _token(disposition[0])
        headers += f"Content-Disposition: {kind}{_parameters(disposition[1])}\r\n"
    message = message_from_bytes((headers + "\r\n").encode(), policy=policy.default)
    filename = message.get_filename() or ""
    if "\ufffd" in filename or any(0xD800 <= ord(ch) <= 0xDFFF for ch in filename):
        raise _incomplete("undecodable filename")
    return kind, filename


def _extension_offset(s: tuple[Any, ...], maintype: str, rfc822: bool) -> int:
    extension = 10 if rfc822 else 8 if maintype == "text" else 7
    # Disposition follows MD5, which must be present even when NIL. A BODY
    # response without extensions cannot prove that no disposition existed.
    if len(s) <= extension + 1:
        raise _incomplete("missing disposition extension")
    if s[extension] is not None and not isinstance(s[extension], bytes):
        raise _incomplete("invalid MD5 extension position")
    if maintype == "text" and (type(s[7]) is not int or s[7] < 0):
        raise _incomplete("invalid text line count")
    if rfc822:
        if not isinstance(s[7], tuple) or len(s[7]) != 10 or not isinstance(s[8], tuple):
            raise _incomplete("malformed encapsulated message")
        if type(s[9]) is not int or s[9] < 0:
            raise _incomplete("invalid encapsulated message line count")
    return extension


def _leaf(s: tuple[Any, ...], section: str) -> AttachmentPart | None:
    if len(s) < 7:
        raise _incomplete("truncated leaf")
    maintype, subtype, encoding = _token(s[0]), _token(s[1]), _token(s[5])
    if maintype == "multipart":
        raise _incomplete("multipart leaf instead of grouped children")
    if type(s[6]) is not int or s[6] < 0:
        raise _incomplete("invalid encoded size")
    if any(value is not None and not isinstance(value, bytes) for value in s[3:5]):
        raise _incomplete("invalid content identifier or description")
    rfc822 = (maintype, subtype) == ("message", "rfc822")
    extension = _extension_offset(s, maintype, rfc822)
    disposition, filename = _filename(maintype, subtype, s[2], s[extension + 1])
    if disposition == "attachment" or (disposition == "inline" and filename) or rfc822:
        return AttachmentPart(section, maintype, subtype, filename, encoding, s[6])
    return None


def describe_attachment_parts(structure: Any) -> list[AttachmentPart]:
    """Return canonical descriptors, or raise if enumeration is unreliable.

    Accepts IMAPClient BodyData and equivalent grouped tuples. Unknown/raw
    tuple layouts require the caller's bounded canonical fallback; they are
    never guessed into part sections or silently treated as no attachments.
    """
    result: list[AttachmentPart] = []

    def walk(node: Any, prefix: str, depth: int) -> None:
        if depth > 64 or not isinstance(node, tuple) or not node:
            raise _incomplete("invalid or excessively nested part tree")
        if isinstance(node[0], list):
            if len(node) < 4 or not node[0]:
                raise _incomplete("missing multipart children or extensions")
            subtype = _token(node[1])
            _filename("multipart", subtype, node[2], node[3])
            for index, child in enumerate(node[0], 1):
                walk(child, f"{prefix}.{index}" if prefix else str(index), depth + 1)
        else:
            part = _leaf(node, prefix or "1")
            if part is not None:
                result.append(part)

    walk(structure, "", 0)
    return result


def decode_attachment_part(part: AttachmentPart, encoded_body: bytes) -> bytes:
    """Decode an exact complete BODY[section] literal, rejecting silent damage.

    Uses the same email policy as full-message extraction after validating
    transfer encoding. Missing/truncated data and unsupported encodings are
    incomplete reads; legitimate empty attachment payloads remain valid.
    """
    if not part.optimized or not isinstance(encoded_body, bytes):
        raise _incomplete("selected part requires canonical message fallback")
    if len(encoded_body) != part.encoded_size:
        raise _incomplete("selected part size differs from BODYSTRUCTURE")
    if part.encoding == "base64":
        compact = re.sub(rb"[ \t\r\n]", b"", encoded_body)
        try:
            base64.b64decode(compact, validate=True)
        except binascii.Error as exc:
            raise _incomplete("invalid base64 payload") from exc
    elif part.encoding == "quoted-printable" and re.search(
        rb"=(?![0-9a-fA-F]{2}|\r?\n)", encoded_body
    ):
        raise _incomplete("invalid quoted-printable payload")
    elif part.encoding == "7bit" and any(byte > 127 for byte in encoded_body):
        raise _incomplete("non-ASCII 7bit payload")
    headers = f"Content-Type: {part.maintype}/{part.subtype}\r\nContent-Transfer-Encoding: {part.encoding}\r\n\r\n"
    message = message_from_bytes(headers.encode("ascii") + encoded_body, policy=policy.default)
    decoded = message.get_payload(decode=True)
    if not isinstance(decoded, bytes) or message.defects:
        raise _incomplete("payload decoder reported malformed content")
    return decoded
