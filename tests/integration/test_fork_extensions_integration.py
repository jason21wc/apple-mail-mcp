"""Live integration coverage for the fork-unique extensions.

After the re-baseline onto upstream v0.10.2, the fork keeps only two
behavioral mods, both at the server layer. Upstream's integration suite
exercises the connector's attachment paths live, but it cannot cover these
fork params/markers — so this file fills that exact gap with real round-trips:

- ``save_attachments`` ``output_filename`` (fork mod #2) — the file lands under
  the caller-chosen name (the tempdir-then-move logic against a real fetch).
- ``content_is_untrusted`` / ``security_notice`` on ``get_messages`` +
  ``get_attachment_content`` (fork PR #37) — present on real responses, with
  content returned verbatim.

These are server-layer, so the tests drive the server tools (not the
connector). Read-only on mail; the only write is to a pytest tmp_path.

Run with: MAIL_TEST_ACCOUNT=iCloud pytest \
    tests/integration/test_fork_extensions_integration.py --run-integration

Requires a configured account whose INBOX has at least one attachment-bearing
message (the test skips cleanly otherwise). The connector reaches it via the
IMAP fast path (Keychain creds) or the AppleScript fallback.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from apple_mail_mcp.server import (
    get_attachment_content,
    get_messages,
    save_attachments,
    search_messages,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        "not config.getoption('--run-integration')",
        reason="Integration tests disabled by default. Use --run-integration.",
    ),
]


@pytest.fixture
def test_account() -> str:
    """Account to exercise (matches the upstream integration convention)."""
    return os.getenv("MAIL_TEST_ACCOUNT", "iCloud")


@pytest.fixture
def attachment_message(test_account: str) -> tuple[dict[str, Any], str]:
    """A real INBOX message whose attachment at index 0 is actually fetchable.

    The metadata enumeration (search/get_attachments) and the byte-fetch
    enumeration (get_attachment_content) historically disagreed on inline
    images / alternative-nested parts; that index-contract divergence is now
    fixed by ``draft_builder.extract_attachment_payloads`` and asserted by
    ``TestAttachmentEnumerationContract`` below. We still probe for a message
    that yields bytes at index 0 (skips, doesn't fail) so an account whose
    only attachments are over the inline byte-cap doesn't fail these
    fork-patch assertions for unrelated reasons.
    """
    res = search_messages(
        account=test_account,
        mailbox="INBOX",
        has_attachment=True,
        limit=15,
        include_attachments=True,
    )
    if not res.get("success"):
        pytest.skip(f"search_messages failed on {test_account}: {res.get('error')}")
    for msg in res.get("messages") or []:
        mid = msg.get("id") or msg.get("message_id")
        if not (msg.get("attachments") and mid):
            continue
        probe = get_attachment_content(mid, 0, account=test_account, mailbox="INBOX")
        if probe.get("success") and probe.get("content"):
            return msg, test_account
    pytest.skip(f"No message with a fetchable index-0 attachment in {test_account}/INBOX")


class TestForkExtensionsLive:
    def test_save_attachments_output_filename_writes_custom_name(
        self,
        attachment_message: tuple[dict[str, Any], str],
        tmp_path: Path,
    ) -> None:
        msg, account = attachment_message
        mid = msg.get("id") or msg["message_id"]

        result = save_attachments(
            mid,
            str(tmp_path),
            attachment_indices=[0],
            output_filename="FORK_LIVE_REPORT.dat",
            account=account,
            mailbox="INBOX",
        )

        assert result["success"] is True, result
        # If the single attachment wasn't byte-cap-rejected, it lands under the
        # custom name; if it was rejected, saved==0 and no filename (still ok).
        if result["saved"] == 1:
            assert result["filename"] == "FORK_LIVE_REPORT.dat"
            written = tmp_path / "FORK_LIVE_REPORT.dat"
            assert written.exists()
            assert written.stat().st_size > 0
            # Nothing else leaked into the destination.
            assert [p.name for p in tmp_path.iterdir() if p.is_file()] == [
                "FORK_LIVE_REPORT.dat"
            ]
        else:
            assert "filename" not in result
            assert result.get("rejected")

    def test_get_messages_carries_untrusted_marker(
        self, attachment_message: tuple[dict[str, Any], str]
    ) -> None:
        msg, account = attachment_message
        mid = msg.get("id") or msg["message_id"]

        result = get_messages(
            [mid], account=account, mailbox="INBOX", include_attachments=True
        )

        assert result["success"] is True, result
        assert result["count"] >= 1
        assert result["content_is_untrusted"] is True
        assert result["security_notice"]

    def test_get_attachment_content_carries_untrusted_marker(
        self, attachment_message: tuple[dict[str, Any], str]
    ) -> None:
        msg, account = attachment_message
        mid = msg.get("id") or msg["message_id"]

        result = get_attachment_content(mid, 0, account=account, mailbox="INBOX")

        assert result["success"] is True, result
        assert result["content_is_untrusted"] is True
        assert result["security_notice"]
        # Content is returned verbatim (non-empty for a real attachment).
        assert result["content"]
        assert result["encoding"] in ("text", "base64")


class TestAttachmentEnumerationContract:
    """Every attachment_index the metadata list advertises must resolve to the
    SAME part on the byte-fetch path.

    The IMAP metadata list (search / get_attachments / get_messages, via
    BODYSTRUCTURE) and the byte-fetch list (get_attachment_content /
    save_attachments) are produced by different code over different IMAP
    responses but share one 0-based index. Pre-fix they diverged on
    multipart/related inline images (dropped) and parts nested under a
    multipart/alternative (skipped) — so an advertised index returned the
    WRONG part's bytes or went out of range. This is the live guard a unit
    test can't be (the real BODYSTRUCTURE-vs-raw split only exists on a
    server). See draft_builder.extract_attachment_payloads.
    """

    def test_every_advertised_index_resolves_to_the_matching_part(
        self, test_account: str
    ) -> None:
        res = search_messages(
            account=test_account,
            mailbox="INBOX",
            has_attachment=True,
            limit=20,
            include_attachments=True,
        )
        if not res.get("success"):
            pytest.skip(f"search failed on {test_account}: {res.get('error')}")

        checked = 0
        for msg in res.get("messages") or []:
            mid = msg.get("id") or msg.get("message_id")
            atts = msg.get("attachments") or []
            if not (mid and atts):
                continue
            checked += 1
            for i, meta in enumerate(atts):
                got = get_attachment_content(
                    mid, i, account=test_account, mailbox="INBOX"
                )
                # The index the listing advertised must never be out of range.
                assert got.get("error_type") != "attachment_index_out_of_range", (
                    f"msg {mid} index {i}/{len(atts)} out of range: {got}"
                )
                # When it fetches, it must be the SAME part the listing named
                # (not a neighbour's bytes under a shifted index).
                if got.get("success"):
                    assert got.get("mime_type") == meta.get("mime_type"), (
                        f"msg {mid} index {i}: listing={meta} fetched={got}"
                    )
        if checked == 0:
            pytest.skip(
                f"No attachment-bearing messages in {test_account}/INBOX"
            )
