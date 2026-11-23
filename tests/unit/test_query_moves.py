"""Unit tests for the query-driven bulk move (fork-local, ``query_moves``).

The IMAP client is a MagicMock handed out by a fake connector, so no test
touches the network. The properties under test are the safety ones: a
selector can never mean "the whole mailbox", a preview never mutates, and a
commit moves exactly the previewed UIDs and nothing that arrived later.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from imapclient.exceptions import IMAPClientError
from imapclient.response_types import Address, Envelope

from apple_mail_fast_mcp import query_moves as qm
from apple_mail_fast_mcp.exceptions import (
    MailImapMoveUnsupportedError,
    MailMailboxNotFoundError,
)


def _envelope(sender: str, when: datetime | None) -> Envelope:
    mailbox, host = sender.split("@")
    addr = Address(None, None, mailbox.encode(), host.encode())
    return Envelope(when, b"subj", (addr,), (addr,), (addr,), None, None, None, None, b"<id@x>")


UIDPLUS_ONLY = (b"IMAP4REV1", b"UIDPLUS")


class FakeConnector:
    """Stands in for ImapConnector: hands the same mock client to each session."""

    def __init__(self, client: MagicMock) -> None:
        self.client = client
        self.sessions = 0

    @contextmanager
    def _session(self) -> Iterator[MagicMock]:
        self.sessions += 1
        yield self.client

    @staticmethod
    def _has_capability(client: MagicMock, name: bytes) -> bool:
        return name in client.capabilities()


def _client(
    uids: list[int],
    *,
    senders: dict[int, str] | None = None,
    dates: dict[int, datetime] | None = None,
    capabilities: tuple[bytes, ...] = (b"IMAP4REV1", b"MOVE", b"UIDPLUS"),
    uidvalidity: int = 7,
) -> MagicMock:
    client = MagicMock()
    client.capabilities.return_value = capabilities
    client.select_folder.return_value = {b"UIDVALIDITY": uidvalidity}
    client.folder_exists.return_value = True
    present = set(uids)
    client._matches = list(uids)

    def search(criteria: list[Any], charset: str | None = None) -> list[int]:
        if criteria and criteria[0] == "UID":
            asked = {int(u) for u in str(criteria[1]).split(",")}
            return sorted(asked & present)
        return list(client._matches)

    client.search.side_effect = search

    def fetch(chunk: list[int], parts: list[str]) -> dict[int, dict[bytes, Any]]:
        out: dict[int, dict[bytes, Any]] = {}
        for uid in chunk:
            if uid not in present:
                continue
            if "ENVELOPE" in parts:
                sender = (senders or {}).get(uid, "news@example.com")
                when = (dates or {}).get(uid, datetime(2026, 3, 1, 9, 0))
                out[uid] = {b"ENVELOPE": _envelope(sender, when)}
        return out

    def move(chunk: list[int], dest: str) -> None:
        for uid in chunk:
            present.discard(uid)

    def uid_expunge(chunk: list[int]) -> None:
        for uid in chunk:
            present.discard(uid)

    client.fetch.side_effect = fetch
    client.move.side_effect = move
    client.uid_expunge.side_effect = uid_expunge
    client._present = present
    return client


def _preview(conn: FakeConnector, store: qm.PreviewStore, **overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "account": "iCloud",
        "source_mailbox": "News",
        "destination_mailbox": "_To Delete",
        "selector": qm.Selector(sender_contains="news@example.com", date_to="2026-05-16"),
        "ceiling": 5000,
    }
    args.update(overrides)
    return qm.preview(conn, store, **args)


# --------------------------------------------------------------- criteria


class TestBuildCriteria:
    def test_positive_filters_and_exclusions(self) -> None:
        sel = qm.Selector(
            sender_contains="a@x.com",
            sender_not_contains=("gmail.com", "me.com"),
            date_to="2026-05-16",
        )
        assert qm.build_criteria(sel) == [
            "FROM", "a@x.com", "BEFORE", "17-May-2026",
            "NOT", "FROM", "gmail.com", "NOT", "FROM", "me.com",
        ]

    def test_empty_selector_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            qm.build_criteria(qm.Selector())

    @pytest.mark.parametrize("field", ["sender_contains", "subject_contains"])
    @pytest.mark.parametrize("blank", ["", "   "])
    def test_blank_string_rejected_not_ignored(self, field: str, blank: str) -> None:
        """A blank filter must not silently widen the selection to ALL."""
        with pytest.raises(ValueError, match=field):
            qm.build_criteria(qm.Selector(**{field: blank, "date_to": "2026-05-16"}))

    def test_blank_exclusion_rejected(self) -> None:
        with pytest.raises(ValueError, match="sender_not_contains"):
            qm.build_criteria(
                qm.Selector(date_to="2026-05-16", sender_not_contains=("ok.com", " "))
            )

    def test_exclusions_alone_rejected(self) -> None:
        """NOT-only criteria select nearly everything; require a positive filter."""
        with pytest.raises(ValueError, match="at least one"):
            qm.build_criteria(qm.Selector(sender_not_contains=("gmail.com",)))

    def test_control_characters_rejected(self) -> None:
        with pytest.raises(ValueError, match="control character"):
            qm.build_criteria(
                qm.Selector(date_to="2026-05-16", sender_not_contains=("a\r\nb",))
            )

    def test_bad_date_rejected(self) -> None:
        with pytest.raises(ValueError, match="date_to"):
            qm.build_criteria(qm.Selector(date_to="05/16/2026"))


# ---------------------------------------------------------------- preview


class TestPreview:
    def test_reports_set_and_issues_id_without_mutating(self) -> None:
        client = _client(
            [1, 2, 3],
            senders={1: "news@example.com", 2: "news@example.com", 3: "other@example.com"},
            dates={
                1: datetime(2025, 1, 5),
                2: datetime(2026, 5, 16, 23, 0),
                3: datetime(2026, 6, 1),
            },
        )
        store = qm.PreviewStore()
        out = _preview(FakeConnector(client), store)

        assert out["matched"] == 3
        assert out["preview_id"]
        assert out["oldest_date"] == "2025-01-05"
        assert out["newest_date"] == "2026-06-01"
        assert out["header_dates_outside_window"] == 1
        assert out["distinct_senders"] == 2
        assert out["senders"][0] == {"address": "news@example.com", "count": 2}
        assert out["other_senders_count"] == 0
        client.select_folder.assert_called_once_with("News", readonly=True)
        client.move.assert_not_called()
        client.copy.assert_not_called()
        client.add_flags.assert_not_called()

    def test_sender_list_capped_and_rest_summed(self) -> None:
        uids = list(range(1, 61))
        senders = {u: f"s{u:02d}@example.com" for u in uids}
        out = _preview(FakeConnector(_client(uids, senders=senders)), qm.PreviewStore())
        assert out["distinct_senders"] == 60
        assert len(out["senders"]) == qm.SENDER_LIMIT
        assert out["other_senders_count"] == 60 - qm.SENDER_LIMIT

    def test_over_ceiling_gives_counts_but_no_id(self) -> None:
        client = _client(list(range(1, 12)))
        out = _preview(FakeConnector(client), qm.PreviewStore(), ceiling=10)
        assert out["matched"] == 11
        assert out["preview_id"] is None
        assert out["over_limit"] is True
        client.fetch.assert_not_called()

    def test_no_matches_gives_no_id(self) -> None:
        out = _preview(FakeConnector(_client([])), qm.PreviewStore())
        assert out["matched"] == 0
        assert out["preview_id"] is None

    def test_same_source_and_destination_rejected(self) -> None:
        with pytest.raises(ValueError, match="differ"):
            _preview(
                FakeConnector(_client([1])), qm.PreviewStore(),
                destination_mailbox="news",
            )

    def test_gmail_refused(self) -> None:
        client = _client([1], capabilities=(b"MOVE", b"X-GM-EXT-1"))
        with pytest.raises(qm.QueryMoveUnsupportedError, match="Gmail"):
            _preview(FakeConnector(client), qm.PreviewStore())
        client.search.assert_not_called()

    def test_server_without_move_or_uidplus_refused(self) -> None:
        client = _client([1], capabilities=(b"IMAP4REV1",))
        with pytest.raises(MailImapMoveUnsupportedError):
            _preview(FakeConnector(client), qm.PreviewStore())

    def test_uidplus_only_server_can_preview(self) -> None:
        """iCloud advertises UIDPLUS but not MOVE (observed 2026-11-23)."""
        client = _client([1, 2], capabilities=UIDPLUS_ONLY)
        out = _preview(FakeConnector(client), qm.PreviewStore())
        assert out["matched"] == 2
        assert out["preview_id"]

    def test_missing_destination_refused(self) -> None:
        client = _client([1])
        client.folder_exists.return_value = False
        with pytest.raises(MailMailboxNotFoundError):
            _preview(FakeConnector(client), qm.PreviewStore())

    def test_missing_uidvalidity_refused(self) -> None:
        client = _client([1])
        client.select_folder.return_value = {}
        with pytest.raises(qm.QueryMoveUnsupportedError, match="UIDVALIDITY"):
            _preview(FakeConnector(client), qm.PreviewStore())

    def test_criteria_survive_imapclient_normalisation(self) -> None:
        """Run the real imapclient normaliser: exclusions must stay NOT FROM
        pairs and the presence check must be a bare sequence set."""
        from imapclient.imapclient import _normalise_search_criteria

        sel = qm.Selector(sender_contains="a@x.com", sender_not_contains=("my co",))
        assert _normalise_search_criteria(qm.build_criteria(sel)) == [
            b"FROM", b"a@x.com", b"NOT", b"FROM", b'"my co"',
        ]
        assert _normalise_search_criteria(["UID", "1,2,3"]) == [b"UID", b"1,2,3"]

    def test_mailbox_control_characters_rejected(self) -> None:
        with pytest.raises(ValueError, match="control character"):
            _preview(
                FakeConnector(_client([1])), qm.PreviewStore(),
                source_mailbox="News\r\nX",
            )


# ----------------------------------------------------------------- commit


class TestCommit:
    def _ready(
        self, uids: list[int], **client_kw: Any
    ) -> tuple[FakeConnector, qm.PreviewStore, str, MagicMock]:
        client = _client(uids, **client_kw)
        conn = FakeConnector(client)
        store = qm.PreviewStore()
        pid = _preview(conn, store)["preview_id"]
        client.select_folder.reset_mock()
        return conn, store, pid, client

    def test_moves_exactly_the_previewed_uids(self) -> None:
        conn, store, pid, client = self._ready([1, 2, 3])
        # Mail that arrives after the preview also matches the selector.
        client._present.add(99)
        client._matches = [1, 2, 3, 99]

        out = qm.commit(conn, store, preview_id=pid, max_count=10)

        assert out == {
            "moved": 3, "already_gone": 0, "remaining": 0, "total": 3,
            "outcome": "complete",
        }
        client.select_folder.assert_called_once_with("News", readonly=False)
        client.move.assert_called_once_with([1, 2, 3], "_To Delete")
        assert 99 in client._present

    def test_preview_is_single_use_once_complete(self) -> None:
        conn, store, pid, _ = self._ready([1])
        qm.commit(conn, store, preview_id=pid, max_count=10)
        with pytest.raises(qm.PreviewNotFoundError):
            qm.commit(conn, store, preview_id=pid, max_count=10)

    def test_unknown_preview_id(self) -> None:
        with pytest.raises(qm.PreviewNotFoundError):
            qm.commit(
                FakeConnector(_client([])), qm.PreviewStore(),
                preview_id="nope", max_count=10,
            )

    def test_expired_preview_refused(self) -> None:
        now = [1000.0]
        store = qm.PreviewStore(ttl=900, clock=lambda: now[0])
        conn = FakeConnector(_client([1]))
        pid = _preview(conn, store)["preview_id"]
        now[0] += 901
        with pytest.raises(qm.PreviewNotFoundError):
            qm.commit(conn, store, preview_id=pid, max_count=10)
        conn.client.move.assert_not_called()

    def test_max_count_below_previewed_set_refused(self) -> None:
        conn, store, pid, client = self._ready([1, 2, 3])
        with pytest.raises(qm.PreviewTooLargeError):
            qm.commit(conn, store, preview_id=pid, max_count=2)
        client.move.assert_not_called()
        # The preview survives so the caller can retry with a correct ceiling.
        assert qm.commit(conn, store, preview_id=pid, max_count=3)["moved"] == 3

    def test_uidvalidity_change_refused_and_preview_dropped(self) -> None:
        conn, store, pid, client = self._ready([1, 2])
        client.select_folder.return_value = {b"UIDVALIDITY": 8}
        with pytest.raises(qm.SelectionStaleError):
            qm.commit(conn, store, preview_id=pid, max_count=10)
        client.move.assert_not_called()
        with pytest.raises(qm.PreviewNotFoundError):
            store.get(pid)

    def test_slices_per_call_and_resumes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(qm, "MAX_PER_CALL", 4)
        monkeypatch.setattr(qm, "MOVE_CHUNK", 2)
        conn, store, pid, client = self._ready(list(range(1, 8)))

        first = qm.commit(conn, store, preview_id=pid, max_count=10)
        assert (first["moved"], first["remaining"], first["outcome"]) == (4, 3, "more")
        assert [c.args[0] for c in client.move.call_args_list] == [[1, 2], [3, 4]]

        second = qm.commit(conn, store, preview_id=pid, max_count=10)
        assert (second["moved"], second["remaining"], second["outcome"]) == (3, 0, "complete")

    def test_messages_already_gone_are_not_counted_as_moved(self) -> None:
        conn, store, pid, client = self._ready([1, 2, 3])
        client._present.discard(2)  # the user moved one by hand meanwhile
        out = qm.commit(conn, store, preview_id=pid, max_count=10)
        assert (out["moved"], out["already_gone"], out["outcome"]) == (2, 1, "complete")
        client.move.assert_called_once_with([1, 3], "_To Delete")

    def test_mid_run_error_reports_recounted_remainder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(qm, "MOVE_CHUNK", 2)
        conn, store, pid, client = self._ready([1, 2, 3, 4, 5])
        real_move = client.move.side_effect
        calls = {"n": 0}

        def flaky(chunk: list[int], dest: str) -> None:
            calls["n"] += 1
            if calls["n"] == 2:
                # The server applied the chunk but the reply was lost.
                real_move(chunk, dest)
                raise IMAPClientError("socket timeout")
            real_move(chunk, dest)

        client.move.side_effect = flaky
        sessions_before = conn.sessions

        out = qm.commit(conn, store, preview_id=pid, max_count=10)

        assert out["outcome"] == "failed"
        assert out["moved"] == 2
        assert out["remaining"] == 1
        # The chunk whose reply was lost is reported, not silently dropped.
        assert out["moved_unconfirmed"] == 2
        assert out["moved"] + out["moved_unconfirmed"] + out["remaining"] == out["total"]
        assert "socket timeout" in out["error"]
        assert conn.sessions == sessions_before + 2  # recount used a fresh session

        client.move.side_effect = real_move
        again = qm.commit(conn, store, preview_id=pid, max_count=10)
        assert (again["moved"], again["outcome"]) == (1, "complete")

    def test_recount_failure_is_reported_as_unverified(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(qm, "MOVE_CHUNK", 2)
        conn, store, pid, client = self._ready([1, 2, 3])
        client.move.side_effect = IMAPClientError("down")
        real_search = client.search.side_effect
        seen = {"n": 0}

        def search(criteria: list[Any], charset: str | None = None) -> list[int]:
            seen["n"] += 1
            if seen["n"] > 1:
                raise IMAPClientError("still down")
            return real_search(criteria, charset)

        client.search.side_effect = search
        out = qm.commit(conn, store, preview_id=pid, max_count=10)
        assert out["outcome"] == "failed"
        assert out["remaining"] is None
        assert out["remaining_unverified"] is True
        # Nothing is dropped from the stored set, so a retry is still safe.
        assert store.get(pid).remaining == [1, 2, 3]

    def test_lost_reply_uids_are_recorded_in_the_receipt(self, tmp_path: Path) -> None:
        conn, store, pid, client = self._ready([1, 2])
        real_move = client.move.side_effect

        def applied_then_lost(chunk: list[int], dest: str) -> None:
            real_move(chunk, dest)
            raise IMAPClientError("reply lost")

        client.move.side_effect = applied_then_lost
        receipts = tmp_path / "r.jsonl"
        out = qm.commit(conn, store, preview_id=pid, max_count=10, receipts_path=receipts)
        assert (out["moved"], out["moved_unconfirmed"], out["remaining"]) == (0, 2, 0)
        record = json.loads(receipts.read_text().strip())
        assert record["source_uids"] == []
        assert record["source_uids_unconfirmed"] == [1, 2]

    def test_uidplus_fallback_copies_then_removes_only_those_uids(self) -> None:
        conn, store, pid, client = self._ready([1, 2, 3], capabilities=UIDPLUS_ONLY)
        client._present.add(99)

        out = qm.commit(conn, store, preview_id=pid, max_count=10)

        assert (out["moved"], out["remaining"], out["outcome"]) == (3, 0, "complete")
        client.move.assert_not_called()
        steps = [c[0] for c in client.method_calls if c[0] in ("copy", "add_flags", "uid_expunge")]
        assert steps == ["copy", "add_flags", "uid_expunge"]
        client.copy.assert_called_once_with([1, 2, 3], "_To Delete")
        client.add_flags.assert_called_once_with([1, 2, 3], [b"\\Deleted"], silent=True)
        # Scoped expunge: never a bare EXPUNGE, which would remove every
        # message flagged deleted in the mailbox.
        client.uid_expunge.assert_called_once_with([1, 2, 3])
        client.expunge.assert_not_called()
        assert 99 in client._present

    def test_uidplus_failure_before_removal_loses_nothing(self) -> None:
        conn, store, pid, client = self._ready([1, 2], capabilities=UIDPLUS_ONLY)
        client.add_flags.side_effect = IMAPClientError("dropped")

        out = qm.commit(conn, store, preview_id=pid, max_count=10)

        assert out["outcome"] == "failed"
        assert (out["moved"], out["remaining"]) == (0, 2)
        client.uid_expunge.assert_not_called()
        assert client._present == {1, 2}

    def test_move_support_lost_at_commit_is_a_typed_refusal(self) -> None:
        conn, store, pid, client = self._ready([1])
        client.capabilities.return_value = (b"IMAP4REV1",)
        with pytest.raises(MailImapMoveUnsupportedError):
            qm.commit(conn, store, preview_id=pid, max_count=10)
        client.move.assert_not_called()

    def test_connection_failure_before_any_move(self) -> None:
        conn, store, pid, client = self._ready([1, 2])
        client.select_folder.side_effect = IMAPClientError("no route")
        out = qm.commit(conn, store, preview_id=pid, max_count=10)
        assert (out["outcome"], out["moved"]) == ("failed", 0)
        assert store.get(pid).total == 2

    def test_receipt_written_without_message_content(self, tmp_path: Path) -> None:
        conn, store, pid, _ = self._ready([1, 2])
        receipts = tmp_path / "nested" / "receipts.jsonl"
        qm.commit(conn, store, preview_id=pid, max_count=10, receipts_path=receipts)

        record = json.loads(receipts.read_text().strip())
        assert record["account"] == "iCloud"
        assert record["source_mailbox"] == "News"
        assert record["destination_mailbox"] == "_To Delete"
        assert record["source_uids"] == [1, 2]
        assert record["moved"] == 2
        assert record["selector"]["sender_contains"] == "news@example.com"
        assert "subject" not in json.dumps(record).lower().replace("subject_contains", "")

    def test_receipt_failure_does_not_hide_a_completed_move(self, tmp_path: Path) -> None:
        conn, store, pid, _ = self._ready([1])
        blocker = tmp_path / "file"
        blocker.write_text("x")
        out = qm.commit(
            conn, store, preview_id=pid, max_count=10,
            receipts_path=blocker / "receipts.jsonl",
        )
        assert out["moved"] == 1
        assert out["outcome"] == "complete"
        assert "receipt_error" in out
