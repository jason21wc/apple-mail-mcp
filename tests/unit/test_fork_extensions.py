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
from unittest.mock import MagicMock, patch

import pytest

from apple_mail_mcp.server import (
    get_attachment_content,
    get_messages,
    save_attachments,
)


@pytest.fixture
def mock_mail() -> Any:
    with patch("apple_mail_mcp.server.mail") as m:
        yield m


@pytest.fixture
def mock_logger() -> Any:
    with patch("apple_mail_mcp.server.operation_logger") as m:
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
            "apple_mail_mcp.server._resolve_id_list_to_messages", return_value=[]
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
