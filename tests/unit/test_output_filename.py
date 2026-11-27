"""Tests for ``sanitize_output_filename`` and its use by ``save_attachments``.

A name the caller chose keeps its spaces and letters. What must never get
through is anything that could place the file outside the directory it was
asked to go in, or a character a file name cannot safely hold.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from apple_mail_fast_mcp.server import save_attachments
from apple_mail_fast_mcp.utils import sanitize_filename, sanitize_output_filename


class TestSanitizeOutputFilename:
    @pytest.mark.parametrize(
        "name",
        [
            "Fosse Daily Closing Report 2026.05.04.txt",
            "Q1 Report (final) - v2.pdf",
            "Zürich café résumé.pdf",
            "報告書 2026.txt",
            "report_v2-final.TXT",
            "a & b, c + d = e.csv",
        ],
    )
    def test_ordinary_names_are_kept_as_written(self, name: str) -> None:
        assert sanitize_output_filename(name) == name

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("../../etc/passwd", "passwd"),
            ("/etc/passwd", "passwd"),
            ("..", "unnamed_file"),
            ("../", "unnamed_file"),
            ("a/../../b c.txt", "b c.txt"),
            ("..\\..\\windows\\system32.txt", "_.._windows_system32.txt"),
            ("C:\\temp\\x y.txt", "C__temp_x y.txt"),
            ("sub:dir.txt", "sub_dir.txt"),
        ],
    )
    def test_path_parts_cannot_survive(self, name: str, expected: str) -> None:
        out = sanitize_output_filename(name)
        assert out == expected
        assert "/" not in out and "\\" not in out and ":" not in out
        assert out not in ("", ".", "..")
        assert not out.startswith(".")

    @pytest.mark.parametrize(
        "char", ["\x00", "\n", "\r", "\t", "\x1b", "\x7f", "\x85", "\x9f"]
    )
    def test_control_characters_are_replaced(self, char: str) -> None:
        out = sanitize_output_filename(f"my{char}report.txt")
        assert char not in out
        assert out.startswith("my") and out.endswith("report.txt")

    def test_direction_overrides_are_removed(self) -> None:
        """With a right-to-left override, ``invoice<RLO>fdp.exe`` is shown as
        ``invoiceexe.pdf``: the name must not be able to lie about its type."""
        assert sanitize_output_filename("invoice‮fdp.exe") == "invoicefdp.exe"
        assert sanitize_output_filename("a‎‏b⁦c⁩.txt") == "abc.txt"

    def test_joiners_that_scripts_and_emoji_need_are_kept(self) -> None:
        name = "می‌خواهم.txt"  # zero-width non-joiner, part of the word
        assert sanitize_output_filename(name) == name

    def test_leading_dots_are_stripped_so_nothing_is_hidden(self) -> None:
        assert sanitize_output_filename(".hidden report.txt") == "hidden report.txt"
        assert sanitize_output_filename("...x") == "x"

    def test_trailing_dots_and_spaces_are_stripped(self) -> None:
        assert sanitize_output_filename("report 2026.  ") == "report 2026"
        assert sanitize_output_filename("  padded name.txt") == "padded name.txt"

    @pytest.mark.parametrize("name", ["", "   ", "...", "\x00", "/"])
    def test_nothing_left_gets_a_placeholder(self, name: str) -> None:
        assert sanitize_output_filename(name) == "unnamed_file"

    def test_long_names_are_cut_to_fit_and_keep_the_extension(self) -> None:
        out = sanitize_output_filename("x" * 400 + ".txt")
        assert out.endswith(".txt")
        assert len(out.encode("utf-8")) <= 255

    def test_length_is_counted_in_bytes_not_characters(self) -> None:
        out = sanitize_output_filename("報" * 200 + ".txt")  # 3 bytes each
        assert out.endswith(".txt")
        assert len(out.encode("utf-8")) <= 255
        out.encode("utf-8").decode("utf-8")  # no character was cut in half

    def test_unicode_is_normalised_to_one_form(self) -> None:
        decomposed = unicodedata.normalize("NFD", "café.txt")
        assert sanitize_output_filename(decomposed) == unicodedata.normalize(
            "NFC", "café.txt"
        )

    def test_the_strict_sanitizer_is_unchanged(self) -> None:
        """Names that come from mail still get the strict treatment."""
        assert sanitize_filename("my report.txt") == "my_report.txt"
        assert sanitize_filename("../../../etc/passwd") == "passwd"


@pytest.fixture
def mock_mail() -> Any:
    with patch("apple_mail_fast_mcp.server.mail") as m:
        yield m


@pytest.fixture
def mock_logger() -> Any:
    with patch("apple_mail_fast_mcp.server.operation_logger") as m:
        yield m


def _fake_save(**kwargs: Any) -> dict[str, Any]:
    Path(kwargs["save_directory"], "email.txt").write_bytes(b"data")
    return {"saved": 1, "rejected": []}


class TestSaveAttachmentsKeepsSpaces:
    def test_spaces_in_output_filename_are_kept(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Path
    ) -> None:
        mock_mail.save_attachments.side_effect = _fake_save
        name = "Fosse Daily Closing Report 2026.05.04.txt"
        result = save_attachments(
            "1", str(tmp_path), attachment_indices=[0], output_filename=name
        )
        assert result["success"] is True
        assert result["filename"] == name
        assert (tmp_path / name).read_bytes() == b"data"
        assert [p.name for p in tmp_path.iterdir()] == [name]

    @pytest.mark.parametrize(
        "name", ["../../evil name.pdf", "/tmp/evil name.pdf", "sub/dir/evil name.pdf"]
    )
    def test_traversal_is_still_blocked(
        self, mock_mail: MagicMock, mock_logger: MagicMock, tmp_path: Path, name: str
    ) -> None:
        mock_mail.save_attachments.side_effect = _fake_save
        target = tmp_path / "inside"
        target.mkdir()
        result = save_attachments(
            "1", str(target), attachment_indices=[0], output_filename=name
        )
        assert result["success"] is True
        assert result["filename"] == "evil name.pdf"
        assert [p.name for p in target.iterdir()] == ["evil name.pdf"]
        # Nothing was written next to, or above, the requested directory.
        assert [p.name for p in tmp_path.iterdir()] == ["inside"]
