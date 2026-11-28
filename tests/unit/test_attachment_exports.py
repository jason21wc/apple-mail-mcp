"""Unit tests for ``attachment_exports`` (fork-local).

The IMAP client is a strict fake: it has only the read commands the export
is allowed to use, so any attempt to flag, copy, move or delete a message
fails the test with an AttributeError. No test touches the network.
"""

from __future__ import annotations

import csv
import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
from imapclient.response_types import Address, Envelope

from apple_mail_fast_mcp import attachment_exports as ax
from apple_mail_fast_mcp import query_moves as qm
from tests.unit.test_imap_connector import _LEAF_TEXT, _MULTIPART_WITH_ATTACHMENT

SENDER = "lp@denbt1.marriott.com"
PATTERN = r"^(?P<report>.+?) (?P<date>\d{2}[A-Za-z]{3}\d{2}) from"
KINDS = [
    "Daily Closing Report Summary",
    "Credit Card Activity",
    "Daily Segmentation Report",
    "Detail Aging Report",
]


def _raw(subject: str, attachments: list[tuple[str, bytes]], message_id: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = SENDER
    msg["To"] = "owner@example.com"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg.set_content("report attached")
    for name, payload in attachments:
        msg.add_attachment(
            payload, maintype="application", subtype="octet-stream", filename=name
        )
    return msg.as_bytes()


def _message(
    uid: int,
    subject: str,
    attachments: list[tuple[str, bytes]] | None = None,
    received: datetime | None = None,
    sender: str = SENDER,
) -> dict[str, Any]:
    if attachments is None:
        attachments = [("email.txt", f"body of {uid}".encode())]
    message_id = f"<m{uid}@denbt1.marriott.com>"
    return {
        "subject": subject,
        "sender": sender,
        "received": received or datetime(2026, 5, 5, 0, 3),
        "message_id": message_id,
        "raw": _raw(subject, attachments, message_id),
        "has_attachments": bool(attachments),
    }


def _reports(count: int = 8) -> dict[int, dict[str, Any]]:
    """Four report types a day, dated in the subject, received a day later."""
    out: dict[int, dict[str, Any]] = {}
    for uid in range(1, count + 1):
        day = 1 + (uid - 1) // 4
        out[uid] = _message(
            uid,
            f"{KINDS[(uid - 1) % 4]} {day:02d}May26 from denbt1.marriott.com",
            received=datetime(2026, 5, day + 1, 0, 3 + uid),
        )
    return out


class StrictClient:
    """Only what a read-only export may call. Anything else does not exist."""

    def __init__(self, messages: dict[int, dict[str, Any]], uidvalidity: int = 7) -> None:
        self.messages = messages
        self.uidvalidity = uidvalidity
        self.selected: list[tuple[str, bool]] = []
        self.fetches: list[tuple[list[int], list[str]]] = []
        self.searches: list[list[Any]] = []
        self.fail_fetch_on: int | None = None  # 1-based body-fetch number
        self._body_fetches = 0
        self.hide_size_once = False  # the first size read reports nothing
        self.empty_body: set[int] = set()  # UIDs answered with no body
        self.hide_structure = False  # the server sends no BODYSTRUCTURE

    def select_folder(self, name: str, readonly: bool = False) -> dict[bytes, Any]:
        self.selected.append((name, readonly))
        return {b"UIDVALIDITY": self.uidvalidity}

    def search(self, criteria: list[Any], charset: str | None = None) -> list[int]:
        self.searches.append(criteria)
        return sorted(self.messages)

    def fetch(self, uids: list[int], parts: list[str]) -> dict[int, dict[bytes, Any]]:
        self.fetches.append((list(uids), list(parts)))
        if "BODY.PEEK[]" in parts:
            self._body_fetches += 1
            if self.fail_fetch_on == self._body_fetches:
                raise TimeoutError("timed out")
        out: dict[int, dict[bytes, Any]] = {}
        for uid in uids:
            row = self.messages.get(uid)
            if row is None:
                continue
            data: dict[bytes, Any] = {}
            if "ENVELOPE" in parts:
                mailbox, host = row["sender"].split("@")
                addr = Address(None, None, mailbox.encode(), host.encode())
                to = (Address(None, None, b"owner", b"example.com"),)
                cc = tuple(
                    Address(None, None, *(part.encode() for part in a.split("@")))
                    for a in row.get("cc", ())
                ) or None
                data[b"ENVELOPE"] = Envelope(
                    row["received"], row["subject"].encode(), (addr,), (addr,),
                    (addr,), to, cc, None, None, row["message_id"].encode(),
                )
            if "BODYSTRUCTURE" in parts and not self.hide_structure:
                data[b"BODYSTRUCTURE"] = (
                    _MULTIPART_WITH_ATTACHMENT
                    if row.get("has_attachments", True) else _LEAF_TEXT
                )
            if "INTERNALDATE" in parts:
                data[b"INTERNALDATE"] = row["received"]
            if "RFC822.SIZE" in parts and not self.hide_size_once:
                data[b"RFC822.SIZE"] = len(row["raw"])
            if "BODY.PEEK[]" in parts:
                data[b"BODY[]"] = b"" if uid in self.empty_body else row["raw"]
            out[uid] = data
        if "RFC822.SIZE" in parts:
            self.hide_size_once = False
        return out


class FakeConnector:
    def __init__(self, client: StrictClient) -> None:
        self.client = client
        self._host = "imap.example.com"
        self._email = "me@example.com"
        self.left_with: list[BaseException | None] = []

    @contextmanager
    def _session(self) -> Iterator[StrictClient]:
        try:
            yield self.client
        except BaseException as exc:
            self.left_with.append(exc)
            raise
        else:
            self.left_with.append(None)


def _naming(**overrides: Any) -> ax.Naming:
    args: dict[str, Any] = {
        "filename_template": "Fosse {report} {subject_date:%Y.%m.%d}{ext}",
        "subfolder_template": "{subject_date:%Y}/{report}",
        "subject_pattern": PATTERN,
        "subject_date_format": "%d%b%y",
    }
    args.update(overrides)
    return ax.build_naming(**args)


def _preview(
    conn: FakeConnector, store: ax.ExportStore, folder: Path, **overrides: Any
) -> dict[str, Any]:
    args: dict[str, Any] = {
        "account": "iCloud",
        "mailbox": "_Fosse Export",
        "save_directory": folder,
        "selector": qm.Selector(sender_contains=SENDER, date_from="2026-05-01"),
        "naming": _naming(),
    }
    args.update(overrides)
    return ax.preview(conn, store, **args)


def _commit(
    conn: FakeConnector, store: ax.ExportStore, preview_id: str, **overrides: Any
) -> dict[str, Any]:
    args: dict[str, Any] = {
        "preview_id": preview_id,
        "max_count": 5000,
        "max_attachment_bytes": 100 * 1024 * 1024,
    }
    args.update(overrides)
    return ax.commit(conn, store, **args)


def _manifest(folder: Path) -> list[dict[str, str]]:
    with (folder / ax.MANIFEST_NAME).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _files(folder: Path) -> list[str]:
    return sorted(
        str(p.relative_to(folder))
        for p in folder.rglob("*")
        if p.is_file() and not p.name.startswith(".") and p.name != ax.MANIFEST_NAME
        # The test suite points the app's data directory at tmp_path, so the
        # export's lock files land beside the exported files here.
        and "export_locks" not in p.parts
    )


def _export(
    folder: Path, messages: dict[int, dict[str, Any]], **overrides: Any
) -> tuple[dict[str, Any], FakeConnector, ax.ExportStore]:
    conn = FakeConnector(StrictClient(messages))
    store = ax.ExportStore()
    preview = _preview(conn, store, folder, **overrides)
    assert preview["preview_id"], preview
    return _commit(conn, store, preview["preview_id"]), conn, store


# --------------------------------------------------------------- templates


class TestBuildNaming:
    def test_the_daily_report_case(self) -> None:
        naming = _naming()
        values = ax.message_values(
            subject="Daily Closing Report Summary 04May26 from denbt1.marriott.com",
            sender=SENDER, received=datetime(2026, 5, 5, 0, 3), sent=None, uid=9,
            naming=naming,
        )
        assert values is not None
        assert ax.render_path(naming, values, name="email.txt", index=0) == (
            ("2026", "Daily Closing Report Summary"),
            "Fosse Daily Closing Report Summary 2026.05.04.txt",
        )

    def test_default_keeps_the_attachment_name(self) -> None:
        naming = ax.build_naming()
        values = ax.message_values(
            subject="anything", sender="a@x.com", received=datetime(2026, 1, 2),
            sent=None, uid=1, naming=naming,
        )
        assert values is not None
        assert ax.render_path(naming, values, name="Q1 report.pdf", index=0) == (
            (), "Q1 report.pdf",
        )

    def test_received_and_sent_and_plain_tokens(self) -> None:
        naming = ax.build_naming(
            filename_template="{received:%Y-%m-%d} {sent:%H%M} {sender} {uid} {index} {stem}{ext}",
            subfolder_template="{received:%Y}/{received:%m}",
        )
        values = ax.message_values(
            subject="s", sender="a@x.com", received=datetime(2026, 5, 5, 0, 3),
            sent=datetime(2026, 5, 4, 23, 58), uid=42, naming=naming,
        )
        assert values is not None
        assert ax.render_path(naming, values, name="email.txt", index=1) == (
            ("2026", "05"), "2026-05-05 2358 a@x.com 42 1 email.txt",
        )

    def test_a_date_token_without_a_format_is_iso(self) -> None:
        naming = ax.build_naming(filename_template="{received} {name}")
        values = ax.message_values(
            subject="s", sender="a@x.com", received=datetime(2026, 5, 5, 0, 3),
            sent=None, uid=1, naming=naming,
        )
        assert values is not None
        assert ax.render_path(naming, values, name="a.txt", index=0)[1] == "2026-05-05 a.txt"

    def test_subject_that_does_not_match_gives_no_values(self) -> None:
        naming = _naming()
        assert ax.message_values(
            subject="Something else entirely", sender=SENDER,
            received=datetime(2026, 5, 5), sent=None, uid=1, naming=naming,
        ) is None

    def test_subject_date_that_does_not_parse_gives_no_values(self) -> None:
        naming = _naming()
        assert ax.message_values(
            subject="Daily Closing Report Summary 99Xyz26 from denbt1.marriott.com",
            sender=SENDER, received=datetime(2026, 5, 5), sent=None, uid=1,
            naming=naming,
        ) is None

    def test_a_slash_in_a_value_cannot_add_a_folder_or_cut_the_name(self) -> None:
        naming = ax.build_naming(
            filename_template="{subject} {name}", subfolder_template="{subject}",
        )
        values = ax.message_values(
            subject="A/B ../../etc report", sender="a@x.com",
            received=datetime(2026, 1, 1), sent=None, uid=1, naming=naming,
        )
        assert values is not None
        folders, filename = ax.render_path(naming, values, name="x/y.txt", index=0)
        assert len(folders) == 1
        for part in (*folders, filename):
            assert "/" not in part and part not in (".", "..")
        assert filename == "A_B .._.._etc report x_y.txt"

    @pytest.mark.parametrize(
        "template",
        [
            "{name.__class__}",           # attribute access
            "{name[0]}",                  # index access
            "{name!r}",                   # conversion
            "{unknown}",                  # not a token
            "{name:>999999999}",          # format spec on a text token
            "{received:%Y{uid}}",         # nested field in a spec
            "{received:%s}",              # a directive that is not allowed
            "{received:%Y/%m}",           # a separator smuggled into a spec
            "{}",                         # positional
            "",                           # empty
            "x" * 400,                    # too long
        ],
    )
    def test_unsafe_or_unknown_templates_are_rejected(self, template: str) -> None:
        with pytest.raises(ValueError):
            ax.build_naming(filename_template=template)

    @pytest.mark.parametrize(
        "subfolder", ["/abs", "a//b", "../up", "a/../b", "a/./b", "{unknown}/x"]
    )
    def test_unsafe_subfolders_are_rejected(self, subfolder: str) -> None:
        with pytest.raises(ValueError):
            ax.build_naming(subfolder_template=subfolder)

    @pytest.mark.parametrize(
        "pattern",
        [
            r"(a+)+$",                   # nested quantifier
            r"(a|aa)+$",                 # alternation under a repeat
            r"(?P<x>a)\1",               # backreference
            r"(?P<x>a)(?P=x)",           # named backreference
            r"a(?=b)",                   # lookahead
            r"(?<!a)b",                  # lookbehind
            r"(unclosed",                # does not compile
            "a" * 201,                   # too long
            r"(?P<name>.+)",             # collides with a built-in token
        ],
    )
    def test_unsafe_subject_patterns_are_rejected(self, pattern: str) -> None:
        with pytest.raises(ValueError):
            ax.build_naming(filename_template="{name}", subject_pattern=pattern)

    def test_subject_date_needs_a_date_group_and_a_format(self) -> None:
        with pytest.raises(ValueError, match="subject_date"):
            ax.build_naming(filename_template="{subject_date} {name}")
        with pytest.raises(ValueError, match="subject_date_format"):
            ax.build_naming(
                filename_template="{subject_date} {name}", subject_pattern=PATTERN
            )
        with pytest.raises(ValueError, match="subject_date_format"):
            ax.build_naming(
                filename_template="{subject_date} {name}", subject_pattern=PATTERN,
                subject_date_format="%d%b%y %s",
            )

    def test_a_group_token_needs_the_pattern_to_define_it(self) -> None:
        with pytest.raises(ValueError, match="report"):
            ax.build_naming(filename_template="{report} {name}")


# ----------------------------------------------------------------- preview


class TestPreview:
    def test_reports_the_set_and_writes_nothing(self, tmp_path: Path) -> None:
        conn = FakeConnector(StrictClient(_reports(8)))
        out = _preview(conn, ax.ExportStore(), tmp_path)
        assert out["matched"] == 8
        assert out["preview_id"]
        assert out["pattern_no_match"] == 0
        assert out["already_exported"] == 0
        assert out["duplicate_target_paths"] == 0
        assert out["oldest_date"] == "2026-05-02"
        assert out["newest_date"] == "2026-05-03"
        assert out["senders"] == [{"address": SENDER, "count": 8}]
        assert out["total_message_bytes"] > 0
        assert len(out["sample_paths"]) == 5
        assert out["sample_paths"][0] == (
            "2026/Daily Closing Report Summary/"
            "Fosse Daily Closing Report Summary 2026.05.01.EXT"
        )
        assert list(tmp_path.iterdir()) == []
        assert conn.client.selected == [("_Fosse Export", True)]
        # Message bodies are not read by a preview.
        assert not any("BODY.PEEK[]" in parts for _, parts in conn.client.fetches)

    def test_counts_subjects_the_pattern_does_not_match(self, tmp_path: Path) -> None:
        messages = _reports(4)
        messages[5] = _message(5, "Welcome to the portal")
        out = _preview(FakeConnector(StrictClient(messages)), ax.ExportStore(), tmp_path)
        assert out["matched"] == 5
        assert out["pattern_no_match"] == 1

    def test_reports_messages_that_would_share_a_path(self, tmp_path: Path) -> None:
        messages = _reports(4)
        messages[5] = _message(5, messages[1]["subject"])  # the report sent again
        out = _preview(FakeConnector(StrictClient(messages)), ax.ExportStore(), tmp_path)
        assert out["duplicate_target_paths"] == 2

    def test_over_the_ceiling_gives_windows_and_no_id(self, tmp_path: Path) -> None:
        conn = FakeConnector(StrictClient(_reports(8)))
        out = _preview(conn, ax.ExportStore(), tmp_path, ceiling=5)
        assert out["matched"] == 8
        assert out["preview_id"] is None
        assert out["over_limit"] is True
        assert out["limit"] == 5
        assert sum(w["approx_matched"] for w in out["suggested_windows"]) == 8

    def test_out_of_time_gives_no_id(self, tmp_path: Path) -> None:
        messages = {u: _message(u, f"Credit Card Activity 0{1 + u % 9}May26 from x")
                    for u in range(1, 301)}
        ticks = iter([0.0, 1.0, 30.0, 31.0, 32.0, 33.0])
        out = _preview(
            FakeConnector(StrictClient(messages)), ax.ExportStore(), tmp_path,
            budget=20.0, clock=lambda: next(ticks),
        )
        assert out["preview_id"] is None
        assert out["over_budget"] is True
        assert out["matched"] == 300

    def test_nothing_matched(self, tmp_path: Path) -> None:
        out = _preview(FakeConnector(StrictClient({})), ax.ExportStore(), tmp_path)
        assert out["matched"] == 0
        assert out["preview_id"] is None

    def test_an_open_selector_is_refused(self, tmp_path: Path) -> None:
        conn = FakeConnector(StrictClient(_reports(4)))
        with pytest.raises(ValueError, match="at least one"):
            _preview(conn, ax.ExportStore(), tmp_path, selector=qm.Selector())
        assert conn.client.searches == []

    @pytest.mark.parametrize("value", ["", "  "])
    def test_a_blank_attachment_filter_is_rejected(
        self, tmp_path: Path, value: str
    ) -> None:
        with pytest.raises(ValueError, match="attachment_name_contains"):
            _preview(
                FakeConnector(StrictClient(_reports(4))), ax.ExportStore(), tmp_path,
                attachment_name_contains=value,
            )

    def test_unknown_if_exists_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="if_exists"):
            _preview(
                FakeConnector(StrictClient(_reports(4))), ax.ExportStore(), tmp_path,
                if_exists="clobber",
            )

    def test_a_missing_directory_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="save_directory"):
            _preview(
                FakeConnector(StrictClient(_reports(4))), ax.ExportStore(),
                tmp_path / "nope",
            )


