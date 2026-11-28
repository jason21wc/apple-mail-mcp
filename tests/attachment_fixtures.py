"""Explicit payload API doubles for connector-only tests (no transport I/O)."""

from apple_mail_fast_mcp.draft_builder import count_attachment_parts, extract_attachment_payloads


def selected_payloads_from_raw(raw):
    def fetch(message_id, mailbox="INBOX", *, indices=None):
        count = count_attachment_parts(raw)
        selected = list(range(count)) if indices is None else sorted({i for i in indices if type(i) is int and 0 <= i < count})
        payloads = extract_attachment_payloads(raw, indices=selected)
        return count, dict(zip(selected, payloads, strict=True))
    return fetch
