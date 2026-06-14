"""Integration tests for ImapConnector against real iCloud.

Guarded by ``MAIL_TEST_MODE=true``. Requires a Keychain entry:

    security add-generic-password \\
        -s "apple-mail-mcp.imap.iCloud" \\
        -a "s.morgan.jeffries@icloud.com" \\
        -w "<APP_PASSWORD>" -T "" -U

Run:

    MAIL_TEST_MODE=true MAIL_TEST_ACCOUNT=iCloud \\
        uv run pytest tests/integration/test_imap_connector.py -v
"""

from __future__ import annotations

import os

import pytest

from apple_mail_mcp.exceptions import MailKeychainEntryNotFoundError
from apple_mail_mcp.imap_connector import ImapConnector
from apple_mail_mcp.keychain import get_imap_password

ICLOUD_HOST = "imap.mail.me.com"
ICLOUD_PORT = 993
ICLOUD_ACCOUNT_NAME = "iCloud"
ICLOUD_EMAIL = "s.morgan.jeffries@icloud.com"


def _test_mode_enabled() -> bool:
    return os.getenv("MAIL_TEST_MODE") == "true"


@pytest.mark.integration
@pytest.mark.skipif(not _test_mode_enabled(), reason="MAIL_TEST_MODE != 'true'")
class TestEndToEndICloud:
    def test_end_to_end_search_returns_list(self):
        password = get_imap_password(ICLOUD_ACCOUNT_NAME, ICLOUD_EMAIL)
        connector = ImapConnector(
            ICLOUD_HOST, ICLOUD_PORT, ICLOUD_EMAIL, password
        )
        result = connector.search_messages(limit=5)
        assert isinstance(result, list)
        # May be empty (per PR #70 spike finding — merged-away Apple ID's
        # residual mailbox). Any non-empty result must have the standard
        # keys matching mail_connector.search_messages output shape.
        expected_keys = {
            "id",
            "subject",
            "sender",
            "date_received",
            "read_status",
            "flagged",
        }
        for msg in result:
            assert set(msg.keys()) == expected_keys
            assert isinstance(msg["read_status"], bool)
            assert isinstance(msg["flagged"], bool)

    def test_keychain_entry_missing_raises_entry_not_found(self):
        with pytest.raises(MailKeychainEntryNotFoundError):
            get_imap_password("DoesNotExistAccount", "nobody@example.com")


@pytest.mark.integration
@pytest.mark.skipif(not _test_mode_enabled(), reason="MAIL_TEST_MODE != 'true'")
class TestBatchedMessageIdSearchLive:
    """P2: bulk mutations resolve Message-IDs via one OR-chained SEARCH per
    chunk. Mocks can't prove a real server accepts the OR-chain wire syntax;
    this read-only check does. Resolves the LOCAL iCloud account so it's not
    tied to the upstream-hardcoded address."""

    def test_or_chain_resolves_real_message_ids_in_one_search(self) -> None:
        from apple_mail_mcp.imap_connector import _resolve_message_id_uids
        from apple_mail_mcp.mail_connector import AppleMailConnector

        account = os.getenv("MAIL_SMOKE_ACCOUNT", "iCloud")
        mc = AppleMailConnector()
        try:
            host, port, email = mc._resolve_imap_config(account)
            password = get_imap_password(account, email)
        except MailKeychainEntryNotFoundError:
            pytest.skip(f"No Keychain entry for {account!r}")

        conn = ImapConnector(host, port, email, password)
        with conn._session() as client:
            client.select_folder("INBOX", readonly=True)  # no mutation
            uids = client.search(["ALL"])
            sample = uids[-3:]
            if not sample:
                pytest.skip("INBOX empty; nothing to resolve")

            resp = client.fetch(sample, ["ENVELOPE"])
            mids = []
            for data in resp.values():
                mid = data[b"ENVELOPE"].message_id
                mids.append(mid.decode() if isinstance(mid, bytes) else mid)

            calls = {"n": 0}
            real_search = client.search

            def counting_search(criteria, *a, **k):  # type: ignore[no-untyped-def]
                calls["n"] += 1
                return real_search(criteria, *a, **k)

            client.search = counting_search  # type: ignore[method-assign]
            resolved = _resolve_message_id_uids(client, mids)

        # Real server accepted the OR-chain and returned the right UIDs...
        assert sorted(resolved) == sorted(sample)
        # ...in a single round-trip, not one-per-id.
        assert calls["n"] == 1
