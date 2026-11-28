"""Tests for what the 2026-11-27 field test of ``attachment_exports`` asked for.

The export was used to inventory Message-IDs across thousands of messages
and was far too slow, because every message was downloaded even when no
file could come of it. A manifest-only mode lists messages from headers
alone, and the ordinary mode no longer downloads a message that has no
attachments.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from apple_mail_fast_mcp import attachment_exports as ax
from tests.unit.test_attachment_exports import (
    SENDER,
    FakeConnector,
    StrictClient,
    _commit,
    _files,
    _manifest,
    _message,
    _naming,
    _preview,
    _reports,
)


def _plain() -> ax.Naming:
    return ax.build_naming(filename_template="{name}")


def _body_fetches(conn: FakeConnector) -> list[list[int]]:
    return [uids for uids, parts in conn.client.fetches if "BODY.PEEK[]" in parts]


def _many(count: int) -> dict[int, dict[str, Any]]:
    return {
        uid: _message(
            uid, f"Thread about hotel {uid}",
            received=datetime(2026, 5, 1 + uid % 20, 9, uid % 60),
        )
        for uid in range(1, count + 1)
    }


class TestManifestOnly:
    def test_lists_every_message_without_downloading_any(self, tmp_path: Path) -> None:
        messages = _many(300)
        messages[7]["cc"] = ("owen@uscanrealestate.com", "second@example.org")
        conn = FakeConnector(StrictClient(messages))
        store = ax.ExportStore()
        preview = _preview(conn, store, tmp_path, naming=_plain(), manifest_only=True)
        assert preview["manifest_only"] is True
        assert preview["matched"] == 300
        assert preview["to_export"] == 300
        fetches_before = len(conn.client.fetches)

        out = _commit(conn, store, preview["preview_id"])

        # One call, and nothing at all asked of the mail server.
        assert out["outcome"] == "complete"
        assert out["listed"] == 300
        assert out["messages_done"] == 300
        assert out["saved"] == 0
        assert len(conn.client.fetches) == fetches_before
        assert _body_fetches(conn) == []
        assert _files(tmp_path) == []

        rows = _manifest(tmp_path)
        assert len(rows) == 300
        assert {r["status"] for r in rows} == {"listed"}
        assert len({r["message_id"] for r in rows}) == 300
        row = next(r for r in rows if r["uid"] == "7")
        assert row["sender"] == SENDER
        assert row["to"] == "owner@example.com"
        assert row["cc"] == "owen@uscanrealestate.com, second@example.org"
        assert row["subject"] == "Thread about hotel 7"
        assert row["date_received"].startswith("2026-05-08")
        assert row["saved_path"] == "" and row["sha256"] == ""

    def test_preview_does_not_talk_about_files(self, tmp_path: Path) -> None:
        conn = FakeConnector(StrictClient(_many(5)))
        preview = _preview(
            conn, ax.ExportStore(), tmp_path, naming=_plain(), manifest_only=True
        )
        for key in ("sample_paths", "duplicate_target_paths", "pattern_no_match",
                    "already_exported"):
            assert key not in preview

    def test_listing_again_lists_again(self, tmp_path: Path) -> None:
        """An inventory is not an export: nothing is treated as already done."""
        messages = _many(4)
        for _ in range(2):
            conn = FakeConnector(StrictClient(messages))
            store = ax.ExportStore()
            preview = _preview(conn, store, tmp_path, naming=_plain(), manifest_only=True)
            assert preview["to_export"] == 4
            _commit(conn, store, preview["preview_id"])
        assert len(_manifest(tmp_path)) == 8

    def test_a_listing_does_not_make_a_later_export_skip_anything(
        self, tmp_path: Path
    ) -> None:
        messages = _reports(4)
        conn = FakeConnector(StrictClient(messages))
        store = ax.ExportStore()
        listing = _preview(conn, store, tmp_path, naming=_naming(), manifest_only=True)
        _commit(conn, store, listing["preview_id"])
        preview = _preview(conn, store, tmp_path, naming=_naming())
        assert preview["already_exported"] == 0
        out = _commit(conn, store, preview["preview_id"])
        assert out["saved"] == 4

    def test_a_message_the_preview_could_not_read_is_fetched_by_header_only(
        self, tmp_path: Path
    ) -> None:
        messages = _many(3)
        conn = FakeConnector(StrictClient(messages))
        store = ax.ExportStore()
        preview = _preview(conn, store, tmp_path, naming=_plain(), manifest_only=True)
        entry = store.get(preview["preview_id"])
        del entry.fields[2]
        out = _commit(conn, store, preview["preview_id"])
        assert out["listed"] == 3
        assert _body_fetches(conn) == []
        assert ([2], ["ENVELOPE", "INTERNALDATE"]) in conn.client.fetches
        assert len({r["message_id"] for r in _manifest(tmp_path)}) == 3

    def test_max_count_still_guards(self, tmp_path: Path) -> None:
        from apple_mail_fast_mcp import query_moves as qm

        conn = FakeConnector(StrictClient(_many(5)))
        store = ax.ExportStore()
        preview = _preview(conn, store, tmp_path, naming=_plain(), manifest_only=True)
        with pytest.raises(qm.PreviewTooLargeError):
            _commit(conn, store, preview["preview_id"], max_count=4)
        assert not (tmp_path / ax.MANIFEST_NAME).exists()

    def test_a_cell_from_mail_is_still_made_inert(self, tmp_path: Path) -> None:
        messages = {1: _message(1, "=HYPERLINK(\"http://x\")")}
        conn = FakeConnector(StrictClient(messages))
        store = ax.ExportStore()
        preview = _preview(conn, store, tmp_path, naming=_plain(), manifest_only=True)
        _commit(conn, store, preview["preview_id"])
        assert _manifest(tmp_path)[0]["subject"].startswith("'=")


class TestNoAttachmentIsNotDownloaded:
    def _mixed(self) -> dict[int, dict[str, Any]]:
        messages = _many(12)
        for uid in (1, 2, 4, 5, 7, 8, 10, 11):
            messages[uid] = _message(uid, f"Plain note {uid}", attachments=[])
        return messages

    def test_only_messages_with_attachments_are_downloaded(self, tmp_path: Path) -> None:
        conn = FakeConnector(StrictClient(self._mixed()))
        store = ax.ExportStore()
        preview = _preview(
            conn, store, tmp_path,
            naming=ax.build_naming(filename_template="{uid} {name}"),
        )
        out = _commit(conn, store, preview["preview_id"])
        assert out["outcome"] == "complete"
        assert out["saved"] == 4
        assert out["no_matching_attachment"] == 8
        assert sorted(u for batch in _body_fetches(conn) for u in batch) == [3, 6, 9, 12]
        rows = _manifest(tmp_path)
        assert len(rows) == 12
        bare = [r for r in rows if r["status"] == "no_matching_attachment"]
        assert len(bare) == 8 and all(r["message_id"] and r["subject"] for r in bare)

    def test_when_the_server_does_not_say_the_message_is_downloaded(
        self, tmp_path: Path
    ) -> None:
        """Skipping is only allowed on a positive answer. With none, the
        message is read, as before."""
        conn = FakeConnector(StrictClient(self._mixed()))
        conn.client.hide_structure = True
        store = ax.ExportStore()
        preview = _preview(
            conn, store, tmp_path,
            naming=ax.build_naming(filename_template="{uid} {name}"),
        )
        out = _commit(conn, store, preview["preview_id"])
        assert out["saved"] == 4
        assert out["no_matching_attachment"] == 8
        assert len({u for batch in _body_fetches(conn) for u in batch}) == 12

    def test_a_name_filter_is_still_settled_from_the_real_names(
        self, tmp_path: Path
    ) -> None:
        """Names in the structure are not decoded the way the saved names
        are, so a name filter never decides a skip: only "no attachments at
        all" does."""
        messages = {1: _message(1, "With file", attachments=[("report.pdf", b"x")])}
        conn = FakeConnector(StrictClient(messages))
        store = ax.ExportStore()
        preview = _preview(
            conn, store, tmp_path, naming=_plain(), attachment_name_contains=".zzz-none",
        )
        out = _commit(conn, store, preview["preview_id"])
        assert out["no_matching_attachment"] == 1
        assert _body_fetches(conn) == [[1]]


