"""Bound raw MIME transfer before parsing; this is not a process-memory cap.

IMAPClient uses imaplib.read(size) for server literals. The scoped guard below
checks that size *before* the literal is allocated/read. Keep its adapter tests
when upgrading IMAPClient or Python. Every caller must own the connection lease.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, cast

from imapclient.exceptions import IMAPClientError

from .exceptions import MailAttachmentTooLargeError, MailMessageReadIncompleteError

DEFAULT_RAW_MESSAGE_BYTES = 500 * 1024 * 1024
RAW_CHUNK_BYTES = 1024 * 1024
_BUDGET_ENV = "APPLE_MAIL_MCP_MAX_RAW_MESSAGE_BYTES"


class MailRawMessageLimitError(MailAttachmentTooLargeError):
    """Raw transfer exceeded its budget; never retry with an unbounded path."""


class MailRawMessageReadError(MailMessageReadIncompleteError):
    """Size/range response was incomplete or inconsistent, not proven absence."""


def raw_message_budget() -> int:
    value = os.environ.get(_BUDGET_ENV)
    if value is None:
        return DEFAULT_RAW_MESSAGE_BYTES
    try:
        limit = int(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{_BUDGET_ENV} must be a positive integer byte count") from exc
    if limit <= 0:
        raise ValueError(f"{_BUDGET_ENV} must be a positive integer byte count")
    return limit


class _CountedLiteral(bytes):
    """Unique provenance for literal bytes, including Python's interned bytes."""


@dataclass
class LiteralBudget:
    """Shared remaining literal allowance for one bounded read operation."""

    remaining: int


def guarded_fetch(
    client: Any, uid: int, keys: list[bytes], allowance: int,
    transfer: LiteralBudget,
) -> dict[Any, Any]:
    """Bound all literals in one FETCH, including unsolicited literal data."""
    stream = client._imap
    original_read = stream.read
    remaining = min(allowance, transfer.remaining)

    def limited_read(size: int) -> bytes:
        nonlocal remaining
        if not isinstance(size, int) or size < 0 or size > remaining:
            raise MailRawMessageLimitError("IMAP literal exceeds the remaining raw-message transfer allowance")
        data = original_read(size)
        if not isinstance(data, bytes) or len(data) != size:
            raise MailRawMessageReadError("IMAP literal reader returned inconsistent bytes")
        remaining -= len(data)
        transfer.remaining -= len(data)
        return _CountedLiteral(data)

    stream.read = limited_read
    try:
        rows = cast(dict[Any, Any], client.fetch([uid], keys))
        _charge_quoted_bodies(rows, transfer)
        return rows
    except (MailRawMessageLimitError, MailRawMessageReadError, IMAPClientError, OSError) as exc:
        # An interrupted literal leaves the stream unsynchronized. Close it
        # without sending LOGOUT into unread payload, preserving the cause.
        try:
            client.shutdown()
        except Exception:
            pass
        if isinstance(exc, (IMAPClientError, OSError)):
            raise MailRawMessageReadError("IMAP bounded retrieval did not complete") from exc
        raise
    finally:
        stream.read = original_read


def _charge_quoted_bodies(rows: dict[Any, Any], transfer: LiteralBudget) -> None:
    """Quoted BODY nstrings bypass imaplib.read; count them before another FETCH.

    IMAPClient preserves literal byte objects. The dedicated subclass avoids
    mistaking an interned one-byte quoted string for a charged literal. Protocol
    framing remains subject to imaplib's line limit, not this payload budget.
    """
    for row in rows.values():
        if not isinstance(row, dict):
            continue
        for key, value in row.items():
            is_body = isinstance(key, bytes) and (key.startswith(b"BODY[") or key in (
                b"RFC822", b"RFC822.TEXT", b"RFC822.HEADER",
            ))
            if is_body and isinstance(value, bytes) and not isinstance(value, _CountedLiteral):
                if len(value) > transfer.remaining:
                    raise MailRawMessageLimitError("Quoted IMAP body exceeds the remaining raw-message allowance")
                transfer.remaining -= len(value)


def _size(client: Any, uid: int, budget: int, transfer: LiteralBudget) -> int:
    rows = guarded_fetch(client, uid, [b"RFC822.SIZE"], 0, transfer)
    entry = rows.get(uid)
    if not isinstance(entry, dict):
        raise MailRawMessageReadError("IMAP omitted the selected UID from size preflight")
    size = entry.get(b"RFC822.SIZE")
    if type(size) is not int or size <= 0:
        raise MailRawMessageReadError("IMAP did not provide a valid positive RFC822.SIZE for the selected UID")
    if size > min(budget, transfer.remaining):
        raise MailRawMessageLimitError("Message exceeds the configured raw-message transfer budget")
    return size


def _chunk(
    client: Any, uid: int, offset: int, count: int, allowance: int, transfer: LiteralBudget,
) -> bytes:
    rows = guarded_fetch(client, uid, [f"BODY.PEEK[]<{offset}.{count}>".encode()], allowance, transfer)
    entry = rows.get(uid)
    if not isinstance(entry, dict):
        raise MailRawMessageReadError("IMAP omitted the selected UID from body retrieval")
    # RFC9051 requires origin even when the range contains the entire message.
    data = entry.get(f"BODY[]<{offset}>".encode())
    if not isinstance(data, bytes) or len(data) > count:
        raise MailRawMessageReadError("IMAP returned a missing, oversized, or inconsistent body range")
    return data


def fetch_bounded_message(
    client: Any, uid: int, *, budget: int | None = None, chunk_size: int = RAW_CHUNK_BYTES,
    transfer: LiteralBudget | None = None,
) -> bytes:
    """Read one exact UID using size preflight and cumulatively bounded PEEK.

    Verify EOF even at the advertised size. At an exact budget boundary, a
    nonempty trailing literal is rejected before reading its bytes. Missing or
    inaccurate sizes are errors; no full-message fallback is allowed.
    """
    limit = raw_message_budget() if budget is None else budget
    if type(limit) is not int or limit <= 0 or type(chunk_size) is not int or chunk_size <= 0:
        raise ValueError("Raw-message budget and chunk size must be positive integer byte counts")
    transfer = transfer if transfer is not None else LiteralBudget(limit)
    size = _size(client, uid, limit, transfer)
    data = bytearray()
    while len(data) < size:
        count = min(chunk_size, size - len(data))
        piece = _chunk(client, uid, len(data), count, min(count, limit - len(data)), transfer)
        if len(piece) != count:
            raise MailRawMessageReadError("IMAP body ended before its advertised RFC822.SIZE")
        data.extend(piece)
    if _chunk(client, uid, size, 1, min(1, limit - size), transfer):
        raise MailRawMessageReadError("IMAP body is longer than its advertised RFC822.SIZE")
    return bytes(data)