# ------------------------------------------------------------------ commit


class TestCommit:
    def test_exports_every_attachment_under_its_rendered_name(
        self, tmp_path: Path
    ) -> None:
        out, conn, _ = _export(tmp_path, _reports(8))
        assert out["outcome"] == "complete"
        assert out["messages_done"] == 8
        assert out["saved"] == 8
        assert out["remaining"] == 0
        assert out["name_collisions"] == 0
        assert out["manifest_path"] == str(tmp_path / ax.MANIFEST_NAME)
        files = _files(tmp_path)
        assert len(files) == 8
        assert (
            "2026/Daily Closing Report Summary/"
            "Fosse Daily Closing Report Summary 2026.05.01.txt"
        ) in files
        assert "2026/Credit Card Activity/Fosse Credit Card Activity 2026.05.02.txt" in files
        first = tmp_path / files[0]
        assert first.read_bytes().startswith(b"body of ")

    def test_the_result_carries_counts_only(self, tmp_path: Path) -> None:
        out, _, _ = _export(tmp_path, _reports(4))
        assert all(not isinstance(v, (list, dict)) for v in out.values())
        assert "Daily" not in str(out)

    def test_manifest_matches_the_files(self, tmp_path: Path) -> None:
        _export(tmp_path, _reports(8))
        rows = _manifest(tmp_path)
        assert len(rows) == 8
        assert set(rows[0]) == set(ax.MANIFEST_FIELDS)
        for row in rows:
            assert row["status"] == "saved"
            path = tmp_path / row["saved_path"]
            data = path.read_bytes()
            assert int(row["size"]) == len(data)
            assert row["sha256"] == hashlib.sha256(data).hexdigest()
            assert row["account"] == "iCloud"
            assert row["mailbox"] == "_Fosse Export"
            assert row["uidvalidity"] == "7"
            assert row["message_id"].endswith("@denbt1.marriott.com")
            assert row["attachment_name"] == "email.txt"
            assert row["sender"] == SENDER
            assert row["date_received"].startswith("2026-05-0")
        assert sorted(r["saved_path"] for r in rows) == _files(tmp_path)

    def test_never_changes_mail(self, tmp_path: Path) -> None:
        """The strict client has no mutating command at all; reaching for one
        would have raised. This also pins how the mail is read."""
        _, conn, _ = _export(tmp_path, _reports(8))
        assert conn.client.selected
        assert all(readonly for _, readonly in conn.client.selected)
        body_parts = [parts for _, parts in conn.client.fetches if "BODY.PEEK[]" in parts]
        assert body_parts
        assert not any(
            part.startswith("BODY[") or part == "RFC822"
            for _, parts in conn.client.fetches for part in parts
        )

    def test_attachment_name_filter(self, tmp_path: Path) -> None:
        messages = {
            1: _message(
                1, "Credit Card Activity 04May26 from x",
                attachments=[("email.txt", b"text"), ("logo.PNG", b"image")],
            )
        }
        out, _, _ = _export(
            tmp_path, messages, attachment_name_contains=".png",
            naming=ax.build_naming(),
        )
        assert out["saved"] == 1
        assert _files(tmp_path) == ["logo.PNG"]

    def test_message_without_a_matching_attachment_is_recorded(
        self, tmp_path: Path
    ) -> None:
        messages = {1: _message(1, "Credit Card Activity 04May26 from x", attachments=[])}
        out, _, _ = _export(tmp_path, messages)
        assert out["saved"] == 0
        assert out["no_matching_attachment"] == 1
        assert out["outcome"] == "complete"
        assert [r["status"] for r in _manifest(tmp_path)] == ["no_matching_attachment"]

    def test_subject_that_does_not_match_is_not_saved_or_guessed(
        self, tmp_path: Path
    ) -> None:
        messages = _reports(4)
        messages[5] = _message(5, "Welcome to the portal")
        out, _, _ = _export(tmp_path, messages)
        assert out["saved"] == 4
        assert out["pattern_no_match"] == 1
        assert len(_files(tmp_path)) == 4
        statuses = [r["status"] for r in _manifest(tmp_path)]
        assert statuses.count("pattern_no_match") == 1

    def test_several_attachments_on_one_message(self, tmp_path: Path) -> None:
        messages = {
            1: _message(
                1, "Credit Card Activity 04May26 from x",
                attachments=[("a.txt", b"one"), ("b.txt", b"two")],
            )
        }
        out, _, _ = _export(
            tmp_path, messages,
            naming=_naming(filename_template="{report} {subject_date:%Y.%m.%d} {index} {name}",
                           subfolder_template=None),
        )
        assert out["saved"] == 2
        assert _files(tmp_path) == [
            "Credit Card Activity 2026.05.04 0 a.txt",
            "Credit Card Activity 2026.05.04 1 b.txt",
        ]

    def test_oversized_attachment_is_recorded_not_written(self, tmp_path: Path) -> None:
        messages = {
            1: _message(1, "Credit Card Activity 04May26 from x",
                        attachments=[("big.txt", b"x" * 5000)])
        }
        conn = FakeConnector(StrictClient(messages))
        store = ax.ExportStore()
        preview = _preview(conn, store, tmp_path)
        out = _commit(conn, store, preview["preview_id"], max_attachment_bytes=1000)
        assert out["saved"] == 0
        assert out["too_large"] == 1
        assert _files(tmp_path) == []
        assert [r["status"] for r in _manifest(tmp_path)] == ["too_large"]

    def test_a_message_gone_since_the_preview_is_recorded_and_dropped(
        self, tmp_path: Path
    ) -> None:
        messages = _reports(4)
        conn = FakeConnector(StrictClient(messages))
        store = ax.ExportStore()
        preview = _preview(conn, store, tmp_path)
        del messages[2]
        out = _commit(conn, store, preview["preview_id"])
        assert out["saved"] == 3
        assert out["missing"] == 1
        assert out["remaining"] == 0
        assert out["outcome"] == "complete"

    def test_max_count_below_the_set_refuses(self, tmp_path: Path) -> None:
        conn = FakeConnector(StrictClient(_reports(8)))
        store = ax.ExportStore()
        preview = _preview(conn, store, tmp_path)
        with pytest.raises(qm.PreviewTooLargeError):
            _commit(conn, store, preview["preview_id"], max_count=7)
        assert _files(tmp_path) == []

    def test_unknown_preview_id(self, tmp_path: Path) -> None:
        with pytest.raises(qm.PreviewNotFoundError):
            _commit(FakeConnector(StrictClient({})), ax.ExportStore(), "nope")

    def test_rebuilt_mailbox_refuses(self, tmp_path: Path) -> None:
        conn = FakeConnector(StrictClient(_reports(4)))
        store = ax.ExportStore()
        preview = _preview(conn, store, tmp_path)
        conn.client.uidvalidity = 8
        with pytest.raises(qm.SelectionStaleError):
            _commit(conn, store, preview["preview_id"])
        assert _files(tmp_path) == []

    def test_a_different_server_refuses(self, tmp_path: Path) -> None:
        conn = FakeConnector(StrictClient(_reports(4)))
        store = ax.ExportStore()
        preview = _preview(conn, store, tmp_path)
        conn._host = "other.example.com"
        with pytest.raises(qm.SelectionStaleError):
            _commit(conn, store, preview["preview_id"])

    def test_time_budget_stops_with_more_and_resumes(self, tmp_path: Path) -> None:
        conn = FakeConnector(StrictClient(_reports(8)))
        store = ax.ExportStore()
        preview = _preview(conn, store, tmp_path)
        entry = store.get(preview["preview_id"])
        entry.batch_size = 2
        ticks = iter([0.0, 1.0, 2.0, 25.0, 26.0, 27.0, 28.0, 29.0])
        first = _commit(
            conn, store, preview["preview_id"], time_budget=20.0,
            clock=lambda: next(ticks),
        )
        assert first["outcome"] == "more"
        assert 0 < first["messages_done"] < 8
        assert first["remaining"] == 8 - first["messages_done"]
        second = _commit(conn, store, preview["preview_id"])
        assert second["outcome"] == "complete"
        assert first["saved"] + second["saved"] == 8
        assert len(_files(tmp_path)) == 8
        assert len(_manifest(tmp_path)) == 8

    def test_completed_preview_is_discarded(self, tmp_path: Path) -> None:
        out, conn, store = _export(tmp_path, _reports(4))
        assert out["outcome"] == "complete"
        assert not store._entries

    def test_a_timeout_keeps_the_batch_for_the_next_call(self, tmp_path: Path) -> None:
        conn = FakeConnector(StrictClient(_reports(8)))
        store = ax.ExportStore()
        preview = _preview(conn, store, tmp_path)
        store.get(preview["preview_id"]).batch_size = 4
        conn.client.fail_fetch_on = 2
        first = _commit(conn, store, preview["preview_id"])
        assert first["outcome"] == "failed"
        assert first["server_slow"] is True
        assert first["saved"] == 4
        assert first["remaining"] == 4
        assert "timed out" in first["error"]
        # It left the session as an exception, so the connection is dropped.
        assert conn.left_with[-1] is not None
        conn.client.fail_fetch_on = None
        second = _commit(conn, store, preview["preview_id"])
        assert second["outcome"] == "complete"
        assert second["saved"] == 4
        assert len(_files(tmp_path)) == 8
        assert len(_manifest(tmp_path)) == 8

    def test_two_commits_on_one_preview_do_not_run_together(
        self, tmp_path: Path
    ) -> None:
        conn = FakeConnector(StrictClient(_reports(4)))
        store = ax.ExportStore()
        preview = _preview(conn, store, tmp_path)
        entry = store.get(preview["preview_id"])
        assert entry.lock.acquire(blocking=False)
        try:
            with pytest.raises(qm.CommitInProgressError):
                _commit(conn, store, preview["preview_id"])
        finally:
            entry.lock.release()

    def test_a_folder_that_is_a_symlink_out_of_the_directory_is_refused(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "exports"
        target.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        os.symlink(outside, target / "2026")
        out, _, _ = _export(target, _reports(4))
        assert out["saved"] == 0
        assert out["errors"] == 4
        assert list(outside.iterdir()) == []
        assert all(r["status"] == "error" for r in _manifest(target))

    def test_a_write_failure_is_recorded_and_the_run_goes_on(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_link = os.link
        calls = {"n": 0}

        def flaky_link(src: Any, dst: Any, **kwargs: Any) -> None:
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError(28, "No space left on device")
            real_link(src, dst, **kwargs)

        monkeypatch.setattr(ax.os, "link", flaky_link)
        out, _, _ = _export(tmp_path, _reports(4))
        assert out["saved"] == 3
        assert out["errors"] == 1
        assert out["outcome"] == "complete"
        # No temp file is left behind.
        assert not list(tmp_path.rglob(".export-*"))


# ------------------------------------------------------------ existing files


class TestExistingFiles:
    def _resent(self) -> dict[int, dict[str, Any]]:
        """UID 2 is the same report as UID 1 sent again with different text."""
        subject = "Credit Card Activity 04May26 from denbt1.marriott.com"
        return {
            1: _message(1, subject, attachments=[("email.txt", b"first")]),
            2: _message(2, subject, attachments=[("email.txt", b"second, corrected")]),
        }

    def test_skip_reports_a_name_collision_instead_of_dropping_it_quietly(
        self, tmp_path: Path
    ) -> None:
        out, _, _ = _export(tmp_path, self._resent())
        assert out["saved"] == 1
        assert out["name_collisions"] == 1
        files = _files(tmp_path)
        assert len(files) == 1
        assert (tmp_path / files[0]).read_bytes() == b"first"
        assert [r["status"] for r in _manifest(tmp_path)] == ["saved", "name_collision"]

    def test_suffix_keeps_both(self, tmp_path: Path) -> None:
        out, _, _ = _export(tmp_path, self._resent(), if_exists="suffix")
        assert out["saved"] == 1
        assert out["renamed"] == 1
        assert out["name_collisions"] == 0
        assert _files(tmp_path) == [
            "2026/Credit Card Activity/Fosse Credit Card Activity 2026.05.04 (2).txt",
            "2026/Credit Card Activity/Fosse Credit Card Activity 2026.05.04.txt",
        ]

    def test_overwrite_replaces(self, tmp_path: Path) -> None:
        out, _, _ = _export(tmp_path, self._resent(), if_exists="overwrite")
        assert out["saved"] == 1
        assert out["overwritten"] == 1
        files = _files(tmp_path)
        assert len(files) == 1
        assert (tmp_path / files[0]).read_bytes() == b"second, corrected"

    def test_the_same_content_already_there_is_not_a_collision(
        self, tmp_path: Path
    ) -> None:
        """Covers a crash between writing a file and logging it: the next
        run finds the file, sees it is the same, and records it."""
        _export(tmp_path, _reports(4))
        (tmp_path / ax.MANIFEST_NAME).unlink()
        out, _, _ = _export(tmp_path, _reports(4))
        assert out["saved"] == 0
        assert out["identical_existing"] == 4
        assert out["name_collisions"] == 0
        assert len(_files(tmp_path)) == 4
        assert [r["status"] for r in _manifest(tmp_path)] == ["identical_existing"] * 4

    def test_suffix_rerun_does_not_multiply_files(self, tmp_path: Path) -> None:
        _export(tmp_path, self._resent(), if_exists="suffix")
        (tmp_path / ax.MANIFEST_NAME).unlink()
        out, _, _ = _export(tmp_path, self._resent(), if_exists="suffix")
        assert out["saved"] == 0 and out["renamed"] == 0
        assert out["identical_existing"] == 2
        assert len(_files(tmp_path)) == 2

    def test_a_directory_where_the_file_should_go_is_an_error(
        self, tmp_path: Path
    ) -> None:
        clash = tmp_path / "2026" / "Credit Card Activity"
        clash.mkdir(parents=True)
        (clash / "Fosse Credit Card Activity 2026.05.04.txt").mkdir()
        messages = {1: self._resent()[1]}
        for mode in ("skip", "suffix", "overwrite"):
            out, _, _ = _export(tmp_path, messages, if_exists=mode)
            assert out["errors"] == 1, mode
            assert out["saved"] == 0


# ------------------------------------------------------------------ resume


class TestResume:
    def test_a_rerun_leaves_out_what_the_manifest_and_files_show_as_done(
        self, tmp_path: Path
    ) -> None:
        messages = _reports(8)
        _export(tmp_path, messages)
        conn = FakeConnector(StrictClient(messages))
        again = _preview(conn, ax.ExportStore(), tmp_path)
        assert again["matched"] == 8
        assert again["already_exported"] == 8
        assert again["to_export"] == 0
        assert again["preview_id"] is None

    def test_only_what_is_left_is_downloaded(self, tmp_path: Path) -> None:
        messages = _reports(8)
        _export(tmp_path, {u: messages[u] for u in range(1, 5)})
        conn = FakeConnector(StrictClient(messages))
        store = ax.ExportStore()
        preview = _preview(conn, store, tmp_path)
        assert preview["already_exported"] == 4
        assert preview["to_export"] == 4
        out = _commit(conn, store, preview["preview_id"])
        assert out["saved"] == 4
        fetched = sorted(
            u for uids, parts in conn.client.fetches if "BODY.PEEK[]" in parts for u in uids
        )
        assert fetched == [5, 6, 7, 8]
        assert len(_files(tmp_path)) == 8

    def test_a_missing_file_is_exported_again(self, tmp_path: Path) -> None:
        messages = _reports(4)
        _export(tmp_path, messages)
        victim = tmp_path / _files(tmp_path)[0]
        victim.unlink()
        conn = FakeConnector(StrictClient(messages))
        store = ax.ExportStore()
        preview = _preview(conn, store, tmp_path)
        assert preview["already_exported"] == 3
        out = _commit(conn, store, preview["preview_id"])
        assert out["saved"] == 1
        assert victim.exists()

    def test_a_file_changed_in_size_is_not_trusted(self, tmp_path: Path) -> None:
        messages = _reports(4)
        _export(tmp_path, messages)
        (tmp_path / _files(tmp_path)[0]).write_bytes(b"edited by hand, longer")
        preview = _preview(
            FakeConnector(StrictClient(messages)), ax.ExportStore(), tmp_path
        )
        assert preview["already_exported"] == 3

    def test_resume_survives_the_mail_moving_to_another_mailbox(
        self, tmp_path: Path
    ) -> None:
        messages = _reports(4)
        _export(tmp_path, messages)
        moved = {uid + 100: row for uid, row in messages.items()}
        conn = FakeConnector(StrictClient(moved, uidvalidity=99))
        preview = _preview(conn, ax.ExportStore(), tmp_path, mailbox="Elsewhere")
        assert preview["already_exported"] == 4

    def test_other_modes_do_not_skip_by_manifest(self, tmp_path: Path) -> None:
        messages = _reports(4)
        _export(tmp_path, messages)
        preview = _preview(
            FakeConnector(StrictClient(messages)), ax.ExportStore(), tmp_path,
            if_exists="overwrite",
        )
        assert preview["already_exported"] == 0
        assert preview["to_export"] == 4

    def test_an_unreadable_manifest_is_reported_not_trusted(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / ax.MANIFEST_NAME).write_bytes(b"\xff\xfe not a csv \x00")
        preview = _preview(
            FakeConnector(StrictClient(_reports(4))), ax.ExportStore(), tmp_path
        )
        assert preview["already_exported"] == 0
        assert preview["manifest_unreadable"] is True

    def test_a_manifest_path_pointing_outside_is_not_followed(
        self, tmp_path: Path
    ) -> None:
        """A manifest is a file in a folder the user may have had shared with
        them; a row naming ``../../something`` must not be believed."""
        messages = _reports(1)
        target = tmp_path / "exports"
        target.mkdir()
        (tmp_path / "secret.txt").write_bytes(b"12345")
        with (target / ax.MANIFEST_NAME).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=ax.MANIFEST_FIELDS)
            writer.writeheader()
            writer.writerow({
                **dict.fromkeys(ax.MANIFEST_FIELDS, ""),
                "message_id": messages[1]["message_id"].strip("<>"),
                "saved_path": "../secret.txt", "size": "5", "status": "saved",
            })
        preview = _preview(FakeConnector(StrictClient(messages)), ax.ExportStore(), target)
        assert preview["already_exported"] == 0


# ---------------------------------------------------------------- manifest


class TestManifestCells:
    @pytest.mark.parametrize("lead", ["=", "+", "-", "@", "\t", "\r"])
    def test_every_formula_lead_is_made_inert(self, lead: str) -> None:
        assert ax._csv_cell(f"{lead}cmd|' /C calc'!A0") == f"'{lead}cmd|' /C calc'!A0"
        assert ax._csv_cell(None) == ""
        assert ax._csv_cell(42) == "42"

    @pytest.mark.parametrize("lead", ["=", "+", "-", "@"])
    def test_a_subject_cannot_become_a_formula(self, tmp_path: Path, lead: str) -> None:
        subject = f"{lead}HYPERLINK(\"http://x\") 04May26 from x"
        messages = {1: _message(1, subject)}
        _export(
            tmp_path, messages,
            naming=ax.build_naming(filename_template="{uid} {name}"),
        )
        with (tmp_path / ax.MANIFEST_NAME).open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        assert rows[0]["subject"].startswith("'")

    def test_ordinary_cells_are_left_alone(self, tmp_path: Path) -> None:
        _export(tmp_path, _reports(1))
        row = _manifest(tmp_path)[0]
        assert row["subject"].startswith("Daily Closing Report Summary")
        assert not row["saved_path"].startswith("'")


class TestStore:
    def test_expires(self) -> None:
        now = [0.0]
        store = ax.ExportStore(ttl=10.0, clock=lambda: now[0])
        entry_id = store.put(_entry())
        store.get(entry_id)
        now[0] = 11.0
        with pytest.raises(qm.PreviewNotFoundError):
            store.get(entry_id)

    def test_touch_extends_the_lifetime(self) -> None:
        now = [0.0]
        store = ax.ExportStore(ttl=10.0, clock=lambda: now[0])
        entry_id = store.put(_entry())
        now[0] = 8.0
        store.touch(entry_id)
        now[0] = 15.0
        store.get(entry_id)


def _entry() -> ax.ExportEntry:
    return ax.ExportEntry(
        account="iCloud", mailbox="X", uidvalidity=7, identity=("h", "l"),
        total=1, remaining=[1], sizes={1: 10}, save_directory=Path("/tmp"),
        naming=ax.build_naming(), attachment_name_contains=None, if_exists="skip",
    )
