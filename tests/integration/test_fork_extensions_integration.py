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

    `search(has_attachment=True, include_attachments=True)` and the connector's
    byte-fetch enumeration can disagree for some real messages (inline images /
    message-rfc822 parts get reported by search but aren't byte-fetchable at
    index 0). So we probe fetchability with a real `get_attachment_content`
    and pick the first message that genuinely yields bytes — otherwise the
    fork-patch assertions would fail for reasons unrelated to the patches.

    Skips (does not fail) when the account has no fetchable attachment.
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
