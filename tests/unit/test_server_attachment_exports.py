"""Server-layer tests for preview_attachment_export / commit_attachment_export.

``attachment_exports`` itself is covered in test_attachment_exports.py.
These check what the tool wrappers add: gates, directory checks, structured
errors, and that the response carries counts rather than mail content.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from apple_mail_fast_mcp import attachment_exports, server
from apple_mail_fast_mcp.exceptions import MailKeychainEntryNotFoundError
from tests.unit.test_attachment_exports import (
    PATTERN,
    SENDER,
    FakeConnector,
    StrictClient,
    _files,
    _manifest,
    _reports,
)


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> FakeConnector:
    connector = FakeConnector(StrictClient(_reports(8)))
    mail = MagicMock()
    mail.max_attachment_bytes = 100 * 1024 * 1024
    monkeypatch.setattr(server, "mail", mail)
    monkeypatch.setattr(server, "_export_store", attachment_exports.ExportStore())
    monkeypatch.setattr(server, "_export_connector", lambda _account: connector)
    monkeypatch.delenv("MAIL_TEST_MODE", raising=False)
    return connector


async def _preview(folder: Path, **overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "account": "iCloud",
        "mailbox": "_Fosse Export",
        "save_directory": str(folder),
        "sender_contains": SENDER,
        "date_from": "2026-05-01",
        "date_to": "2026-05-07",
        "subject_pattern": PATTERN,
        "subject_date_format": "%d%b%y",
        "filename_template": "Fosse {report} {subject_date:%Y.%m.%d}{ext}",
        "subfolder_template": "{subject_date:%Y}/{report}",
    }
    args.update(overrides)
    return await server.preview_attachment_export(**args)


class TestPreviewTool:
    async def test_success_is_marked_untrusted_and_writes_nothing(
        self, backend: FakeConnector, tmp_path: Path
    ) -> None:
        out = await _preview(tmp_path)
        assert out["success"] is True
        assert out["matched"] == 8
        assert out["to_export"] == 8
        assert out["preview_id"]
        assert out["content_is_untrusted"] is True
        assert list(tmp_path.iterdir()) == []

    async def test_tilde_is_expanded(
        self, backend: FakeConnector, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "Reports").mkdir()
        out = await _preview(Path("~/Reports"))
        assert out["success"] is True
        assert out["manifest_path"].startswith(str((tmp_path / "Reports").resolve()))

    async def test_missing_directory(self, backend: FakeConnector, tmp_path: Path) -> None:
        out = await _preview(tmp_path / "nope")
        assert out["success"] is False
        assert out["error_type"] == "directory_not_found"
        assert backend.client.searches == []

    async def test_a_file_is_not_a_directory(
        self, backend: FakeConnector, tmp_path: Path
    ) -> None:
        target = tmp_path / "file.txt"
        target.write_text("x")
        out = await _preview(target)
        assert out["error_type"] == "invalid_directory"

    @pytest.mark.parametrize(
        "overrides",
        [
            {"filename_template": "{nope}"},
            {"filename_template": "{name:>999999999}"},
            {"subfolder_template": "../up"},
            {"subject_pattern": "(a+)+$"},
            {"if_exists": "clobber"},
            {"sender_contains": " "},
            {"attachment_name_contains": ""},
            {"date_from": "05/01/2026"},
            {"sender_contains": None, "date_from": None, "date_to": None},
        ],
    )
    async def test_bad_arguments_are_validation_errors(
        self, backend: FakeConnector, tmp_path: Path, overrides: dict[str, Any]
    ) -> None:
        out = await _preview(tmp_path, **overrides)
        assert out["success"] is False
        assert out["error_type"] == "validation_error"
        assert backend.client.searches == []
        assert list(tmp_path.iterdir()) == []

    async def test_without_imap_it_is_imap_required(
        self, backend: FakeConnector, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_imap(_account: str) -> Any:
            raise MailKeychainEntryNotFoundError("no password stored")

        monkeypatch.setattr(server, "_export_connector", no_imap)
        out = await _preview(tmp_path)
        assert out["error_type"] == "imap_required"

    async def test_a_bad_argument_is_reported_before_imap_is_even_tried(
        self, backend: FakeConnector, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The connector is built on first use, after validation, so the
        caller learns about the argument rather than a missing password."""
        built: list[str] = []

        def no_imap(account: str) -> Any:
            built.append(account)
            raise MailKeychainEntryNotFoundError("no password stored")

        monkeypatch.setattr(server, "_export_connector", no_imap)
        out = await _preview(tmp_path, if_exists="clobber")
        assert out["error_type"] == "validation_error"
        assert built == []

    async def test_the_connector_is_built_once_per_call(
        self, backend: FakeConnector, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        built: list[str] = []

        def counting(account: str) -> Any:
            built.append(account)
            return backend

        monkeypatch.setattr(server, "_export_connector", counting)
        out = await _preview(tmp_path)
        assert out["success"] is True
        assert built == ["iCloud"]

    async def test_test_mode_blocks_a_real_account(
        self, backend: FakeConnector, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MAIL_TEST_MODE", "true")
        monkeypatch.setenv("MAIL_TEST_ACCOUNT", "TestAccount")
        out = await _preview(tmp_path)
        assert out["success"] is False
        assert backend.client.searches == []


class TestCommitTool:
    async def test_exports_and_reports_counts_only(
        self, backend: FakeConnector, tmp_path: Path
    ) -> None:
        preview = await _preview(tmp_path)
        out = await server.commit_attachment_export(preview["preview_id"], max_count=10)
        assert out["success"] is True
        assert out["outcome"] == "complete"
        assert out["saved"] == 8
        assert len(_files(tmp_path)) == 8
        assert len(_manifest(tmp_path)) == 8
        assert "Daily" not in str({k: v for k, v in out.items() if k != "manifest_path"})

    async def test_unknown_preview(self, backend: FakeConnector) -> None:
        out = await server.commit_attachment_export("nope", max_count=10)
        assert out["success"] is False
        assert out["error_type"] == "preview_not_found"

    async def test_max_count_exceeded(self, backend: FakeConnector, tmp_path: Path) -> None:
        preview = await _preview(tmp_path)
        out = await server.commit_attachment_export(preview["preview_id"], max_count=3)
        assert out["error_type"] == "max_count_exceeded"
        assert _files(tmp_path) == []

    async def test_a_failed_fetch_is_reported_with_progress(
        self, backend: FakeConnector, tmp_path: Path
    ) -> None:
        preview = await _preview(tmp_path)
        server._export_store.get(preview["preview_id"]).batch_size = 4
        backend.client.fail_fetch_on = 2
        out = await server.commit_attachment_export(preview["preview_id"], max_count=10)
        assert out["success"] is False
        assert out["error_type"] == "imap_error"
        assert out["outcome"] == "failed"
        assert out["saved"] == 4
        assert out["remaining"] == 4
        assert out["server_slow"] is True

    async def test_a_problem_with_the_directory_is_not_called_an_imap_error(
        self, backend: FakeConnector, tmp_path: Path
    ) -> None:
        preview = await _preview(tmp_path)
        (tmp_path / attachment_exports.MANIFEST_NAME).write_bytes(b"not,ours\r\n1,2\r\n")
        out = await server.commit_attachment_export(preview["preview_id"], max_count=10)
        assert out["success"] is False
        assert out["error_type"] == "directory_error"
        assert "not a manifest" in out["error"]

    async def test_audit_log_failure_does_not_hide_the_result(
        self, backend: FakeConnector, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        preview = await _preview(tmp_path)

        def boom(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("log full")

        monkeypatch.setattr(server.operation_logger, "log_operation", boom)
        out = await server.commit_attachment_export(preview["preview_id"], max_count=10)
        assert out["success"] is True
        assert out["saved"] == 8
        assert out["audit_log_error"] == "log full"

    async def test_the_attachment_byte_cap_is_the_servers(
        self, backend: FakeConnector, tmp_path: Path
    ) -> None:
        server.mail.max_attachment_bytes = 3
        preview = await _preview(tmp_path)
        out = await server.commit_attachment_export(preview["preview_id"], max_count=10)
        assert out["saved"] == 0
        assert out["too_large"] == 8