class TestHonestPreview:
    def test_duplicates_are_not_guessed_when_names_depend_on_the_attachment(
        self, tmp_path: Path
    ) -> None:
        """With the default template every message rendered to the same
        placeholder, so the preview claimed every message collided."""
        conn = FakeConnector(StrictClient(_many(6)))
        preview = _preview(conn, ax.ExportStore(), tmp_path, naming=_plain())
        assert preview["duplicate_target_paths"] is None
        assert "attachment's own name" in preview["duplicate_check"]
        assert "NAME.EXT" in preview["sample_note"]

    def test_duplicates_are_still_counted_when_they_can_be(self, tmp_path: Path) -> None:
        messages = _reports(4)
        messages[5] = _message(5, messages[1]["subject"])
        naming = _naming(filename_template="Fosse {report} {subject_date:%Y.%m.%d}")
        preview = _preview(
            FakeConnector(StrictClient(messages)), ax.ExportStore(), tmp_path, naming=naming
        )
        assert preview["duplicate_target_paths"] == 2
        assert "duplicate_check" not in preview


class TestNothingLeftInTheFolder:
    def test_no_lock_file_is_left_in_the_save_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "exports"
        target.mkdir()
        conn = FakeConnector(StrictClient(_reports(2)))
        store = ax.ExportStore()
        preview = _preview(conn, store, target)
        _commit(conn, store, preview["preview_id"])
        assert sorted(p.name for p in target.iterdir() if p.is_file()) == [ax.MANIFEST_NAME]
        assert not list(target.rglob(".export*"))

    def test_two_exports_into_one_directory_still_exclude_each_other(
        self, tmp_path: Path
    ) -> None:
        from apple_mail_fast_mcp import query_moves as qm

        conn = FakeConnector(StrictClient(_reports(2)))
        store = ax.ExportStore()
        preview = _preview(conn, store, tmp_path)
        with ax._directory_lock(tmp_path.resolve()):
            with pytest.raises(qm.CommitInProgressError):
                _commit(conn, store, preview["preview_id"])
        out = _commit(conn, store, preview["preview_id"])
        assert out["outcome"] == "complete"

    def test_different_directories_do_not_block_each_other(self, tmp_path: Path) -> None:
        first, second = tmp_path / "a", tmp_path / "b"
        first.mkdir()
        second.mkdir()
        with ax._directory_lock(first.resolve()), ax._directory_lock(second.resolve()):
            pass
