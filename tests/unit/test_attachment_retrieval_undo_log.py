"""Unit tests for the attachment-retrieval skill's undo_log.py helper.

The helper is skill-local tooling (.claude/skills/attachment-retrieval/), not part
of the apple_mail_fast_mcp package, so it's loaded by file path. It is pure stdlib and
needs no Mail.app — all state lives under APPLE_MAIL_MCP_HOME, pointed at a tmp dir.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

import pytest

_HELPER = (
    Path(__file__).resolve().parents[2]
    / ".claude"
    / "skills"
    / "attachment-retrieval"
    / "undo_log.py"
)
_spec = importlib.util.spec_from_file_location("undo_log", _HELPER)
assert _spec and _spec.loader
undo_log = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(undo_log)


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any]]:
    code = undo_log.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


@pytest.fixture(autouse=True)
def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
    return tmp_path


def _saved_file(tmp_path: Path, name: str = "report.txt", body: str = "hello") -> Path:
    dest = tmp_path / "dest"
    dest.mkdir(exist_ok=True)
    f = dest / name
    f.write_text(body)
    return f


class TestUndoLog:
    def test_new_run_id_is_time_sortable(self, capsys: pytest.CaptureFixture[str]) -> None:
        code, res = _run(capsys, "new-run-id", "--recipe", "marriott")
        assert code == 0 and res["success"]
        assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{6}", res["run_id"])

    def test_invalid_recipe_name_rejected(self, capsys: pytest.CaptureFixture[str]) -> None:
        code, res = _run(capsys, "new-run-id", "--recipe", "../evil")
        assert code == 1 and res["success"] is False
        assert res["error_type"] == "invalid_recipe_name"

    def test_record_requires_existing_file(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        code, res = _run(
            capsys,
            "record",
            "--recipe",
            "r",
            "--run-id",
            "x",
            "--dest-path",
            str(tmp_path / "nope.txt"),
        )
        assert code == 1 and res["error_type"] == "dest_missing"

    def test_record_then_undo_roundtrip(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        f = _saved_file(tmp_path)
        run = "20260620T000000Z-aaaaaa"
        code, res = _run(capsys, "record", "--recipe", "r", "--run-id", run, "--dest-path", str(f))
        assert code == 0 and res["recorded"] == 1
        _, runs = _run(capsys, "list-runs", "--recipe", "r")
        assert runs["runs"][0] == {
            "run_id": run,
            "saved_at_first": runs["runs"][0]["saved_at_first"],
            "saved": 1,
            "reverted": 0,
        }
        _, undo = _run(capsys, "undo", "--recipe", "r", "--last")
        assert undo["deleted"] == 1 and undo["missing"] == 0
        assert not f.exists()
        # idempotent: undoing the same (now reverted) run by id deletes nothing.
        _, again = _run(capsys, "undo", "--recipe", "r", "--run-id", run)
        assert again["deleted"] == 0 and again["success"] is True

    def test_undo_last_with_nothing_saved_errors(self, capsys: pytest.CaptureFixture[str]) -> None:
        code, res = _run(capsys, "undo", "--recipe", "empty", "--last")
        assert code == 1 and res["error_type"] == "nothing_to_undo"

    def test_undo_skips_modified_file(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        f = _saved_file(tmp_path)
        _run(
            capsys,
            "record",
            "--recipe",
            "r",
            "--run-id",
            "20260620T000000Z-bbbbbb",
            "--dest-path",
            str(f),
        )
        f.write_text("EDITED BY USER")  # changes sha256
        _, undo = _run(capsys, "undo", "--recipe", "r", "--last")
        assert undo["modified_skipped"] == 1 and undo["deleted"] == 0
        assert f.exists() and f.read_text() == "EDITED BY USER"  # left untouched

    def test_undo_missing_file_marked_reverted(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        f = _saved_file(tmp_path)
        _run(
            capsys,
            "record",
            "--recipe",
            "r",
            "--run-id",
            "20260620T000000Z-cccccc",
            "--dest-path",
            str(f),
        )
        f.unlink()  # user already deleted it
        _, undo = _run(capsys, "undo", "--recipe", "r", "--last")
        assert undo["missing"] == 1 and undo["deleted"] == 0
        _, runs = _run(capsys, "list-runs", "--recipe", "r")
        assert runs["runs"][0]["reverted"] == 1 and runs["runs"][0]["saved"] == 0

    def test_atomic_write_leaves_no_tmp(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        f = _saved_file(tmp_path)
        _run(
            capsys,
            "record",
            "--recipe",
            "r",
            "--run-id",
            "20260620T000000Z-dddddd",
            "--dest-path",
            str(f),
        )
        runs_dir = tmp_path / "retrieval_runs"
        stray = [p.name for p in runs_dir.iterdir() if p.name.startswith(".") or p.suffix == ".tmp"]
        assert stray == []
        assert (runs_dir / "r.json").is_file()

    def test_records_are_append_only_across_runs(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        f1 = _saved_file(tmp_path, "a.txt")
        f2 = _saved_file(tmp_path, "b.txt")
        _run(
            capsys,
            "record",
            "--recipe",
            "r",
            "--run-id",
            "20260620T000000Z-111111",
            "--dest-path",
            str(f1),
        )
        _run(
            capsys,
            "record",
            "--recipe",
            "r",
            "--run-id",
            "20260621T000000Z-222222",
            "--dest-path",
            str(f2),
        )
        _run(capsys, "undo", "--recipe", "r", "--last")  # undoes only run 222222
        data = json.loads((tmp_path / "retrieval_runs" / "r.json").read_text())
        assert len(data["records"]) == 2  # nothing deleted from the log
        assert not f2.exists() and f1.exists()  # only the latest run reverted

    def test_corrupt_log_returns_clean_error(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        runs_dir = tmp_path / "retrieval_runs"
        runs_dir.mkdir()
        (runs_dir / "bad.json").write_text("not json at all")
        code, res = _run(capsys, "list-runs", "--recipe", "bad")
        assert code == 1 and res["error_type"] == "corrupt_log"
        # also the wrong-shape case (records not a list)
        (runs_dir / "shape.json").write_text('{"records": "nope"}')
        code2, res2 = _run(capsys, "undo", "--recipe", "shape", "--last")
        assert code2 == 1 and res2["error_type"] == "corrupt_log"

    def test_undo_mixed_outcomes_in_one_run(
        self, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        run = "20260620T000000Z-eeeeee"
        f_ok = _saved_file(tmp_path, "ok.txt", "keep")
        f_edit = _saved_file(tmp_path, "edit.txt", "orig")
        f_gone = _saved_file(tmp_path, "gone.txt", "bye")
        for f in (f_ok, f_edit, f_gone):
            _run(capsys, "record", "--recipe", "m", "--run-id", run, "--dest-path", str(f))
        f_edit.write_text("USER EDITED")  # -> modified_skipped
        f_gone.unlink()  # -> missing
        _, undo = _run(capsys, "undo", "--recipe", "m", "--last")
        assert undo["deleted"] == 1  # only f_ok
        assert undo["modified_skipped"] == 1  # f_edit
        assert undo["missing"] == 1  # f_gone
        assert not f_ok.exists()  # deleted
        assert f_edit.exists() and f_edit.read_text() == "USER EDITED"  # preserved
