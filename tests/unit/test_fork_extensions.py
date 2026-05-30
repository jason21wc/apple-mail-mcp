"""
Unit tests for fork-specific extensions to the Apple Mail MCP server.

Tests cover:
1. get_attachment_content — server-layer tool that reads attachment content
   without saving to disk (returns text or base64).
2. save_attachments with output_filename — custom filename on save.

Tests derived from code behavior and docstrings. Follows existing patterns
from test_server.py: mock_mail fixture patches the module-level connector,
mock_logger patches operation_logger, and assertions validate the structured
response shape and error_type mapping.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from apple_mail_mcp.exceptions import (
    MailMessageNotFoundError,
)
from apple_mail_mcp.server import (
    get_attachment_content,
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


# ---------------------------------------------------------------------------
# get_attachment_content
# ---------------------------------------------------------------------------


class TestGetAttachmentContent:
    """Tests for the get_attachment_content server tool."""

    def test_happy_path_text_file_returns_content(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        """Text file content is returned with is_binary=False."""
        mock_mail.get_attachment_content.return_value = {
            "name": "report.txt",
            "mime_type": "text/plain",
            "size": 42,
            "content": "Hello, world!",
            "is_binary": False,
        }

        result = get_attachment_content("msg-123", attachment_index=0)

        assert result["success"] is True
        assert result["name"] == "report.txt"
        assert result["mime_type"] == "text/plain"
        assert result["size"] == 42
        assert result["content"] == "Hello, world!"
        assert result["is_binary"] is False
        mock_mail.get_attachment_content.assert_called_once_with(
            message_id="msg-123",
            attachment_index=0,
            account=None,
            mailbox=None,
        )

    def test_binary_file_returns_base64_content(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        """Binary files are returned with base64-encoded content and is_binary=True."""
        mock_mail.get_attachment_content.return_value = {
            "name": "image.png",
            "mime_type": "image/png",
            "size": 1024,
            "content": "iVBORw0KGgoAAAANSUhEUg==",
            "is_binary": True,
        }

        result = get_attachment_content("msg-456", attachment_index=1)

        assert result["success"] is True
        assert result["name"] == "image.png"
        assert result["is_binary"] is True
        assert result["content"] == "iVBORw0KGgoAAAANSUhEUg=="
        mock_mail.get_attachment_content.assert_called_once_with(
            message_id="msg-456",
            attachment_index=1,
            account=None,
            mailbox=None,
        )

    def test_message_not_found_returns_error(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        """MailMessageNotFoundError maps to message_not_found error_type."""
        mock_mail.get_attachment_content.side_effect = MailMessageNotFoundError(
            "not found"
        )

        result = get_attachment_content("nonexistent-id")

        assert result["success"] is False
        assert result["error_type"] == "message_not_found"
        assert "nonexistent-id" in result["error"]

    def test_attachment_index_out_of_range_returns_validation_error(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        """ValueError from connector (e.g. index out of range) maps to validation_error."""
        mock_mail.get_attachment_content.side_effect = ValueError(
            "Attachment index out of range"
        )

        result = get_attachment_content("msg-123", attachment_index=99)

        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "out of range" in result["error"].lower()

    def test_path_traversal_in_attachment_name_returns_validation_error(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        """ValueError raised by connector for path traversal maps to validation_error."""
        mock_mail.get_attachment_content.side_effect = ValueError(
            "Attachment saved outside temp directory: ../../../etc/passwd"
        )

        result = get_attachment_content("msg-123", attachment_index=0)

        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "outside" in result["error"].lower()

    def test_unexpected_exception_maps_to_unknown(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        """Unhandled exceptions map to unknown error_type."""
        mock_mail.get_attachment_content.side_effect = RuntimeError("boom")

        result = get_attachment_content("msg-123")

        assert result["success"] is False
        assert result["error_type"] == "unknown"
        assert "boom" in result["error"]

    def test_default_attachment_index_is_zero(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        """When attachment_index is not provided, defaults to 0."""
        mock_mail.get_attachment_content.return_value = {
            "name": "file.csv",
            "mime_type": "text/csv",
            "size": 100,
            "content": "a,b,c",
            "is_binary": False,
        }

        result = get_attachment_content("msg-789")

        assert result["success"] is True
        mock_mail.get_attachment_content.assert_called_once_with(
            message_id="msg-789",
            attachment_index=0,
            account=None,
            mailbox=None,
        )

    def test_account_and_mailbox_passed_through_for_imap_path(
        self, mock_mail: MagicMock, mock_logger: MagicMock
    ) -> None:
        """account + mailbox forward to the connector to enable the IMAP
        fast path (download-independent attachment fetch)."""
        mock_mail.get_attachment_content.return_value = {
            "name": "04 FS.pdf",
            "mime_type": "application/pdf",
            "size": 2048,
            "content": "JVBERi0=",
            "is_binary": True,
        }

        result = get_attachment_content(
            "<id@host>", attachment_index=0,
            account="iCloud", mailbox="INBOX",
        )

        assert result["success"] is True
        assert result["name"] == "04 FS.pdf"
        mock_mail.get_attachment_content.assert_called_once_with(
            message_id="<id@host>",
            attachment_index=0,
            account="iCloud",
            mailbox="INBOX",
        )


# ---------------------------------------------------------------------------
# save_attachments with output_filename
# ---------------------------------------------------------------------------


class TestSaveAttachmentsOutputFilename:
    """Tests for the output_filename parameter of save_attachments."""

    def test_output_filename_single_attachment_renames_file(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Path
    ) -> None:
        """Single attachment with output_filename renames the saved file."""
        # The connector saves to a temp dir; simulate by creating a file
        # in whatever directory it's called with
        def _mock_save(*, message_id: str, save_directory: Path, attachment_indices: Any) -> int:
            (save_directory / "original_name.txt").write_text("content")
            return 1

        mock_mail.save_attachments.side_effect = _mock_save

        result = save_attachments(
            "msg-1", str(tmp_path), attachment_indices=[0], output_filename="custom.txt"
        )

        assert result["success"] is True
        assert result["saved"] == 1
        assert result["filename"] == "custom.txt"
        # The file should exist at the final destination with the custom name
        assert (tmp_path / "custom.txt").exists()

    def test_output_filename_multiple_indices_returns_error(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Path
    ) -> None:
        """output_filename with multiple attachment indices is rejected."""
        result = save_attachments(
            "msg-1",
            str(tmp_path),
            attachment_indices=[0, 1],
            output_filename="combined.txt",
        )

        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "single attachment" in result["error"].lower()
        mock_mail.save_attachments.assert_not_called()

    def test_output_filename_without_indices_returns_error(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Path
    ) -> None:
        """output_filename with attachment_indices=None is rejected to prevent data loss."""
        result = save_attachments(
            "msg-1",
            str(tmp_path),
            attachment_indices=None,
            output_filename="custom.txt",
        )

        assert result["success"] is False
        assert result["error_type"] == "validation_error"
        assert "single attachment" in result["error"].lower()
        mock_mail.save_attachments.assert_not_called()

    def test_output_filename_sanitized_removes_path_components(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Path
    ) -> None:
        """Path traversal attempts in output_filename are neutralized by sanitize_filename."""
        def _mock_save(*, message_id: str, save_directory: Path, attachment_indices: Any) -> int:
            (save_directory / "original.txt").write_text("data")
            return 1

        mock_mail.save_attachments.side_effect = _mock_save

        result = save_attachments(
            "msg-1",
            str(tmp_path),
            attachment_indices=[0],
            output_filename="../../../etc/passwd",
        )

        assert result["success"] is True
        # sanitize_filename strips path components: "../../../etc/passwd" -> "passwd"
        assert result["filename"] == "passwd"
        assert (tmp_path / "passwd").exists()

    def test_output_filename_sanitized_removes_dangerous_chars(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Path
    ) -> None:
        """Dangerous characters in output_filename are replaced."""
        def _mock_save(*, message_id: str, save_directory: Path, attachment_indices: Any) -> int:
            (save_directory / "original.txt").write_text("data")
            return 1

        mock_mail.save_attachments.side_effect = _mock_save

        result = save_attachments(
            "msg-1",
            str(tmp_path),
            attachment_indices=[0],
            output_filename="file:name<with>bad|chars.txt",
        )

        assert result["success"] is True
        # All dangerous chars replaced with underscores
        assert result["filename"] == "file_name_with_bad_chars.txt"

    def test_output_filename_none_preserves_original_name(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Path
    ) -> None:
        """When output_filename is None, no renaming occurs."""
        mock_mail.save_attachments.return_value = 2

        result = save_attachments("msg-1", str(tmp_path))

        assert result["success"] is True
        assert result["saved"] == 2
        assert "filename" not in result

    def test_output_filename_with_null_bytes_sanitized(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Path
    ) -> None:
        """Null bytes in output_filename are stripped by sanitize_filename."""
        def _mock_save(*, message_id: str, save_directory: Path, attachment_indices: Any) -> int:
            (save_directory / "original.txt").write_text("data")
            return 1

        mock_mail.save_attachments.side_effect = _mock_save

        result = save_attachments(
            "msg-1",
            str(tmp_path),
            attachment_indices=[0],
            output_filename="mal\x00icious.txt",
        )

        assert result["success"] is True
        assert result["filename"] == "malicious.txt"

    def test_output_filename_empty_after_sanitization_becomes_unnamed(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Path
    ) -> None:
        """If filename is empty after sanitization, it becomes 'unnamed_file'."""
        def _mock_save(*, message_id: str, save_directory: Path, attachment_indices: Any) -> int:
            (save_directory / "original.txt").write_text("data")
            return 1

        mock_mail.save_attachments.side_effect = _mock_save

        # Only dots and slashes -> sanitize_filename returns "unnamed_file"
        result = save_attachments(
            "msg-1",
            str(tmp_path),
            attachment_indices=[0],
            output_filename="...",
        )

        assert result["success"] is True
        assert result["filename"] == "unnamed_file"

    def test_output_filename_leading_dots_stripped(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Path
    ) -> None:
        """Leading dots (hidden files) are stripped from output_filename."""
        def _mock_save(*, message_id: str, save_directory: Path, attachment_indices: Any) -> int:
            (save_directory / "original.txt").write_text("data")
            return 1

        mock_mail.save_attachments.side_effect = _mock_save

        result = save_attachments(
            "msg-1",
            str(tmp_path),
            attachment_indices=[0],
            output_filename=".hidden_file.txt",
        )

        assert result["success"] is True
        assert result["filename"] == "hidden_file.txt"
