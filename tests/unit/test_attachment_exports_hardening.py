"""Regression tests for what the code and security reviews of
``attachment_exports`` found. Each test names the failure it guards against.
"""

from __future__ import annotations

import csv
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from apple_mail_fast_mcp import attachment_exports as ax
from apple_mail_fast_mcp import query_moves as qm
from tests.unit.test_attachment_exports import (
    PATTERN,
    SENDER,
    FakeConnector,
    StrictClient,
    _commit,
    _export,
    _files,
    _manifest,
    _message,
    _naming,
    _preview,
    _reports,
)

SUBJECT = "Credit Card Activity 04May26 from denbt1.marriott.com"


def _plain() -> ax.Naming:
    return ax.build_naming(filename_template="{name}")


# ---------------------------------------------------- the module's own files


class TestReservedNames:
    @pytest.mark.parametrize("name", ["export_manifest.csv", "EXPORT_MANIFEST.CSV"])
    @pytest.mark.parametrize("mode", ["skip", "suffix", "overwrite"])
    def test_an_attachment_cannot_become_or_replace_the_manifest(
        self, tmp_path: Path, name: str, mode: str
    ) -> None:
        """An attachment with the manifest's name would be saved over it (the
        filesystem ignores case), forging the record of what was exported."""
        forged = b"message_id,status,saved_path,size\r\nvictim@legit,saved,x.pdf,3\r\n"
        messages = {
            1: _message(1, SUBJECT, attachments=[("x.pdf", b"abc"), (name, forged)])
        }
        out, _, _ = _export(tmp_path, messages, naming=_plain(), if_exists=mode)
        assert out["saved"] == 1
        assert out["errors"] == 1
        rows = _manifest(tmp_path)
        assert [r["status"] for r in rows] == ["saved", "error"]
        assert "keeps for itself" in rows[1]["note"]
        assert _files(tmp_path) == ["x.pdf"]
        header = (tmp_path / ax.MANIFEST_NAME).read_text(encoding="utf-8").splitlines()[0]
        assert tuple(header.split(",")) == ax.MANIFEST_FIELDS

    def test_a_folder_cannot_take_the_manifests_name(self, tmp_path: Path) -> None:
        messages = {
            1: _message(1, SUBJECT, attachments=[("export_manifest.csv", b"x")])
        }
        out, _, _ = _export(
            tmp_path, messages,
            naming=ax.build_naming(filename_template="a.txt", subfolder_template="{name}"),
        )
        assert out["errors"] == 1
        assert not (tmp_path / "export_manifest.csv").is_dir()

    def test_a_foreign_file_at_the_manifests_name_is_not_trusted_or_appended_to(
        self, tmp_path: Path
    ) -> None:
        foreign = b"message_id,status\r\nm1@denbt1.marriott.com,saved\r\n"
        (tmp_path / ax.MANIFEST_NAME).write_bytes(foreign)
        conn = FakeConnector(StrictClient(_reports(2)))
        store = ax.ExportStore()
        preview = _preview(conn, store, tmp_path)
        assert preview["manifest_unreadable"] is True
        assert preview["already_exported"] == 0
        with pytest.raises(ax.ExportDirectoryError, match="not a manifest"):
            _commit(conn, store, preview["preview_id"])
        assert (tmp_path / ax.MANIFEST_NAME).read_bytes() == foreign

    def test_a_foreign_manifest_is_refused_before_any_file_is_written(
        self, tmp_path: Path
    ) -> None:
        conn = FakeConnector(StrictClient(_reports(2)))
        store = ax.ExportStore()
        preview = _preview(conn, store, tmp_path)
        (tmp_path / ax.MANIFEST_NAME).write_bytes(b"a,b\r\n1,2\r\n")
        with pytest.raises(ax.ExportDirectoryError):
            _commit(conn, store, preview["preview_id"])
        assert _files(tmp_path) == []
        assert not any("BODY.PEEK[]" in parts for _, parts in conn.client.fetches)

    def test_a_name_the_filesystem_treats_as_the_manifest_is_refused(
        self, tmp_path: Path
    ) -> None:
        """Some filesystems ignore certain characters when comparing names,
        which a check on the name alone cannot predict. A hard link stands
        in for that here: a different name, the same file."""
        messages = {1: _message(1, SUBJECT, attachments=[("alias.csv", b"forged")])}
        conn = FakeConnector(StrictClient(messages))
        store = ax.ExportStore()
        preview = _preview(conn, store, tmp_path, naming=_plain(), if_exists="overwrite")
        ax._append_manifest(tmp_path, [{"status": "saved", "uid": 99}])
        os.link(tmp_path / ax.MANIFEST_NAME, tmp_path / "alias.csv")
        before = (tmp_path / ax.MANIFEST_NAME).read_bytes()
        out = _commit(conn, store, preview["preview_id"])
        assert out["errors"] == 1
        assert out["overwritten"] == 0
        assert (tmp_path / ax.MANIFEST_NAME).read_bytes().startswith(before)

    @pytest.mark.parametrize("name", [ax.MANIFEST_NAME, ax.LOCK_NAME])
    def test_own_files_are_never_opened_through_a_symlink(
        self, tmp_path: Path, name: str
    ) -> None:
        """A link planted at the lock's name used to have its target emptied,
        and one at the manifest's name had rows appended to its target."""
        target = tmp_path / "exports"
        target.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("precious", encoding="utf-8")
        os.symlink(outside, target / name)
        conn = FakeConnector(StrictClient(_reports(2)))
        store = ax.ExportStore()
        preview = _preview(conn, store, target)
        with pytest.raises(ax.ExportDirectoryError):
            _commit(conn, store, preview["preview_id"])
        assert outside.read_text(encoding="utf-8") == "precious"


