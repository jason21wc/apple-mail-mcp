"""Real-execution smoke suite.

The whole point of this file: actually RUN osascript against the local
Mail.app (and IMAP against the real account) so the bug class that mocked
unit tests structurally cannot catch is exercised before code ships —
AppleScript -10000 property errors, record-vs-list result shapes, and
"does save actually write a file". Every one of those shipped to main in
2026-05 because no AppleScript executed in CI. This suite is what the
pre-push hook runs when the connector changes.

It is deliberately resilient: if Mail.app isn't reachable, the account
isn't configured, or there's no attachment-bearing message to probe, each
test SKIPS (so the hook never blocks a push on a machine without the
setup). On the dogfooding Mac it runs for real.

Run: `make smoke`  (or `pytest tests/integration/test_smoke.py --run-integration`).
Account: override with MAIL_SMOKE_ACCOUNT (default "iCloud").
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from apple_mail_mcp.mail_connector import AppleMailConnector

pytestmark = pytest.mark.skipif(
    "not config.getoption('--run-integration')",
    reason="Smoke suite runs real Mail.app/IMAP. Use --run-integration (make smoke).",
)


def _account() -> str:
    return os.getenv("MAIL_SMOKE_ACCOUNT", "iCloud")


@pytest.fixture(scope="module")
def connector() -> AppleMailConnector:
    return AppleMailConnector(timeout=90)


@pytest.fixture(scope="module")
def target(connector: AppleMailConnector) -> dict[str, Any]:
    """Resolve a real attachment-bearing message via the AppleScript path.

    Returns account/mailbox + numeric id (AppleScript path) + rfc id (IMAP
    path) + first-attachment download state. Skips the whole module if
    Mail.app is unreachable or no attachment-bearing message is found.
    """
    account = _account()
    try:
        rows = connector._search_messages_applescript(
            account=account, mailbox="INBOX",
            has_attachment=True, limit=10,
        )
    except Exception as e:  # noqa: BLE001 — Mail.app not reachable → skip
        pytest.skip(f"Mail.app/account {account!r} not reachable: {e}")

    for row in rows:
        numeric_id = str(row.get("id", ""))
        if not numeric_id:
            continue
        atts = connector._get_attachments_applescript(numeric_id)
        if atts:
            return {
                "account": account,
                "mailbox": "INBOX",
                "numeric_id": numeric_id,
                "rfc_id": row.get("rfc_message_id") or "",
                "attachments": atts,
                "first_downloaded": bool(atts[0].get("downloaded")),
            }
    pytest.skip(f"No attachment-bearing message found in {account!r} INBOX")


def _imap_available(connector: AppleMailConnector, account: str) -> bool:
    from apple_mail_mcp.exceptions import (
        MailKeychainAccessDeniedError,
        MailKeychainEntryNotFoundError,
    )
    from apple_mail_mcp.keychain import get_imap_password
    try:
        _h, _p, email = connector._resolve_imap_config(account)
        get_imap_password(account, email)
        return True
    except (MailKeychainEntryNotFoundError, MailKeychainAccessDeniedError):
        return False
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# AppleScript-runtime tier — no IMAP credentials needed. This is the class
# of bug that bit us (every one was on the osascript path).
# ---------------------------------------------------------------------------


class TestAppleScriptSmoke:
    def test_get_message_returns_a_record_not_a_list(
        self, connector: AppleMailConnector, target: dict[str, Any]
    ) -> None:
        """Catches the result_record `{{...}}` list-vs-record bug: the
        AppleScript path must yield a dict, not a 1-element list. Exercises
        the narrowed AppleScript codegen directly (account+mailbox) — same
        result record, but a single-mailbox scan instead of the slow
        all-mailbox cross-scan (issue #72)."""
        msg = connector._get_message_applescript(
            target["numeric_id"], False,
            account=target["account"], mailbox="INBOX",
        )
        assert isinstance(msg, dict)
        assert msg.get("id") == target["numeric_id"]

    def test_get_message_include_attachments_enumerates(
        self, connector: AppleMailConnector, target: dict[str, Any]
    ) -> None:
        """Catches the -10000 property error aborting the attachment record
        build (it used to raise 'not found')."""
        msg = connector._get_message_applescript(
            target["numeric_id"], False, True,
            account=target["account"], mailbox="INBOX",
        )
        assert isinstance(msg, dict)
        assert isinstance(msg.get("attachments"), list)
        assert len(msg["attachments"]) >= 1
        assert msg["attachments"][0].get("name")

    def test_get_attachments_enumerates(
        self, connector: AppleMailConnector, target: dict[str, Any]
    ) -> None:
        atts = connector._get_attachments_applescript(target["numeric_id"])
        assert isinstance(atts, list) and len(atts) >= 1
        for a in atts:
            assert set(a.keys()) >= {"name", "mime_type", "size", "downloaded"}

    def test_search_include_attachments_enumerates(
        self, connector: AppleMailConnector, target: dict[str, Any]
    ) -> None:
        rows = connector._search_messages_applescript(
            account=target["account"], mailbox="INBOX",
            has_attachment=True, include_attachments=True, limit=5,
        )
        assert isinstance(rows, list) and rows
        assert any(r.get("attachments") for r in rows)

    def test_save_attachments_applescript_writes_contained(
        self, connector: AppleMailConnector, target: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        """The AppleScript save path actually writes a file inside the target
        dir with a sanitized basename. Requires a downloaded attachment."""
        if not target["first_downloaded"]:
            pytest.skip("first attachment not downloaded — AppleScript save "
                        "would -10000; covered by the IMAP tier")
        n = connector.save_attachments(target["numeric_id"], tmp_path)
        assert n >= 1
        written = list(tmp_path.iterdir())
        assert written, "save reported success but wrote nothing"
        for p in written:
            assert p.resolve().is_relative_to(tmp_path.resolve())
            assert "/" not in p.name and ".." not in p.name


# ---------------------------------------------------------------------------
# Write tier — real-execution coverage for the AppleScript WRITE codegen,
# the half of the connector the read tiers never touch. Same osascript bug
# class lives here (a bad brace, a -10000 on `set flagged`, a recipient
# block that silently no-ops, a `whose id is` write scan that finds
# nothing). No IMAP credentials needed — drafts are pure AppleScript.
#
# SAFETY (human-in-the-loop): every test is fully self-contained. It only
# touches a scratch draft it creates and then trashes — no sends
# (send_now stays False), no deletes of real mail, no mutation of any
# pre-existing message. Recipient is the reserved @example.invalid domain.
# ---------------------------------------------------------------------------

_SCRATCH_TO = "write-smoke@example.invalid"
_SCRATCH_SUBJECT = "apple-mail-mcp write-path smoke (safe to delete)"
_SCRATCH_BODY = "write-path smoke body — created and trashed by the test"


def _make_scratch_draft(connector: AppleMailConnector) -> str:
    """Create a scratch draft and return its id, skipping if Mail.app
    is unreachable (keeps the write tier as resilient as the rest)."""
    try:
        result = connector.create_draft(
            to=[_SCRATCH_TO],
            subject=_SCRATCH_SUBJECT,
            body=_SCRATCH_BODY,
            send_now=False,
        )
    except Exception as e:  # noqa: BLE001 — Mail.app not reachable → skip
        pytest.skip(f"create_draft (Mail.app) not reachable: {e}")
    draft_id = result.get("draft_id", "")
    if not draft_id:
        pytest.skip("create_draft returned no draft_id")
    return draft_id


def _settle_whose(fn: Any) -> int:
    """Run a `whose id is`-dependent write, tolerating the documented lag
    where a freshly-created draft is not yet queryable by whose-clause
    (see get_draft_state's docstring). Returns the first count >= 1, or
    the last result after a bounded wait — never sleeps once it succeeds.

    Wall-clock worst case is dominated by the retried op, not the sleeps:
    up to 12 calls of ``fn`` (each a real osascript write) plus 11×0.5s.
    On the happy path it returns after one call with no sleep.
    """
    last = 0
    for _ in range(12):
        last = fn()
        if last >= 1:
            return last
        time.sleep(0.5)
    return last


def _settle_delete(connector: AppleMailConnector, draft_id: str) -> bool:
    """Delete a freshly-created scratch draft, tolerating the same
    whose-clause lag: delete_draft matches via `whose id is`, which can
    briefly miss a just-created draft. Returns True once trashed; re-raises
    MailDraftNotFoundError only after the lag window, where a persistent
    miss is a genuine failure (the draft was never deletable)."""
    from apple_mail_mcp.exceptions import MailDraftNotFoundError

    last_exc: MailDraftNotFoundError | None = None
    for _ in range(12):
        try:
            return connector.delete_draft(draft_id)
        except MailDraftNotFoundError as e:
            last_exc = e
            time.sleep(0.5)
    assert last_exc is not None
    raise last_exc


class TestWriteSmoke:
    def test_create_get_delete_draft_roundtrip(
        self, connector: AppleMailConnector
    ) -> None:
        """create_draft's AppleScript actually WRITES the recipients,
        subject, and body (the does-save-actually-write bug class, write
        side); get_draft_state reads them back; delete_draft trashes it;
        a second delete reports not-found. All pure AppleScript."""
        from apple_mail_mcp.exceptions import MailDraftNotFoundError

        draft_id = _make_scratch_draft(connector)
        deleted = False
        try:
            state = connector.get_draft_state(draft_id)
            # Spec: create_draft persists exactly what it was given.
            assert state["subject"] == _SCRATCH_SUBJECT
            assert _SCRATCH_BODY in state["body"]  # signature may be appended
            assert state["to"] == [_SCRATCH_TO]

            # delete_draft matches via `whose id is`; tolerate the fresh-
            # draft lag (same guard test 2 uses) so this never flakes red.
            assert _settle_delete(connector, draft_id) is True
            deleted = True

            # Spec: deleting an already-trashed draft reports not-found.
            # (Un-retried: not-found is the EXPECTED outcome here, and it
            # also proves the delete above actually removed the draft.)
            with pytest.raises(MailDraftNotFoundError):
                connector.delete_draft(draft_id)
        finally:
            if not deleted:
                try:
                    _settle_delete(connector, draft_id)
                except Exception:  # noqa: BLE001 — best-effort cleanup
                    pass

    def test_update_message_flag_write(
        self, connector: AppleMailConnector
    ) -> None:
        """update_message's AppleScript write codegen (_bulk_repeat_block
        + _build_flag_actions + the `whose id is` write scan) runs against
        a real message and reports it updated. A numeric id forces the
        AppleScript path — the IMAP fast path is RFC-id-only (P0-3). Target
        is a scratch draft so no pre-existing mail is touched.

        count == 1 is a strong signal: `set flagged status` runs inside
        the repeat block's `try`, and the counter increments only AFTER
        it, so a -10000 on the property-set would yield 0. One direction
        (set) exercises the codegen; clearing would only repeat the same
        cross-scan, and cleanup trashes the draft regardless of flag
        state. The set is wrapped to tolerate the documented whose-clause
        lag on a freshly-created draft."""
        draft_id = _make_scratch_draft(connector)
        cleaned = False
        try:
            # Spec: update_message returns the count of messages updated.
            n_set = _settle_whose(
                lambda: connector.update_message([draft_id], flagged=True)
            )
            assert n_set == 1, "flag-set write found/updated no message"
            # Verified cleanup: delete_draft returning True is itself an
            # AppleScript claim worth asserting (self-policing the leak
            # invariant), not just a best-effort finally.
            assert _settle_delete(connector, draft_id) is True
            cleaned = True
        finally:
            if not cleaned:
                try:
                    _settle_delete(connector, draft_id)
                except Exception:  # noqa: BLE001 — best-effort cleanup
                    pass


# ---------------------------------------------------------------------------
# IMAP tier — needs credentials; skips cleanly if unavailable. Covers the
# download-independent byte path (get_attachment_content / save via IMAP).
# ---------------------------------------------------------------------------


class TestImapSmoke:
    def test_get_attachment_content_via_imap(
        self, connector: AppleMailConnector, target: dict[str, Any]
    ) -> None:
        if not target["rfc_id"]:
            pytest.skip("no rfc_message_id on the target row")
        if not _imap_available(connector, target["account"]):
            pytest.skip("IMAP credentials unavailable")
        res = connector.get_attachment_content(
            target["rfc_id"], attachment_index=0,
            account=target["account"], mailbox="INBOX",
        )
        assert res.get("name")
        assert isinstance(res.get("content"), str) and res["content"]

    def test_save_attachments_via_imap_writes_contained(
        self, connector: AppleMailConnector, target: dict[str, Any],
        tmp_path: Path,
    ) -> None:
        if not target["rfc_id"]:
            pytest.skip("no rfc_message_id on the target row")
        if not _imap_available(connector, target["account"]):
            pytest.skip("IMAP credentials unavailable")
        n = connector.save_attachments(
            target["rfc_id"], tmp_path,
            account=target["account"], mailbox="INBOX",
        )
        assert n >= 1
        written = list(tmp_path.iterdir())
        assert written
        for p in written:
            assert p.resolve().is_relative_to(tmp_path.resolve())
            assert p.stat().st_size > 0
