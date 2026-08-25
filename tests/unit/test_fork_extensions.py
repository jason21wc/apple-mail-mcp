"""Tests for fork-unique extensions carried across the upstream re-baseline.

The fork keeps a thin layer on top of upstream/main. The genuinely-unique
behavioral mods (everything else has converged into upstream) are:

- ``save_attachments`` ``output_filename`` (fork mod #2) — save a single
  attachment under a caller-chosen, sanitized name.
- ``content_is_untrusted`` / ``security_notice`` marking on ``get_messages``
  and ``get_attachment_content`` (fork PR #37). NOTE: this composes with
  upstream's #225 per-message ``prompt_injection`` annotation — ours is a
  blanket response-level signal that also covers attachment payloads; #225 is
  per-message body pattern-detection. Different layers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apple_mail_fast_mcp.server import (
    _UNTRUSTED_CONTENT_NOTICE,
    _mark_untrusted,
    delete_rule,
    get_attachment_content,
    get_messages,
    get_thread,
    save_attachments,
    save_template,
    search_messages,
    update_message,
)


@pytest.fixture
def mock_mail() -> Any:
    with patch("apple_mail_fast_mcp.server.mail") as m:
        yield m


@pytest.fixture
def mock_logger() -> Any:
    with patch("apple_mail_fast_mcp.server.operation_logger") as m:
        yield m


class TestSaveAttachmentsOutputFilename:
    def test_rejects_when_not_exactly_one_index(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Any
    ) -> None:
        for indices in (None, [0, 1]):
            result = save_attachments(
                "1",
                str(tmp_path),
                attachment_indices=indices,
                output_filename="x.pdf",
            )
            assert result["success"] is False
            assert result["error_type"] == "validation_error"
        mock_mail.save_attachments.assert_not_called()

    def test_moves_to_custom_name_and_reports_filename(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Any
    ) -> None:
        def _fake_save(**kwargs: Any) -> dict[str, Any]:
            # The connector writes into the temp dir it is handed; the tool
            # then moves that file into the destination under output_filename.
            Path(kwargs["save_directory"], "original.pdf").write_bytes(b"%PDF-1.7")
            return {"saved": 1, "rejected": []}

        mock_mail.save_attachments.side_effect = _fake_save

        result = save_attachments(
            "1",
            str(tmp_path),
            attachment_indices=[0],
            output_filename="report.pdf",
        )

        assert result["success"] is True
        assert result["saved"] == 1
        assert result["filename"] == "report.pdf"
        assert (tmp_path / "report.pdf").read_bytes() == b"%PDF-1.7"

    def test_sanitizes_custom_name_no_traversal(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Any
    ) -> None:
        def _fake_save(**kwargs: Any) -> dict[str, Any]:
            Path(kwargs["save_directory"], "original.pdf").write_bytes(b"data")
            return {"saved": 1, "rejected": []}

        mock_mail.save_attachments.side_effect = _fake_save

        result = save_attachments(
            "1",
            str(tmp_path),
            attachment_indices=[0],
            output_filename="../../evil.pdf",
        )

        assert result["success"] is True
        # The written file stays inside the destination; name is sanitized.
        saved = [p for p in tmp_path.iterdir() if p.is_file()]
        assert len(saved) == 1
        assert saved[0].parent == tmp_path
        assert ".." not in result["filename"]
        assert "/" not in result["filename"]

    def test_without_output_filename_unchanged(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Any
    ) -> None:
        # Regression guard: the default path is upstream's behavior, no filename.
        mock_mail.save_attachments.return_value = {"saved": 2, "rejected": []}
        result = save_attachments("1", str(tmp_path))
        assert result["success"] is True
        assert result["saved"] == 2
        assert "filename" not in result

    def test_output_filename_when_attachment_rejected(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Any
    ) -> None:
        # The fork×upstream boundary the re-baseline created: the single
        # attachment is rejected by the #236 byte cap -> saved 0, no move,
        # no `filename`, `rejected` still surfaced, nothing in the destination.
        rejected = [{"name": "big.pdf", "size": 99_999, "reason": "per_attachment_cap"}]

        def _fake_save(**kwargs: Any) -> dict[str, Any]:
            return {"saved": 0, "rejected": rejected}

        mock_mail.save_attachments.side_effect = _fake_save

        result = save_attachments(
            "1",
            str(tmp_path),
            attachment_indices=[0],
            output_filename="report.pdf",
        )

        assert result["success"] is True
        assert result["saved"] == 0
        assert result["rejected"] == rejected
        assert "filename" not in result
        assert [p for p in tmp_path.iterdir() if p.is_file()] == []


class TestUntrustedContentMarking:
    def test_mark_untrusted_helper_contract(self) -> None:
        # Single source for the marking — pin the exact verbatim-contract
        # fields so a future edit to the constant/helper can't silently drift.
        marked = _mark_untrusted({"success": True}, True)
        assert marked["content_is_untrusted"] is True
        assert marked["security_notice"] == _UNTRUSTED_CONTENT_NOTICE
        # No content -> nothing to distrust -> no marker.
        bare = _mark_untrusted({"success": True}, False)
        assert "content_is_untrusted" not in bare
        assert "security_notice" not in bare

    def test_get_messages_marks_nonempty_and_keeps_content_verbatim(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        body = "line1\nline2 exact-parse-sensitive"
        mock_mail.get_message.return_value = {
            "id": "1",
            "subject": "S",
            "content": body,
        }

        result = get_messages(["1"])

        assert result["success"] is True
        assert result["content_is_untrusted"] is True
        assert "untrusted" in result["security_notice"].lower()
        # Non-breaking: the body itself is returned byte-for-byte.
        assert result["messages"][0]["content"] == body

    def test_get_messages_empty_result_has_no_marker(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        # An empty result carries nothing to distrust -> no marker (the
        # `if messages:` guard). Patch the resolver to yield no messages.
        with patch(
            "apple_mail_fast_mcp.server._resolve_id_list_to_messages", return_value=[]
        ):
            result = get_messages(["1"])

        assert result["success"] is True
        assert result["count"] == 0
        assert "content_is_untrusted" not in result
        assert "security_notice" not in result

    def test_get_attachment_content_marks_and_keeps_content_verbatim(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.get_attachment_content.return_value = {
            "payload": b"raw-bytes",
            "name": "a.txt",
            "mime_type": "text/plain",
            "size": 9,
        }

        result = get_attachment_content("1", 0)

        assert result["success"] is True
        assert result["content_is_untrusted"] is True
        assert result["security_notice"]
        assert result["content"] == "raw-bytes"

    # --- coverage extended to search_messages + get_thread ---------------
    # Their rows carry attacker-controlled sender/subject/snippet, so an LLM
    # consuming them needs the same untrusted signal get_messages already
    # gives (prompt-injection defense; coding-quality-workflow-integrity).

    def test_search_messages_marks_results_and_keeps_rows(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.search_messages.return_value = [
            {"id": "1", "subject": "S", "sender": "x@y.com"}
        ]
        result = search_messages(account="Acct", mailbox="INBOX")

        assert result["success"] is True
        assert result["content_is_untrusted"] is True
        assert result["security_notice"] == _UNTRUSTED_CONTENT_NOTICE
        # Rows returned unchanged (non-breaking).
        assert result["messages"][0]["sender"] == "x@y.com"

    def test_search_messages_empty_has_no_marker(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail.search_messages.return_value = []
        result = search_messages(account="Acct", mailbox="INBOX")

        assert result["success"] is True
        assert result["count"] == 0
        assert "content_is_untrusted" not in result
        assert "security_notice" not in result

    def test_search_messages_source_path_marks_results(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        # The source=[ids] branch builds a separate response; it must mark too.
        with patch(
            "apple_mail_fast_mcp.server._resolve_id_list_to_messages",
            return_value=[{"id": "1", "subject": "S", "sender": "x@y.com"}],
        ), patch(
            "apple_mail_fast_mcp.server._apply_search_filters",
            side_effect=lambda resolved, *a, **k: resolved,
        ):
            result = search_messages(source=["1"])

        assert result["success"] is True
        assert result["content_is_untrusted"] is True
        assert result["security_notice"]

    def test_get_thread_marks_results(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        # Upstream #420 moved the server onto _get_thread_with_status, which
        # returns (rows, degraded_reason) rather than a bare list.
        mock_mail._get_thread_with_status.return_value = (
            [
                {"id": "1", "subject": "S", "sender": "x@y.com"},
                {"id": "2", "subject": "S", "sender": "z@y.com"},
            ],
            None,
        )
        result = get_thread("1")

        assert result["success"] is True
        assert result["count"] == 2
        assert result["partial"] is False
        assert result["content_is_untrusted"] is True
        assert result["security_notice"] == _UNTRUSTED_CONTENT_NOTICE

    def test_get_thread_partial_keeps_untrusted_marker(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        """A degraded thread must carry BOTH signals: upstream's partial
        flag (#420) and the fork's untrusted marking. The two compose --
        a truncated thread is still attacker-controlled content."""
        mock_mail._get_thread_with_status.return_value = (
            [{"id": "1", "subject": "S", "sender": "x@y.com"}],
            "imap_timeout",
        )
        result = get_thread("1")

        assert result["success"] is True
        assert result["partial"] is True
        assert result["partial_reason"] == "imap_timeout"
        assert result["content_is_untrusted"] is True
        assert result["security_notice"] == _UNTRUSTED_CONTENT_NOTICE

    def test_get_thread_empty_has_no_marker(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail._get_thread_with_status.return_value = ([], None)
        result = get_thread("1")

        assert result["success"] is True
        assert result["count"] == 0
        assert "content_is_untrusted" not in result
        assert "security_notice" not in result


class TestOutputFilenameNoClobber:
    """#FORK — `output_filename` must not silently destroy an existing file.

    ADR-5 makes incremental retrieval depend on deterministic filenames plus an
    existence check, but the check lived only in the attachment-retrieval
    skill's instructions. Caller guidance is not an enforcement boundary: a
    direct MCP call could overwrite a previously-saved report, and the
    post-write size prune could then delete the replacement, losing both.
    """

    def test_existing_destination_is_not_overwritten(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Path
    ) -> None:
        target = tmp_path / "weekly-report.pdf"
        target.write_bytes(b"ORIGINAL CONTENT")

        def _fake_save(**kwargs: Any) -> dict[str, Any]:
            Path(kwargs["save_directory"], "raw.pdf").write_bytes(b"NEW CONTENT")
            return {"saved": 1, "rejected": []}

        mock_mail.save_attachments.side_effect = _fake_save

        result = save_attachments(
            message_id="1",
            save_directory=str(tmp_path),
            attachment_indices=[0],
            output_filename="weekly-report.pdf",
        )

        assert result["success"] is False
        assert result["error_type"] == "already_exists"
        assert target.read_bytes() == b"ORIGINAL CONTENT"

    def test_overwrite_true_replaces_the_file(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Path
    ) -> None:
        target = tmp_path / "weekly-report.pdf"
        target.write_bytes(b"ORIGINAL CONTENT")

        def _fake_save(**kwargs: Any) -> dict[str, Any]:
            Path(kwargs["save_directory"], "raw.pdf").write_bytes(b"NEW CONTENT")
            return {"saved": 1, "rejected": []}

        mock_mail.save_attachments.side_effect = _fake_save

        result = save_attachments(
            message_id="1",
            save_directory=str(tmp_path),
            attachment_indices=[0],
            output_filename="weekly-report.pdf",
            overwrite=True,
        )

        assert result["success"] is True
        assert target.read_bytes() == b"NEW CONTENT"


class TestOutputFilenameAtomicity:
    """#FORK — the no-clobber guarantee must survive concurrency and odd targets.

    A check-then-act `exists()` guard leaves a TOCTOU window: a file created
    between the test and the move is silently overwritten. The commit itself
    has to be the exclusive operation.
    """

    @staticmethod
    def _fake_save(payload: bytes) -> Any:
        def _inner(**kwargs: Any) -> dict[str, Any]:
            Path(kwargs["save_directory"], "raw.pdf").write_bytes(payload)
            return {"saved": 1, "rejected": []}

        return _inner

    def test_directory_at_destination_is_rejected_not_nested(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Path
    ) -> None:
        """shutil.move would place the file INSIDE the directory and call it
        success, producing report.pdf/raw.pdf."""
        (tmp_path / "report.pdf").mkdir()
        mock_mail.save_attachments.side_effect = self._fake_save(b"NEW")

        result = save_attachments(
            message_id="1",
            save_directory=str(tmp_path),
            attachment_indices=[0],
            output_filename="report.pdf",
            overwrite=True,
        )

        assert result["success"] is False
        assert result["error_type"] == "invalid_destination"
        assert (tmp_path / "report.pdf").is_dir()
        assert list((tmp_path / "report.pdf").iterdir()) == []

    def test_concurrent_default_saves_exactly_one_wins(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Path
    ) -> None:
        """Two concurrent no-clobber saves to the same name: exactly one
        succeeds, the other reports already_exists. Never two successes."""
        import threading

        barrier = threading.Barrier(2)

        def _racing_save(**kwargs: Any) -> dict[str, Any]:
            Path(kwargs["save_directory"], "raw.pdf").write_bytes(b"NEW")
            # Both threads finish staging before either commits, so the
            # commit is the only thing separating them.
            barrier.wait(timeout=5)
            return {"saved": 1, "rejected": []}

        mock_mail.save_attachments.side_effect = _racing_save
        results: list[dict[str, Any]] = []
        lock = threading.Lock()

        def _run() -> None:
            r = save_attachments(
                message_id="1",
                save_directory=str(tmp_path),
                attachment_indices=[0],
                output_filename="report.pdf",
            )
            with lock:
                results.append(r)

        threads = [threading.Thread(target=_run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(results) == 2
        successes = [r for r in results if r.get("success")]
        refusals = [r for r in results if r.get("error_type") == "already_exists"]
        assert len(successes) == 1, f"expected exactly one winner, got {results}"
        assert len(refusals) == 1, f"expected one already_exists, got {results}"


class TestGetThreadMailboxHints:
    """get_thread must be able to reach a message filed outside INBOX.

    Live-reproduced 2026-08-22 on a real iCloud account: messages returned by
    search_messages from `Projects/Filed` were reported
    message_not_found by get_thread, because anchor resolution only probes
    INBOX + Sent (no Gmail All-Mail on iCloud). The hints close that gap
    without reintroducing the unindexed all-mailbox scan #415 removed.
    """

    def test_hints_are_forwarded_to_the_connector(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail._get_thread_with_status.return_value = (
            [{"id": "1", "subject": "S", "sender": "sender@example.com"}],
            None,
        )

        result = get_thread(
            "abc@example.com",
            account="iCloud",
            mailbox="Projects/Filed",
        )

        assert result["success"] is True
        mock_mail._get_thread_with_status.assert_called_once_with(
            "abc@example.com", "iCloud", "Projects/Filed"
        )

    def test_hints_are_optional(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        """Existing single-argument callers keep working unchanged."""
        mock_mail._get_thread_with_status.return_value = ([], None)

        result = get_thread("1")

        assert result["success"] is True
        mock_mail._get_thread_with_status.assert_called_once_with("1", None, None)

    def test_untrusted_marking_survives_the_new_signature(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        """Fork mod #2 must still apply on the hinted path."""
        mock_mail._get_thread_with_status.return_value = (
            [{"id": "1", "subject": "S", "sender": "x@y.com"}],
            None,
        )

        result = get_thread("1", account="iCloud", mailbox="INBOX")

        assert result["content_is_untrusted"] is True
        assert result["security_notice"] == _UNTRUSTED_CONTENT_NOTICE


class TestGetThreadHintFailurePaths:
    """#FORK — hint-specific failure paths the original PR #54 did not cover."""

    def test_uuid_account_hint_reaches_applescript_fallback(self) -> None:
        """An account hint is forwarded verbatim, so anchor["account"] can be a
        UUID. `account "<uuid>"` matches nothing in AppleScript — it must be
        emitted as `account id "<uuid>"`."""
        from apple_mail_fast_mcp.mail_connector import AppleMailConnector

        conn = AppleMailConnector()
        scripts: list[str] = []
        uuid = "1A2B3C4D-5E6F-7081-9A2B-3C4D5E6F7081"

        with patch.object(
            conn, "_run_applescript", side_effect=lambda s: scripts.append(s) or "[]"
        ):
            conn._collect_thread_applescript(
                {"account": uuid, "subject": "S", "rfc_message_id": "a@b.com"}
            )

        assert scripts, "no AppleScript was generated"
        assert f'account id "{uuid}"' in scripts[0], (
            "a UUID account hint must produce `account id`, not `account`"
        )

    def test_bad_account_hint_reports_account_not_found(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        """B11: the account hint skips list_accounts() by design, so a bad name
        surfaces from _resolve_imap_config rather than as an empty candidate
        list. It must not land in the generic handler as "unknown"."""
        from apple_mail_fast_mcp.exceptions import MailAccountNotFoundError

        mock_mail._get_thread_with_status.side_effect = MailAccountNotFoundError(
            "Account 'iClod' not found"
        )

        result = get_thread("a@b.com", account="iClod", mailbox="INBOX")

        assert result["success"] is False
        assert result["error_type"] == "account_not_found"

    def test_control_chars_in_mailbox_hint_report_validation_error(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        mock_mail._get_thread_with_status.side_effect = ValueError(
            "mailbox contains control characters"
        )

        result = get_thread("a@b.com", account="iCloud", mailbox="bad\r\nname")

        assert result["success"] is False
        assert result["error_type"] == "validation_error"


class TestSaveDirectoryTildeExpansion:
    """`~` must expand. Live-hit 2026-08-22: `~/Desktop/mcp-test/` returned
    directory_not_found on a machine where the folder existed, because the
    path was used literally. Every caller had to know to pass an absolute
    path, and nothing said so."""

    def test_tilde_path_is_expanded(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        target = tmp_path / "Desktop" / "mcp-test"
        target.mkdir(parents=True)
        mock_mail.save_attachments.return_value = {"saved": 1, "rejected": []}

        result = save_attachments(
            message_id="1", save_directory="~/Desktop/mcp-test"
        )

        assert result["success"] is True, result
        # The connector must receive the EXPANDED path, not the literal tilde.
        passed = mock_mail.save_attachments.call_args.kwargs["save_directory"]
        assert "~" not in str(passed)
        assert Path(passed) == target

    def test_absolute_paths_still_work(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Path
    ) -> None:
        mock_mail.save_attachments.return_value = {"saved": 1, "rejected": []}
        result = save_attachments(message_id="1", save_directory=str(tmp_path))
        assert result["success"] is True

    def test_unresolvable_tilde_user_is_a_validation_error(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        """`~nosuchuser/x` makes expanduser raise RuntimeError, which used to
        surface as error_type "unknown"."""
        result = save_attachments(
            message_id="1", save_directory="~nosuchuser-zz/whatever"
        )
        assert result["success"] is False
        assert result["error_type"] in ("validation_error", "directory_not_found")


class TestConsequenceGating:
    """#FORK — gate on what a call DOES, not which tool was called.

    The policy is consequence-based, but each gate was written per tool, so two
    tools reaching the same end state carried different gates. Upstream #440
    tracks the structural fix; these close the verified instances.
    """

    @pytest.mark.asyncio
    async def test_moving_to_trash_requires_confirmation(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        """update_message(destination_mailbox="Trash") reaches delete_messages'
        end state — up to 100 messages in Trash — and must confirm too."""
        with patch(
            "apple_mail_fast_mcp.server._elicit_confirmation",
            new_callable=AsyncMock,
        ) as elicit:
            elicit.return_value = {"success": False, "error_type": "cancelled"}
            result = await update_message(
                ["1", "2"], destination_mailbox="Trash", ctx=None
            )

        assert elicit.called, "a bulk move to Trash was not confirmed"
        assert result["error_type"] == "cancelled"
        mock_mail.update_message.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "mailbox", ["Trash", "trash", "Deleted Messages", "Deleted Items"]
    )
    async def test_trash_detection_covers_provider_names(
        self, mailbox: str, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        """iCloud says 'Deleted Messages', Outlook 'Deleted Items'. Gating on
        the literal string 'Trash' would miss the ones that matter."""
        with patch(
            "apple_mail_fast_mcp.server._elicit_confirmation",
            new_callable=AsyncMock,
        ) as elicit:
            elicit.return_value = {"success": False, "error_type": "cancelled"}
            await update_message(["1"], destination_mailbox=mailbox, ctx=None)

        assert elicit.called, f"move to {mailbox!r} was not treated as a delete"

    @pytest.mark.asyncio
    async def test_ordinary_move_still_unconfirmed(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        """Reversible moves stay unprompted — that is the policy, not an
        oversight."""
        mock_mail.update_message.return_value = 1
        with patch(
            "apple_mail_fast_mcp.server._elicit_confirmation",
            new_callable=AsyncMock,
        ) as elicit:
            result = await update_message(
                ["1"], destination_mailbox="Archive", ctx=None
            )

        assert not elicit.called
        assert result["success"] is True

    def test_save_template_refuses_silent_overwrite(
        self, mock_logger: MagicMock, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Its docstring opened 'Create or overwrite' — destroying an existing
        template was indistinguishable from creating a new one."""
        monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
        first = save_template(name="weekly", body="original")
        assert first["success"] is True

        second = save_template(name="weekly", body="replacement")
        assert second["success"] is False
        assert second["error_type"] == "already_exists"

    def test_save_template_overwrite_true_replaces(
        self, mock_logger: MagicMock, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
        save_template(name="weekly", body="original")
        result = save_template(
            name="weekly", body="replacement", overwrite=True
        )
        assert result["success"] is True


    @pytest.mark.asyncio
    async def test_rule_index_revalidated_after_confirmation(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        """The prompt names a rule by index, and rules can be reordered while
        it is open. Without a re-check the user confirms one rule and a
        different one is deleted — TOCTOU with a human-length window."""
        # The list is reordered while the dialog is open.
        snapshots = iter([
            [{"index": 1, "name": "Archive newsletters", "enabled": True},
             {"index": 2, "name": "Forward to accountant", "enabled": True}],
            [{"index": 1, "name": "Forward to accountant", "enabled": True},
             {"index": 2, "name": "Archive newsletters", "enabled": True}],
        ])
        mock_mail.list_rules.side_effect = lambda: next(snapshots)

        with patch(
            "apple_mail_fast_mcp.server._elicit_confirmation",
            new_callable=AsyncMock,
        ) as elicit:
            elicit.return_value = None  # user approves what they were shown
            result = await delete_rule(rule_index=1, ctx=None)

        assert result["success"] is False
        assert result["error_type"] == "target_changed"
        mock_mail.delete_rule.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_message_positional_order_preserved(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        """`ctx` was inserted as the SECOND parameter, so an existing call
        `update_message(ids, True)` silently passed True as the context instead
        of read_status. Context params belong at the end."""
        mock_mail.update_message.return_value = 1

        await update_message(["1"], True)  # positional read_status

        kwargs = mock_mail.update_message.call_args.kwargs
        assert kwargs.get("read_status") is True

    @pytest.mark.asyncio
    async def test_rule_revalidation_rejects_ambiguous_duplicate_names(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        """Rule names are explicitly NOT unique (TOOLS.md). Comparing only the
        name cannot identify the approved rule: swap two same-named rules and
        a name check still passes."""
        mock_mail.list_rules.return_value = [
            {"index": 1, "name": "Filter", "enabled": True},
            {"index": 2, "name": "Filter", "enabled": True},
        ]

        result = await delete_rule(rule_index=1, ctx=None)

        assert result["success"] is False
        assert result["error_type"] == "ambiguous_target"