# ------------------------------------------------------------------ pattern


class TestPatternCost:
    @pytest.mark.parametrize(
        "pattern",
        [
            r"a*a*a*a*a*a*b",
            r".*.*.*.*.*.*x\d",
            r"(.*)(.*)(.*)(.*)(.*)z!",
            r"\w*\d*\w*\d*\w*\d*\w*!",
            r"(a+){1}(a+){1}(a+){1}(a+){1}(a+){1}b",
            "(a|aa)" * 26 + "b",
            "a?" * 26 + "a" * 26,
            r"(?>(a+)+b)",
            r"(?>(a|a)*b)",
            r"(a++)*",
            r"(a|a)*",
            r"(.*a){20}",
            r"(a*)*",
            r"(?:a+)+",
            r"(a?){50}a{50}",
            r"(a{1,100}){1,100}",
        ],
    )
    def test_slow_shapes_are_refused(self, pattern: str) -> None:
        with pytest.raises(ValueError, match="subject_pattern"):
            ax.build_naming(filename_template="{name}", subject_pattern=pattern)

    @pytest.mark.parametrize(
        "pattern",
        [
            PATTERN,
            r"^(?P<report>.+?) (?P<date>\d{2}[A-Za-z]{3}\d{2}) from (?P<host>.+)$",
            r"(?P<kind>Invoice|Receipt|Statement) #(?P<number>\d{1,12})",
            r"\[(?P<tag>[A-Z]{2,8})\] (?P<rest>.*)",
        ],
    )
    def test_ordinary_patterns_are_accepted(self, pattern: str) -> None:
        ax.build_naming(filename_template="{name}", subject_pattern=pattern)

    def test_deep_nesting_is_refused(self) -> None:
        """The bound counts ways to match, not the work each takes. Seventy
        nested groups made one subject take seconds while staying under it."""
        deep = ".?" * 16 + "(?:" + "(" * 78 + "." + ")" * 78 + "){284}Z"
        with pytest.raises(ValueError, match="nested"):
            ax.build_naming(filename_template="{name}", subject_pattern=deep)
        with pytest.raises(ValueError, match="nested"):
            ax.build_naming(
                filename_template="{name}", subject_pattern="(((((a)))))",
            )
        ax.build_naming(filename_template="{name}", subject_pattern="(((a)))")

    def test_the_worst_accepted_pattern_is_still_quick(self) -> None:
        """Two open-ended repeats is the most that is allowed. On a subject
        built to make it backtrack as far as it can, it must still return
        at once."""
        naming = ax.build_naming(
            filename_template="{name}", subject_pattern=r"(?P<a>.*)(?P<b>.*)z!"
        )
        began = time.monotonic()
        for _ in range(50):
            assert ax.message_values(
                subject="a" * 400, sender="a@x.com", received=datetime(2026, 1, 1),
                sent=None, uid=1, naming=naming,
            ) is None
        assert time.monotonic() - began < 2.0

    def test_the_pattern_is_applied_from_the_start_of_the_subject(self) -> None:
        subject = "Daily Closing Report Summary 04May26 from x"
        middle = ax.build_naming(
            filename_template="{subject_date} {name}",
            subject_pattern=r"(?P<date>\d{2}[A-Za-z]{3}\d{2})",
            subject_date_format="%d%b%y",
        )
        assert ax.message_values(
            subject=subject, sender="a@x.com", received=None, sent=None, uid=1,
            naming=middle,
        ) is None
        led = ax.build_naming(
            filename_template="{subject_date} {name}",
            subject_pattern=r".*?(?P<date>\d{2}[A-Za-z]{3}\d{2})",
            subject_date_format="%d%b%y",
        )
        values = ax.message_values(
            subject=subject, sender="a@x.com", received=None, sent=None, uid=1,
            naming=led,
        )
        assert values is not None and values["subject_date"] == datetime(2026, 5, 4)

    def test_an_unused_optional_group_does_not_reject_the_message(self) -> None:
        naming = ax.build_naming(
            filename_template="{report} {name}",
            subject_pattern=r"(?P<report>[A-Za-z ]+?) \d{2}[A-Za-z]{3}\d{2}(?P<tail> from)?",
        )
        values = ax.message_values(
            subject="Credit Card Activity 04May26", sender="a@x.com",
            received=None, sent=None, uid=1, naming=naming,
        )
        assert values is not None and values["report"] == "Credit Card Activity"

    def test_a_used_group_that_matched_nothing_rejects_the_message(self) -> None:
        naming = ax.build_naming(
            filename_template="{report} {name}", subject_pattern=r"(?P<report>[A-Z]*)x",
        )
        assert ax.message_values(
            subject="x", sender="a@x.com", received=None, sent=None, uid=1,
            naming=naming,
        ) is None

    def test_slow_subject_matching_stops_the_preview(self, tmp_path: Path) -> None:
        conn = FakeConnector(StrictClient(_reports(8)))
        # Reading the envelopes is quick; the clock jumps once planning starts.
        ticks = iter([0.0, 1.0, 2.0, 30.0, 60.0, 90.0])
        out = _preview(
            conn, ax.ExportStore(), tmp_path, budget=20.0, clock=lambda: next(ticks)
        )
        assert out["over_budget"] is True
        assert out["preview_id"] is None

    @pytest.mark.parametrize("template", ["{report}/{name}", "a\\b {name}"])
    def test_a_path_in_the_filename_template_is_rejected(self, template: str) -> None:
        with pytest.raises(ValueError, match="filename_template"):
            ax.build_naming(filename_template=template, subject_pattern=PATTERN)


# ---------------------------------------------------- one bad message cannot


class TestOneMessageCannotStopTheExport:
    def test_a_subject_that_cannot_be_encoded(self, tmp_path: Path) -> None:
        """A header can decode to a lone surrogate. Writing it to the manifest
        raised, the batch was retried, and the export never finished."""
        messages = _reports(3)
        messages[2]["subject"] = "=?utf-7?Q?+2AA-?= 04May26 from x"
        out, _, _ = _export(
            tmp_path, messages,
            naming=ax.build_naming(filename_template="{uid} {subject} {name}"),
        )
        assert out["outcome"] == "complete"
        assert out["saved"] == 3
        assert len(_manifest(tmp_path)) == 3

    def test_an_unexpected_failure_is_recorded_and_the_rest_goes_on(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = ax._export_message

        def flaky(entry: Any, uid: int, *args: Any) -> Any:
            if uid == 2:
                raise RuntimeError("something nobody planned for")
            return real(entry, uid, *args)

        monkeypatch.setattr(ax, "_export_message", flaky)
        out, _, _ = _export(tmp_path, _reports(4))
        assert out["outcome"] == "complete"
        assert out["saved"] == 3
        assert out["errors"] == 1
        bad = [r for r in _manifest(tmp_path) if r["status"] == "error"]
        assert bad[0]["uid"] == "2"
        assert "nobody planned" not in bad[0]["note"]  # the type only, no text

    def test_an_empty_body_is_an_error_not_a_message_that_is_gone(
        self, tmp_path: Path
    ) -> None:
        conn = FakeConnector(StrictClient(_reports(3)))
        conn.client.empty_body = {2}
        store = ax.ExportStore()
        preview = _preview(conn, store, tmp_path)
        out = _commit(conn, store, preview["preview_id"])
        assert out["saved"] == 2
        assert out["missing"] == 0
        assert out["errors"] == 1
        row = next(r for r in _manifest(tmp_path) if r["status"] == "error")
        assert "empty body" in row["note"]
        assert row["message_id"]  # still says which message

    def test_a_nul_in_a_subject_does_not_poison_the_manifest(
        self, tmp_path: Path
    ) -> None:
        """A NUL written to the manifest made every later read of it fail."""
        messages = _reports(2)
        messages[1]["subject"] = "Credit Card\x00 Activity\x07 04May26 from x"
        out, _, _ = _export(tmp_path, messages)
        assert out["saved"] == 2
        raw = (tmp_path / ax.MANIFEST_NAME).read_bytes()
        assert b"\x00" not in raw and b"\x07" not in raw
        again = _preview(FakeConnector(StrictClient(messages)), ax.ExportStore(), tmp_path)
        assert "manifest_unreadable" not in again
        assert again["already_exported"] == 2

    def test_a_message_the_server_will_not_deliver_is_given_up_on(
        self, tmp_path: Path
    ) -> None:
        """Its fetch failed on every call, the batch never advanced, and
        nothing behind it was ever exported."""
        conn = FakeConnector(StrictClient(_reports(4)))
        store = ax.ExportStore()
        preview = _preview(conn, store, tmp_path)
        pid = preview["preview_id"]
        real_fetch = conn.client.fetch

        def fetch(uids: list[int], parts: list[str]) -> Any:
            if "BODY.PEEK[]" in parts and 2 in uids:
                raise OSError("connection reset")
            return real_fetch(uids, parts)

        conn.client.fetch = fetch  # type: ignore[method-assign]
        outcomes = []
        for _ in range(12):
            out = _commit(conn, store, pid)
            outcomes.append(out["outcome"])
            if out["outcome"] == "complete":
                break
        assert outcomes[-1] == "complete"
        assert len(_files(tmp_path)) == 3
        bad = [r for r in _manifest(tmp_path) if r["status"] == "error"]
        assert [r["uid"] for r in bad] == ["2"]
        assert "could not be fetched" in bad[0]["note"]

    def test_a_failed_call_does_not_count_rows_it_never_wrote(
        self, tmp_path: Path
    ) -> None:
        messages = {1: _message(1, "Welcome to the portal"), **{
            u + 1: m for u, m in _reports(2).items()
        }}
        conn = FakeConnector(StrictClient(messages))
        store = ax.ExportStore()
        preview = _preview(conn, store, tmp_path)
        conn.client.fail_fetch_on = 1
        out = _commit(conn, store, preview["preview_id"])
        assert out["outcome"] == "failed"
        assert out["pattern_no_match"] == 0
        assert out["messages_done"] == 0

    def test_a_message_built_with_thousands_of_parts_is_capped(
        self, tmp_path: Path
    ) -> None:
        parts = [(f"f{n:03d}.txt", b"x") for n in range(ax.MAX_ATTACHMENTS_PER_MESSAGE + 5)]
        messages = {1: _message(1, SUBJECT, attachments=parts)}
        out, _, _ = _export(tmp_path, messages, naming=_plain())
        assert out["saved"] == ax.MAX_ATTACHMENTS_PER_MESSAGE
        assert out["errors"] == 1
        assert len(_files(tmp_path)) == ax.MAX_ATTACHMENTS_PER_MESSAGE


# ----------------------------------------------------------- sizes and time


class TestLargeMessages:
    def _ready(
        self, tmp_path: Path, count: int = 6
    ) -> tuple[FakeConnector, ax.ExportStore, str, ax.ExportEntry]:
        conn = FakeConnector(StrictClient(_reports(count)))
        store = ax.ExportStore()
        preview = _preview(conn, store, tmp_path)
        return conn, store, preview["preview_id"], store.get(preview["preview_id"])

    def _body_fetches(self, conn: FakeConnector) -> list[list[int]]:
        return [uids for uids, parts in conn.client.fetches if "BODY.PEEK[]" in parts]

    def test_a_message_too_large_for_a_bulk_export_is_recorded_unfetched(
        self, tmp_path: Path
    ) -> None:
        conn, store, pid, entry = self._ready(tmp_path, 4)
        entry.sizes[2] = ax.BULK_MESSAGE_BYTES + 1
        out = _commit(conn, store, pid)
        assert out["outcome"] == "complete"
        assert out["saved"] == 3
        assert out["too_large"] == 1
        assert 2 not in [u for batch in self._body_fetches(conn) for u in batch]
        row = next(r for r in _manifest(tmp_path) if r["status"] == "too_large")
        assert "save_attachments" in row["note"]

    def test_the_servers_own_cap_still_applies_when_it_is_smaller(
        self, tmp_path: Path
    ) -> None:
        conn, store, pid, entry = self._ready(tmp_path, 2)
        entry.sizes[1] = 3 * 1024 * 1024
        out = _commit(conn, store, pid, max_attachment_bytes=1024 * 1024)
        assert out["too_large"] == 1
        assert self._body_fetches(conn) == [[2]]

    def test_a_large_message_is_fetched_alone_as_the_first_work_of_a_call(
        self, tmp_path: Path
    ) -> None:
        conn, store, pid, entry = self._ready(tmp_path, 6)
        entry.sizes[3] = ax.SOLO_MESSAGE_BYTES + 1
        first = _commit(conn, store, pid)
        assert first["outcome"] == "more"
        assert first["messages_done"] == 2
        assert self._body_fetches(conn) == [[1, 2]]
        second = _commit(conn, store, pid)
        assert second["outcome"] == "complete"
        assert self._body_fetches(conn)[1] == [3]
        assert first["saved"] + second["saved"] == 6

    def test_a_batch_stops_at_the_byte_limit(self, tmp_path: Path) -> None:
        conn, store, pid, entry = self._ready(tmp_path, 6)
        for uid in entry.remaining:
            entry.sizes[uid] = 3 * 1024 * 1024
        _commit(conn, store, pid)
        assert self._body_fetches(conn)[0] == [1, 2]  # a third would pass 8 MB

    def test_unknown_sizes_are_read_before_any_body(self, tmp_path: Path) -> None:
        """A message the preview could not size used to count as zero bytes
        and be fetched blind, whatever it weighed."""
        conn = FakeConnector(StrictClient(_reports(3)))
        conn.client.hide_size_once = True
        store = ax.ExportStore()
        preview = _preview(conn, store, tmp_path)
        entry = store.get(preview["preview_id"])
        assert all(entry.sizes[u] == 0 for u in entry.remaining)
        out = _commit(conn, store, preview["preview_id"])
        assert out["saved"] == 3
        parts = [p for _, p in conn.client.fetches]
        assert parts.index(["RFC822.SIZE"]) < next(
            i for i, p in enumerate(parts) if "BODY.PEEK[]" in p
        )
        assert all(size > 0 for size in entry.sizes.values())

    def test_sizes_are_read_for_the_messages_actually_about_to_be_fetched(
        self, tmp_path: Path
    ) -> None:
        """Messages recorded without a download sat in front and used up the
        look-ahead, so the unsized ones behind them were fetched blind."""
        messages = {u: _message(u, "Welcome to the portal") for u in range(1, 13)}
        for uid, row in _reports(3).items():
            messages[20 + uid] = row
        conn = FakeConnector(StrictClient(messages))
        store = ax.ExportStore()
        preview = _preview(conn, store, tmp_path)
        entry = store.get(preview["preview_id"])
        for uid in (21, 22, 23):
            entry.sizes[uid] = 0
        out = _commit(conn, store, preview["preview_id"])
        assert out["saved"] == 3
        assert out["pattern_no_match"] == 12
        assert ([21, 22, 23], ["RFC822.SIZE"]) in conn.client.fetches
        assert all(entry.sizes[u] > 0 for u in (21, 22, 23))

    def test_writing_that_runs_long_leaves_the_rest_for_the_next_call(
        self, tmp_path: Path
    ) -> None:
        conn, store, pid, _ = self._ready(tmp_path, 6)
        ticks = iter([0.0, 1.0, 50.0, 51.0, 52.0, 53.0, 54.0])
        first = _commit(conn, store, pid, time_budget=20.0, clock=lambda: next(ticks))
        assert first["outcome"] == "more"
        assert first["messages_done"] == 1
        assert len(_manifest(tmp_path)) == 1
        second = _commit(conn, store, pid)
        assert second["outcome"] == "complete"
        assert len(_files(tmp_path)) == 6

    def test_adaptive_batch_size(self) -> None:
        assert ax._next_size(8, ax.SLOW_BATCH_SECONDS) == 4
        assert ax._next_size(1, 99.0) == 1
        assert ax._next_size(4, ax.FAST_BATCH_SECONDS) == 8
        assert ax._next_size(8, 0.1) == ax.MAX_BATCH
        assert ax._next_size(6, 5.0) == 6

    def test_subjects_known_not_to_fit_are_recorded_without_a_download(
        self, tmp_path: Path
    ) -> None:
        messages = _reports(2)
        messages[3] = _message(3, "Welcome to the portal")
        conn = FakeConnector(StrictClient(messages))
        store = ax.ExportStore()
        preview = _preview(conn, store, tmp_path)
        out = _commit(conn, store, preview["preview_id"])
        assert out["pattern_no_match"] == 1
        assert out["messages_done"] == 3
        assert self._body_fetches(conn) == [[1, 2]]
        row = next(r for r in _manifest(tmp_path) if r["status"] == "pattern_no_match")
        assert row["subject"] == "Welcome to the portal"
        assert row["message_id"]


# ------------------------------------------------------------------- resume


class TestResumeIsNotFooled:
    def _two_attachments(self) -> dict[int, dict[str, Any]]:
        return {
            1: _message(1, SUBJECT, attachments=[("a.txt", b"one"), ("b.txt", b"two")])
        }

    def _again(self, folder: Path, messages: dict[int, dict[str, Any]], **kw: Any) -> dict[str, Any]:
        args = {"naming": _plain(), **kw}
        return _preview(FakeConnector(StrictClient(messages)), ax.ExportStore(), folder, **args)

    def test_a_message_with_a_failed_attachment_is_tried_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One saved file used to mark the whole message done for good, so
        the attachment that failed was never retried."""
        real_link = os.link
        calls = {"n": 0}

        def flaky(src: Any, dst: Any, **kwargs: Any) -> None:
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError(28, "No space left on device")
            real_link(src, dst, **kwargs)

        monkeypatch.setattr(ax.os, "link", flaky)
        messages = self._two_attachments()
        out, _, _ = _export(tmp_path, messages, naming=_plain())
        assert (out["saved"], out["errors"]) == (1, 1)
        assert self._again(tmp_path, messages)["already_exported"] == 0

        out, _, _ = _export(tmp_path, messages, naming=_plain())
        assert (out["saved"], out["identical_existing"], out["errors"]) == (1, 1, 0)
        assert _files(tmp_path) == ["a.txt", "b.txt"]
        assert self._again(tmp_path, messages)["already_exported"] == 1

    def test_a_name_collision_is_tried_again_once_the_way_is_clear(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "b.txt").write_bytes(b"something else")
        messages = self._two_attachments()
        out, _, _ = _export(tmp_path, messages, naming=_plain())
        assert (out["saved"], out["name_collisions"]) == (1, 1)
        assert self._again(tmp_path, messages)["already_exported"] == 0
        (tmp_path / "b.txt").unlink()
        out, _, _ = _export(tmp_path, messages, naming=_plain())
        assert out["saved"] == 1
        assert (tmp_path / "b.txt").read_bytes() == b"two"

    def test_changed_names_or_filter_export_everything_again(
        self, tmp_path: Path
    ) -> None:
        messages = _reports(4)
        _export(tmp_path, messages)
        assert _preview(
            FakeConnector(StrictClient(messages)), ax.ExportStore(), tmp_path
        )["already_exported"] == 4
        renamed = _naming(filename_template="{report} {subject_date:%Y-%m-%d}{ext}")
        assert _preview(
            FakeConnector(StrictClient(messages)), ax.ExportStore(), tmp_path,
            naming=renamed,
        )["already_exported"] == 0
        assert _preview(
            FakeConnector(StrictClient(messages)), ax.ExportStore(), tmp_path,
            attachment_name_contains=".txt",
        )["already_exported"] == 0

    def test_a_reused_message_id_does_not_hide_another_message(
        self, tmp_path: Path
    ) -> None:
        """Message-IDs are chosen by the sender. Someone who sends mail with
        a known id must not make the real message look already exported."""
        impostor = {1: _message(1, SUBJECT, received=datetime(2026, 5, 1, 9, 0))}
        _export(tmp_path, impostor)
        real = {7: _message(7, SUBJECT, received=datetime(2026, 5, 5, 0, 3))}
        real[7]["message_id"] = impostor[1]["message_id"]
        preview = _preview(FakeConnector(StrictClient(real)), ax.ExportStore(), tmp_path)
        assert preview["already_exported"] == 0
        assert preview["to_export"] == 1

    @pytest.mark.parametrize("lead", ["-", "=", "+", "@"])
    def test_a_message_id_that_looks_like_a_formula_still_resumes(
        self, tmp_path: Path, lead: str
    ) -> None:
        """The manifest writes such a cell with a leading quote. Reading it
        back without removing the quote made the message export every time."""
        messages = _reports(1)
        messages[1]["message_id"] = f"<{lead}123456.7.1700@host>"
        _export(tmp_path, messages)
        assert _manifest(tmp_path)[0]["message_id"].startswith(f"'{lead}")
        again = _preview(FakeConnector(StrictClient(messages)), ax.ExportStore(), tmp_path)
        assert again["already_exported"] == 1

    @pytest.mark.parametrize(
        "value", ["=x", "'=x", "''=x", "'", "''", "'plain", "plain", "-1", "'-1", ""]
    )
    def test_a_cell_reads_back_as_exactly_what_was_written(self, value: str) -> None:
        """A value that itself began with a quote and an equals sign used to
        read back without its quote, as a different value."""
        assert ax._csv_uncell(ax._csv_cell(value)) == value

    def test_an_attachment_name_beginning_with_a_quote_still_resumes(
        self, tmp_path: Path
    ) -> None:
        messages = {1: _message(1, SUBJECT, attachments=[("'=x.pdf", b"x")])}
        _export(tmp_path, messages, naming=_plain())
        assert _files(tmp_path) == ["'=x.pdf"]
        again = _preview(
            FakeConnector(StrictClient(messages)), ax.ExportStore(), tmp_path,
            naming=_plain(),
        )
        assert again["already_exported"] == 1

    def test_a_saved_path_that_looks_like_a_formula_still_resumes(
        self, tmp_path: Path
    ) -> None:
        messages = {1: _message(1, SUBJECT, attachments=[("=sum.txt", b"x")])}
        _export(tmp_path, messages, naming=_plain())
        assert _files(tmp_path) == ["=sum.txt"]
        again = _preview(
            FakeConnector(StrictClient(messages)), ax.ExportStore(), tmp_path,
            naming=_plain(),
        )
        assert again["already_exported"] == 1

    def test_a_row_cut_short_by_a_crash_does_not_swallow_the_next_one(
        self, tmp_path: Path
    ) -> None:
        _export(tmp_path, _reports(2))
        path = tmp_path / ax.MANIFEST_NAME
        path.write_bytes(path.read_bytes().rstrip(b"\r\n") + b"\r\n2026-11-27T00:00:00,iClo")
        conn = FakeConnector(StrictClient(_reports(4)))
        store = ax.ExportStore()
        preview = _preview(conn, store, tmp_path)
        _commit(conn, store, preview["preview_id"])
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        assert [r["status"] for r in rows].count("saved") == 4
        assert rows[2]["exported_at"] == "2026-11-27T00:00:00"  # the torn row, alone

    def test_a_forged_row_pointing_outside_is_not_believed(self, tmp_path: Path) -> None:
        messages = _reports(1)
        target = tmp_path / "exports"
        target.mkdir()
        (tmp_path / "secret.txt").write_bytes(b"12345")
        key = ax.export_key(_naming(), None)
        with (target / ax.MANIFEST_NAME).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=ax.MANIFEST_FIELDS)
            writer.writeheader()
            writer.writerow({
                **dict.fromkeys(ax.MANIFEST_FIELDS, ""),
                "exported_at": "2026-11-27T00:00:00",
                "message_id": messages[1]["message_id"].strip("<>"),
                "date_received": messages[1]["received"].isoformat(),
                "saved_path": "../secret.txt", "size": "5", "status": "saved",
                "export_key": key,
            })
        preview = _preview(FakeConnector(StrictClient(messages)), ax.ExportStore(), target)
        assert preview["already_exported"] == 0
        # The same row pointing at a real file inside is believed, which
        # shows the test above fails for the right reason.
        (target / "ok.txt").write_bytes(b"12345")
        text = (target / ax.MANIFEST_NAME).read_text(encoding="utf-8")
        (target / ax.MANIFEST_NAME).write_text(
            text.replace("../secret.txt", "ok.txt"), encoding="utf-8"
        )
        preview = _preview(FakeConnector(StrictClient(messages)), ax.ExportStore(), target)
        assert preview["already_exported"] == 1


# ---------------------------------------------------------------- directory


class TestDirectory:
    @pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
    def test_a_directory_that_cannot_be_written_is_refused_at_preview(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "locked"
        target.mkdir()
        target.chmod(0o500)
        try:
            with pytest.raises(ValueError, match="not writable"):
                _preview(FakeConnector(StrictClient(_reports(2))), ax.ExportStore(), target)
        finally:
            target.chmod(0o700)

    def test_the_search_uses_the_callers_filters(self, tmp_path: Path) -> None:
        conn = FakeConnector(StrictClient(_reports(2)))
        _preview(
            conn, ax.ExportStore(), tmp_path,
            selector=qm.Selector(
                sender_contains=SENDER, subject_contains="Daily",
                date_from="2026-05-01", date_to="2026-05-07",
            ),
        )
        assert conn.client.searches == [[
            "FROM", SENDER, "SUBJECT", "Daily",
            "SINCE", "01-May-2026", "BEFORE", "08-May-2026",
        ]]
