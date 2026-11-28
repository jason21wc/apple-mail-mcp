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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from imapclient.exceptions import IMAPClientAbortError, IMAPClientError
from imapclient.response_types import Address, Envelope

from apple_mail_fast_mcp import query_moves as qm
from apple_mail_fast_mcp.exceptions import (
    MailImapMoveUnsupportedError,
    MailMailboxNotFoundError,
)


def _envelope(
    sender: str, when: datetime | None, message_id: bytes = b"<id@x>"
) -> Envelope:
    mailbox, host = sender.split("@")
    addr = Address(None, None, mailbox.encode(), host.encode())
    return Envelope(
        when, b"subj", (addr,), (addr,), (addr,), None, None, None, None, message_id
    )


UIDPLUS_ONLY = (b"IMAP4REV1", b"UIDPLUS")


class FakeConnector:
    """Stands in for ImapConnector: hands the same mock client to each session."""

    def __init__(self, client: MagicMock) -> None:
        self.client = client
        self.sessions = 0
        self._host = "imap.example.com"
        self._email = "me@example.com"
        self._port = 993
        self._account_id = "fixture-account"

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

    client._deleted = []
    client._dest = set()  # UIDs of copies in the destination mailbox
    client._dest_origin = {}  # destination UID -> the source UID it copies
    client._dest_deleted = set()  # copies flagged \\Deleted in the destination
    client._mids = {}  # source UID -> Message-ID, where a test sets one
    client._dest_name = None
    client._next_dest_uid = 1000
    # imaplib files response codes here; imapclient's copy() does not return
    # the tagged reply, so this is where the real code reads COPYUID from.
    client._imap.untagged_responses = {}

    def selected_mailbox() -> Any:
        call = client.select_folder.call_args
        return call.args[0] if call else None

    def in_destination() -> bool:
        return client._dest_name is not None and selected_mailbox() == client._dest_name

    def message_id(uid: int) -> bytes:
        return client._mids.get(uid, b"<m%d@x>" % uid)

    def search(criteria: list[Any], charset: str | None = None) -> list[int]:
        if criteria and criteria[0] == "UID":
            pool = client._dest if in_destination() else present
            if in_destination() and "UNDELETED" in criteria:
                pool = pool - client._dest_deleted
            token = str(criteria[1])
            if ":" in token:
                low, high = (int(part) for part in token.split(":"))
                return sorted(u for u in pool if low <= u <= high)
            return sorted({int(u) for u in token.split(",")} & pool)
        if criteria[-1] == "DELETED":
            return [u for u in client._matches if u in client._deleted and u in present]
        if criteria[-1] == "UNDELETED":
            return [u for u in client._matches if u not in client._deleted]
        return list(client._matches)

    def copy(chunk: list[int], dest: str) -> list[None]:
        client._dest_name = dest
        new = list(range(client._next_dest_uid, client._next_dest_uid + len(chunk)))
        client._next_dest_uid += len(chunk)
        client._dest.update(new)
        client._dest_origin.update(dict(zip(new, chunk, strict=True)))
        src = ",".join(str(u) for u in chunk).encode()
        dst = ",".join(str(u) for u in new).encode()
        client._imap.untagged_responses["COPYUID"] = [
            b"%d %s %s" % (uidvalidity, src, dst)
        ]
        return [None]  # what imapclient really returns: no tagged text

    def add_flags(chunk: list[int], flags: list[bytes], silent: bool = False) -> None:
        client._deleted.extend(u for u in chunk if u not in client._deleted)

    client.copy.side_effect = copy
    client.add_flags.side_effect = add_flags

    client.search.side_effect = search

    def fetch(chunk: list[int], parts: list[str]) -> dict[int, dict[bytes, Any]]:
        out: dict[int, dict[bytes, Any]] = {}
        when = datetime(2026, 3, 1, 9, 0)
        if in_destination():
            for uid in chunk:
                if uid in client._dest:
                    origin = client._dest_origin.get(uid, uid)
                    out[uid] = {
                        b"ENVELOPE": _envelope(
                            "news@example.com", when, message_id(origin)
                        )
                    }
            return out
        for uid in chunk:
            if uid not in present:
                continue
            if "INTERNALDATE" in parts:
                out[uid] = {b"INTERNALDATE": (dates or {}).get(uid, when)}
            if "ENVELOPE" in parts:
                sender = (senders or {}).get(uid, "news@example.com")
                out[uid] = {
                    b"ENVELOPE": _envelope(
                        sender, (dates or {}).get(uid, when), message_id(uid)
                    )
                }
        return out

    def move(chunk: list[int], dest: str) -> None:
        for uid in chunk:
            present.discard(uid)

    def uid_expunge(chunk: list[int]) -> None:
        for uid in chunk:
            present.discard(uid)
            if uid in client._deleted:
                client._deleted.remove(uid)

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
        # Exclusions are applied by the preview, never sent as NOT FROM:
        # iCloud times out on a negated sender search.
        assert qm.build_criteria(sel) == ["FROM", "a@x.com", "BEFORE", "17-May-2026"]

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
        # Only received dates are read for a set this size, never senders.
        client.fetch.assert_called()
        assert all(
            call.args[1] == ["INTERNALDATE"] for call in client.fetch.call_args_list
        )

    def test_over_ceiling_suggests_date_windows_that_each_fit(self) -> None:
        uids = list(range(1, 41))
        dates = {u: datetime(2026, 1, 1 + (u - 1) // 10, 9, 0) for u in uids}
        client = _client(uids, dates=dates)
        store = qm.PreviewStore()
        out = _preview(
            FakeConnector(client), store, ceiling=25,
            selector=qm.Selector(
                sender_contains="news@example.com",
                date_from="2025-12-01", date_to="2026-05-16",
            ),
        )
        assert out["preview_id"] is None
        assert out["limit"] == 25
        windows = out["suggested_windows"]
        assert sum(w["approx_matched"] for w in windows) == 40
        assert all(w["approx_matched"] <= 25 for w in windows)
        # Together they cover exactly the range that was asked for.
        assert windows[0]["date_from"] == "2025-12-01"
        assert windows[-1]["date_to"] == "2026-05-16"
        client.move.assert_not_called()
        client.copy.assert_not_called()
        client.add_flags.assert_not_called()

    def test_over_ceiling_without_readable_dates_says_so(self) -> None:
        client = _client(list(range(1, 12)))
        client.fetch.side_effect = IMAPClientError("no")
        out = _preview(FakeConnector(client), qm.PreviewStore(), ceiling=10)
        assert out["over_limit"] is True
        assert out["preview_id"] is None
        assert out["windows_unavailable"] is True
        assert "suggested_windows" not in out

    @pytest.mark.parametrize(
        "failure", [IMAPClientAbortError("gone"), TimeoutError("timed out")]
    )
    def test_connection_lost_reading_dates_still_answers_and_drops_the_session(
        self, failure: Exception
    ) -> None:
        """The count is still true, so the caller gets it. But the failure
        must leave the session as an exception: that is what makes the pool
        drop the dead connection instead of reusing it."""
        client = _client(list(range(1, 12)))
        client.fetch.side_effect = failure
        left_with: list[BaseException | None] = []

        class Recording(FakeConnector):
            @contextmanager
            def _session(self) -> Iterator[MagicMock]:
                try:
                    yield self.client
                except BaseException as exc:
                    left_with.append(exc)
                    raise
                else:
                    left_with.append(None)

        out = _preview(Recording(client), qm.PreviewStore(), ceiling=10)
        assert out["matched"] == 11
        assert out["over_limit"] is True
        assert out["preview_id"] is None
        assert out["windows_unavailable"] is True
        assert len(left_with) == 1 and left_with[0] is not None

    def test_messages_already_flagged_deleted_are_skipped(self) -> None:
        """A message flagged deleted is usually the leftover of an interrupted
        move: its copy already landed. Selecting it again would copy it twice."""
        client = _client([1, 2, 3])
        client._deleted = [2]
        out = _preview(FakeConnector(client), qm.PreviewStore())
        assert out["matched"] == 2
        assert out["flagged_deleted_skipped"] == 1

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

    def test_copyuid_reply_parsing(self) -> None:
        # The form imaplib stores: no "COPYUID" prefix, no brackets.
        assert qm._parse_copyuid(b"38505 304,319:320 3956:3958", 3) == (
            38505, [3956, 3957, 3958],
        )
        assert qm._parse_copyuid(b"COPYUID 38505 1:2 9,7", 2) == (38505, [9, 7])
        # Anything short of a full account of the copies is "not said".
        assert qm._parse_copyuid(b"38505 1:3 3956:3957", 3) is None
        assert qm._parse_copyuid(b"Done", 1) is None
        assert qm._parse_copyuid(None, 1) is None

    def test_criteria_survive_imapclient_normalisation(self) -> None:
        """Run the real imapclient normaliser: no negated sender term reaches
        the server and the presence check must be a bare sequence set."""
        from imapclient.imapclient import _normalise_search_criteria

        sel = qm.Selector(sender_contains="a@x.com", sender_not_contains=("my co",))
        assert _normalise_search_criteria(qm.build_criteria(sel)) == [
            b"FROM", b"a@x.com",
        ]
        assert _normalise_search_criteria(["UID", "1,2,3"]) == [b"UID", b"1,2,3"]
        # The lost-copy search: a UID range must not be quoted either.
        assert _normalise_search_criteria(["UID", "1000:4294967295", "UNDELETED"]) == [
            b"UID", b"1000:4294967295", b"UNDELETED",
        ]

    def test_mailbox_control_characters_rejected(self) -> None:
        with pytest.raises(ValueError, match="control character"):
            _preview(
                FakeConnector(_client([1])), qm.PreviewStore(),
                source_mailbox="News\r\nX",
            )


# ----------------------------------------------------------------- commit


class TestPreviewExclusions:
    def _run(self, senders: dict[int, str], *terms: str) -> tuple[Any, ...]:
        client = _client(sorted(senders), senders=senders)
        conn, store = FakeConnector(client), qm.PreviewStore()
        out = _preview(
            conn, store,
            selector=qm.Selector(date_to="2026-05-16", sender_not_contains=terms),
        )
        return out, store, client

    def test_excluded_senders_are_left_out_of_the_held_set(self) -> None:
        out, store, client = self._run(
            {1: "news@example.com", 2: "pal@gmail.com", 3: "news@example.com"},
            "@GMAIL.com",
        )
        assert out["matched"] == 2
        assert out["excluded_by_sender"] == 1
        assert out["senders"] == [{"address": "news@example.com", "count": 2}]
        assert store.get(out["preview_id"]).remaining == [1, 3]
        # Nothing negated was sent to the server.
        for call in client.search.call_args_list:
            assert "NOT" not in call.args[0]

    def test_exclusion_matches_the_display_name(self) -> None:
        client = _client([1, 2])
        named = Address(b"Peter Sarjeant", None, b"someone", b"example.org")
        plain = _envelope("news@example.com", datetime(2026, 3, 1))
        from_named = Envelope(
            plain.date, b"subj", (named,), (named,), (named,),
            None, None, None, None, b"<id2@x>",
        )
        client.fetch.side_effect = lambda chunk, parts: {
            1: {b"ENVELOPE": from_named},
            2: {b"ENVELOPE": plain},
        }
        store = qm.PreviewStore()
        out = _preview(
            FakeConnector(client), store,
            selector=qm.Selector(date_to="2026-05-16", sender_not_contains=("sarjeant",)),
        )
        assert store.get(out["preview_id"]).remaining == [2]

    def test_unreadable_sender_is_left_out_when_excluding(self) -> None:
        client = _client([1, 2])
        real_fetch = client.fetch.side_effect
        client.fetch.side_effect = lambda chunk, parts: {
            u: d for u, d in real_fetch(chunk, parts).items() if u != 2
        }
        store = qm.PreviewStore()
        out = _preview(
            FakeConnector(client), store,
            selector=qm.Selector(date_to="2026-05-16", sender_not_contains=("gmail",)),
        )
        assert out["sender_unreadable_left_out"] == 1
        assert store.get(out["preview_id"]).remaining == [1]

    def test_everything_excluded_issues_no_preview_id(self) -> None:
        out, _, _ = self._run({1: "a@gmail.com", 2: "b@gmail.com"}, "gmail.com")
        assert out["matched"] == 0
        assert out["excluded_by_sender"] == 2
        assert out["preview_id"] is None

    def test_no_exclusions_reports_no_exclusion_counts(self) -> None:
        out = _preview(FakeConnector(_client([1, 2])), qm.PreviewStore())
        assert "excluded_by_sender" not in out


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
            "outcome": "complete", "slowest_chunk_seconds": 0.0,
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
        client.add_flags.side_effect = IMAPClientAbortError("dropped")

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

    def test_call_stops_when_time_budget_is_spent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A call that outlives the client's timeout gets the server killed
        mid-move (observed live 2026-11-23). Bound it by elapsed time."""
        monkeypatch.setattr(qm, "MOVE_CHUNK", 2)
        conn, store, pid, client = self._ready([1, 2, 3, 4, 5, 6])
        now = [0.0]
        real_move = client.move.side_effect

        def slow_move(chunk: list[int], dest: str) -> None:
            now[0] += 12.0  # two chunks fit in the budget, a third does not
            real_move(chunk, dest)

        client.move.side_effect = slow_move
        out = qm.commit(
            conn, store, preview_id=pid, max_count=10,
            time_budget=20.0, clock=lambda: now[0],
        )

        assert (out["moved"], out["remaining"], out["outcome"]) == (4, 2, "more")
        again = qm.commit(conn, store, preview_id=pid, max_count=10)
        assert (again["moved"], again["outcome"]) == (2, "complete")

    # ---- a slowed server (observed live 2026-11-23, late) ----------------

    def _slow_ready(
        self, uids: list[int], **client_kw: Any
    ) -> tuple[FakeConnector, qm.PreviewStore, str, MagicMock, Any]:
        """A UIDPLUS server that reports where its destination UIDs stand,
        as a real one does. Returns the working copy() as well."""
        conn, store, pid, client = self._ready(
            uids, capabilities=UIDPLUS_ONLY, **client_kw
        )
        client._dest_name = "_To Delete"
        client.folder_status.side_effect = lambda name, what: {
            b"UIDVALIDITY": 7, b"UIDNEXT": client._next_dest_uid,
        }
        return conn, store, pid, client, client.copy.side_effect

    @staticmethod
    def _after_grace(monkeypatch: pytest.MonkeyPatch) -> None:
        later = qm._utcnow() + timedelta(seconds=qm.COPY_GRACE_SECONDS + 5)
        monkeypatch.setattr(qm, "_utcnow", lambda: later)

    @staticmethod
    def _lose_reply(client: MagicMock, real_copy: Any, *, landed: bool) -> None:
        def copy(chunk: list[int], dest: str) -> Any:
            if landed:
                real_copy(chunk, dest)
            raise TimeoutError("timed out")

        client.copy.side_effect = copy

    def test_lost_copy_reply_is_found_and_the_move_finished(
        self, tmp_path: Path
    ) -> None:
        journal = tmp_path / "journal.json"
        conn, store, pid, client, real_copy = self._slow_ready([1, 2, 3])
        self._lose_reply(client, real_copy, landed=True)

        first = qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        assert (first["outcome"], first["moved"]) == ("failed", 0)
        assert first["server_slow"] is True
        assert first["retry_after_seconds"] == int(qm.COPY_GRACE_SECONDS)
        assert json.loads(journal.read_text())[0]["dest_uidnext"] == 1000
        assert client._present == {1, 2, 3}

        client.copy.side_effect = real_copy
        client.copy.reset_mock()
        second = qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)

        client.copy.assert_not_called()  # found, so not copied a second time
        assert second["resumed"] == 3
        assert second["outcome"] == "complete"
        assert "held_back_uncertain" not in second
        assert client._present == set()
        assert json.loads(journal.read_text()) == []

    def test_lost_copy_that_never_landed_waits_then_is_moved_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        journal = tmp_path / "journal.json"
        conn, store, pid, client, real_copy = self._slow_ready([1, 2, 3])
        self._lose_reply(client, real_copy, landed=False)
        qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)

        client.copy.side_effect = real_copy
        client.copy.reset_mock()
        soon = qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        # Inside the grace period the copy may still land: nothing is started.
        client.copy.assert_not_called()
        assert (soon["moved"], soon["outcome"], soon["remaining"]) == (0, "more", 3)
        assert soon["awaiting_copy_check"] == 3
        assert soon["server_slow"] is True

        self._after_grace(monkeypatch)
        done = qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)

        client.copy.assert_called_once()
        assert (done["moved"], done["outcome"]) == (3, "complete")
        assert "server_slow" not in done
        assert client._present == set()
        assert json.loads(journal.read_text()) == []

    def test_lost_copy_found_only_in_part_keeps_every_original(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        journal = tmp_path / "journal.json"
        conn, store, pid, client, real_copy = self._slow_ready([1, 2, 3])
        self._lose_reply(client, real_copy, landed=True)
        qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        client._dest_deleted.add(1001)  # one copy flagged deleted: not proof

        client.copy.side_effect = real_copy
        client.copy.reset_mock()
        soon = qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        assert soon["awaiting_copy_check"] == 3

        self._after_grace(monkeypatch)
        out = qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)

        assert out["held_back_uncertain"] == 3
        assert client._present == {1, 2, 3}
        client.copy.assert_not_called()
        client.uid_expunge.assert_not_called()
        # Its window is closed now, so nothing copied later can fall in it.
        assert json.loads(journal.read_text())[0]["dest_uidceil"] == 1003

    def test_an_earlier_twin_is_not_mistaken_for_the_lost_copy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two messages share a Message-ID (duplicates do). The first moved
        normally; the second's COPY never landed. The first one's copy must
        not be taken as proof for the second."""
        monkeypatch.setattr(qm, "MOVE_CHUNK", 1)
        journal = tmp_path / "journal.json"
        conn, store, pid, client, real_copy = self._slow_ready([1, 2])
        client._mids = {1: b"<same@x>", 2: b"<same@x>"}

        def copy(chunk: list[int], dest: str) -> Any:
            if chunk == [2]:
                raise TimeoutError("timed out")
            return real_copy(chunk, dest)

        client.copy.side_effect = copy
        first = qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        assert (first["moved"], first["outcome"]) == (1, "failed")

        soon = qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        assert "resumed" not in soon
        assert soon["awaiting_copy_check"] == 1
        assert client._present == {2}

        self._after_grace(monkeypatch)
        client.copy.side_effect = real_copy
        client.copy.reset_mock()
        done = qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        client.copy.assert_called_once_with([2], "_To Delete")  # its own copy
        assert (done["moved"], done["outcome"]) == (1, "complete")
        assert "resumed" not in done

    def test_twins_in_one_chunk_each_need_their_own_copy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        journal = tmp_path / "journal.json"
        conn, store, pid, client, real_copy = self._slow_ready([1, 2])
        client._mids = {1: b"<same@x>", 2: b"<same@x>"}
        self._lose_reply(client, real_copy, landed=True)
        qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        client._dest_deleted.add(1000)  # one of the two identical copies

        client.copy.side_effect = real_copy
        self._after_grace(monkeypatch)
        out = qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        assert out["held_back_uncertain"] == 2
        assert client._present == {1, 2}

    def test_original_without_a_message_id_is_held(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        journal = tmp_path / "journal.json"
        conn, store, pid, client, real_copy = self._slow_ready([1, 2])
        client._mids = {2: b""}
        self._lose_reply(client, real_copy, landed=True)
        qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)

        client.copy.side_effect = real_copy
        self._after_grace(monkeypatch)
        out = qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        assert out["held_back_uncertain"] == 2
        assert client._present == {1, 2}

    def test_a_held_chunk_cannot_be_proved_by_copies_made_later(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Review finding: uid 2's copy is missing, so [1, 2] is held. A
        later chunk then copies uid 3, a twin of 2. That copy must not
        complete the held chunk."""
        monkeypatch.setattr(qm, "MOVE_CHUNK", 2)
        journal = tmp_path / "journal.json"
        conn, store, pid, client, real_copy = self._slow_ready([1, 2, 3])
        client._mids = {2: b"<twin@x>", 3: b"<twin@x>"}
        self._lose_reply(client, real_copy, landed=True)
        qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        client._dest_deleted.add(1001)  # the copy of uid 2

        client.copy.side_effect = real_copy
        self._after_grace(monkeypatch)
        second = qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        assert (second["held_back_uncertain"], second["moved"]) == (2, 1)
        assert client._present == {1, 2}  # uid 3 moved, the held pair did not

        # The held pair's window was closed before uid 3 was copied.
        fresh = qm.PreviewStore()
        again = _preview(conn, fresh, journal_path=journal)
        out = qm.commit(
            conn, fresh, preview_id=again["preview_id"], max_count=10,
            journal_path=journal,
        )
        assert "resumed" not in out
        assert client._present == {1, 2}

    def test_another_source_cannot_copy_into_an_open_window(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Review finding: the window belongs to the destination. While an
        INBOX chunk's lost copy is unaccounted for, a move from another
        mailbox into the same destination waits, then closes that window
        before copying."""
        journal = tmp_path / "journal.json"
        conn, store, pid, client, real_copy = self._slow_ready([1, 2])
        self._lose_reply(client, real_copy, landed=False)
        qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        client.copy.side_effect = real_copy
        client.copy.reset_mock()

        other = _preview(conn, store, source_mailbox="Archive", journal_path=journal)
        soon = qm.commit(
            conn, store, preview_id=other["preview_id"], max_count=10,
            journal_path=journal,
        )
        client.copy.assert_not_called()
        assert (soon["moved"], soon["outcome"]) == (0, "more")
        assert soon["awaiting_copy_check"] == 2

        self._after_grace(monkeypatch)
        done = qm.commit(
            conn, store, preview_id=other["preview_id"], max_count=10,
            journal_path=journal,
        )
        assert done["moved"] == 2
        record = json.loads(journal.read_text())[0]
        assert (record["source_mailbox"], record["dest_uidceil"]) == ("News", 1000)

    def test_two_commits_into_one_destination_do_not_overlap(
        self, tmp_path: Path
    ) -> None:
        """Review finding: commits from two sources running at once could
        each land copies inside the other's lost-copy window."""
        journal = tmp_path / "journal.json"
        conn, store, pid, client, _ = self._slow_ready([1, 2])
        other = _preview(conn, store, source_mailbox="Archive", journal_path=journal)
        started: list[dict[str, Any]] = []

        def copy_while_another_commit_tries(chunk: list[int], dest: str) -> Any:
            with pytest.raises(qm.CommitInProgressError):
                qm.commit(
                    conn, store, preview_id=other["preview_id"], max_count=10,
                    journal_path=journal,
                )
            started.append({"chunk": chunk})
            raise IMAPClientError("refused")

        client.copy.side_effect = copy_while_another_commit_tries
        qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        assert started == [{"chunk": [1, 2]}]
        # Released afterwards: the other commit can run now.
        client.copy.side_effect = None
        client.copy.return_value = [None]
        out = qm.commit(
            conn, store, preview_id=other["preview_id"], max_count=10,
            journal_path=journal,
        )
        assert out["outcome"] in {"complete", "more", "failed"}

    def test_destination_rebuilt_since_the_copy_is_held(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        journal = tmp_path / "journal.json"
        conn, store, pid, client, real_copy = self._slow_ready([1, 2])
        self._lose_reply(client, real_copy, landed=True)
        qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)

        def select(name: str, readonly: bool = False) -> dict[bytes, int]:
            return {b"UIDVALIDITY": 8 if name == "_To Delete" else 7}

        client.select_folder.side_effect = select
        client.folder_status.side_effect = lambda name, what: {
            b"UIDVALIDITY": 8, b"UIDNEXT": 1,
        }
        client.copy.side_effect = real_copy
        client.copy.reset_mock()
        self._after_grace(monkeypatch)
        out = qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        assert out["held_back_uncertain"] == 2
        assert client._present == {1, 2}
        client.copy.assert_not_called()

    def test_waiting_is_never_reported_as_complete(
        self, tmp_path: Path
    ) -> None:
        journal = tmp_path / "journal.json"
        conn, store, pid, client, real_copy = self._slow_ready([1, 2])
        self._lose_reply(client, real_copy, landed=False)
        qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        client.copy.side_effect = real_copy
        # A preview issued only to settle: nothing else is left to move.
        store.get(pid).remaining = []
        out = qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        assert (out["outcome"], out["awaiting_copy_check"]) == ("more", 2)
        assert store.get(pid) is not None

    def test_a_timeout_shrinks_the_step_for_the_next_preview_too(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(qm, "MOVE_CHUNK", 20)
        conn, store, pid, client, real_copy = self._slow_ready(list(range(1, 41)))
        self._lose_reply(client, real_copy, landed=False)
        qm.commit(conn, store, preview_id=pid, max_count=99, journal_path=tmp_path / "j")
        assert client.copy.call_args.args[0] == list(range(1, 21))
        assert store.get(pid).chunk_size == 10
        # After a failure the caller previews again: same account, new id.
        again = _preview(conn, store, journal_path=tmp_path / "j")
        assert store.get(again["preview_id"]).chunk_size == 10

    def test_a_slow_step_halves_the_next_and_a_fast_one_grows_it(self) -> None:
        assert qm._next_size(24, qm.SLOW_CHUNK_SECONDS) == 12
        assert qm._next_size(6, 30.0) == qm.MIN_CHUNK
        assert qm._next_size(qm.MIN_CHUNK, 30.0) == qm.MIN_CHUNK
        assert qm._next_size(12, 5.0) == 12
        assert qm._next_size(12, 1.0) == 24
        assert qm._next_size(20, 1.0) == qm.MOVE_CHUNK

    def test_steps_shrink_within_a_call_when_the_server_slows(self) -> None:
        conn, store, pid, client, real_copy = self._slow_ready(list(range(1, 51)))
        now = [0.0]

        def slow_copy(chunk: list[int], dest: str) -> Any:
            now[0] += 9.0
            return real_copy(chunk, dest)

        client.copy.side_effect = slow_copy
        out = qm.commit(
            conn, store, preview_id=pid, max_count=99, clock=lambda: now[0]
        )
        sizes = [len(call.args[0]) for call in client.copy.call_args_list]
        assert sizes == [25, 12, 6]
        assert out["slowest_chunk_seconds"] == 9.0
        assert (out["moved"], out["outcome"]) == (43, "more")

    def test_copying_record_without_a_snapshot_is_still_held(
        self, tmp_path: Path
    ) -> None:
        """Records written before copies could be located (and servers that
        will not report UIDNEXT) keep the old, cautious behaviour."""
        journal = tmp_path / "journal.json"
        conn, store, pid, client = self._ready([1, 2], capabilities=UIDPLUS_ONLY)
        real_copy = client.copy.side_effect
        self._lose_reply(client, real_copy, landed=True)
        qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        assert "dest_uidnext" not in json.loads(journal.read_text())[0]

        client.copy.side_effect = real_copy
        out = qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        assert out["held_back_uncertain"] == 2
        assert client._present == {1, 2}

    def test_at_least_one_chunk_moves_even_with_no_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(qm, "MOVE_CHUNK", 2)
        conn, store, pid, _ = self._ready([1, 2, 3])
        out = qm.commit(conn, store, preview_id=pid, max_count=10, time_budget=0.0)
        assert (out["moved"], out["remaining"], out["outcome"]) == (2, 1, "more")

    def test_interrupted_removal_resumes_without_copying_again(
        self, tmp_path: Path
    ) -> None:
        journal = tmp_path / "journal.json"
        conn, store, pid, client = self._ready([1, 2, 3], capabilities=UIDPLUS_ONLY)
        real_expunge = client.uid_expunge.side_effect
        client.uid_expunge.side_effect = IMAPClientError("dropped")

        first = qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        assert first["outcome"] == "failed"
        assert json.loads(journal.read_text())[0]["stage"] == "copied"
        client.copy.assert_called_once()

        client.uid_expunge.side_effect = real_expunge
        second = qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)

        client.copy.assert_called_once()  # not copied a second time
        assert second["resumed"] == 3
        assert second["outcome"] == "complete"
        assert client._present == set()
        assert json.loads(journal.read_text()) == []

    def test_killed_server_leaves_a_journal_the_next_run_finishes(
        self, tmp_path: Path
    ) -> None:
        """The process dies between copy and removal: no result, no receipt,
        in-memory preview gone. Only the on-disk journal survives."""
        journal = tmp_path / "journal.json"
        conn, store, pid, client = self._ready([1, 2], capabilities=UIDPLUS_ONLY)
        real_expunge = client.uid_expunge.side_effect
        client.uid_expunge.side_effect = KeyboardInterrupt()
        with pytest.raises(KeyboardInterrupt):
            qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)

        client.uid_expunge.side_effect = real_expunge
        fresh = qm.PreviewStore()
        again = _preview(conn, fresh, journal_path=journal)
        # Flagged originals no longer match, yet an id is still issued so the
        # commit that settles them can run.
        assert again["matched"] == 0
        assert again["flagged_deleted_skipped"] == 2
        assert again["interrupted_pending"] == 2
        assert again["preview_id"]

        out = qm.commit(
            conn, fresh, preview_id=again["preview_id"], max_count=10,
            journal_path=journal, receipts_path=tmp_path / "r.jsonl",
        )

        client.copy.assert_called_once()
        assert out["resumed"] == 2
        assert (out["moved"], out["outcome"]) == (0, "complete")
        assert client._present == set()
        record = json.loads((tmp_path / "r.jsonl").read_text().strip())
        assert record["source_uids_resumed"] == [1, 2]

    def test_originals_are_kept_when_their_copies_have_gone(
        self, tmp_path: Path
    ) -> None:
        """The data-loss case: killed after the copy, the user then deletes
        the copies as apparent duplicates (or empties the destination). A
        later commit must NOT remove the originals."""
        journal = tmp_path / "journal.json"
        conn, store, pid, client = self._ready([1, 2], capabilities=UIDPLUS_ONLY)
        real_flags = client.add_flags.side_effect
        client.add_flags.side_effect = KeyboardInterrupt()
        with pytest.raises(KeyboardInterrupt):
            qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        client.add_flags.side_effect = real_flags
        client._dest.clear()  # copies deleted

        fresh = qm.PreviewStore()
        new_pid = _preview(conn, fresh, journal_path=journal)["preview_id"]
        out = qm.commit(conn, fresh, preview_id=new_pid, max_count=10, journal_path=journal)

        assert client._present == {1, 2}
        client.uid_expunge.assert_not_called()
        assert out["held_back_uncertain"] == 2
        assert "resumed" not in out

    def test_one_missing_copy_is_enough_to_keep_all_originals(
        self, tmp_path: Path
    ) -> None:
        journal = tmp_path / "journal.json"
        conn, store, pid, client = self._ready([1, 2, 3], capabilities=UIDPLUS_ONLY)
        real_flags = client.add_flags.side_effect
        client.add_flags.side_effect = IMAPClientAbortError("dropped")
        qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        client.add_flags.side_effect = real_flags
        client._dest.discard(min(client._dest))  # one copy deleted

        out = qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        assert out["held_back_uncertain"] == 3
        assert client._present == {1, 2, 3}
        client.uid_expunge.assert_not_called()

    def test_missing_destination_holds_instead_of_blocking(
        self, tmp_path: Path
    ) -> None:
        journal = tmp_path / "journal.json"
        conn, store, pid, client = self._ready([1, 2], capabilities=UIDPLUS_ONLY)
        real_flags = client.add_flags.side_effect
        client.add_flags.side_effect = IMAPClientAbortError("dropped")
        qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        client.add_flags.side_effect = real_flags

        def select(name: str, readonly: bool = False) -> dict[bytes, int]:
            if name == "_To Delete":
                raise IMAPClientError("[NONEXISTENT] no such mailbox")
            return {b"UIDVALIDITY": 7}

        client.select_folder.side_effect = select
        out = qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        assert out["outcome"] == "complete"
        assert out["held_back_uncertain"] == 2
        assert client._present == {1, 2}
        # The source is selected for writing again after the failed look.
        assert client.select_folder.call_args.args[0] == "News"

    def test_second_commit_on_a_busy_preview_is_refused(self) -> None:
        conn, store, pid, client = self._ready([1, 2])
        entry = store.get(pid)
        assert entry.lock.acquire(blocking=False)
        try:
            with pytest.raises(qm.CommitInProgressError):
                qm.commit(conn, store, preview_id=pid, max_count=10)
        finally:
            entry.lock.release()
        client.move.assert_not_called()
        assert qm.commit(conn, store, preview_id=pid, max_count=10)["moved"] == 2

    def test_move_uses_a_shorter_socket_timeout_and_restores_it(self) -> None:
        conn, store, pid, client = self._ready([1])
        sock = client.socket.return_value
        sock.gettimeout.return_value = 30.0
        qm.commit(conn, store, preview_id=pid, max_count=10)
        assert [c.args[0] for c in sock.settimeout.call_args_list] == [
            qm.CHUNK_SOCKET_TIMEOUT_SECONDS, 30.0,
        ]

    def test_copy_without_copyuid_cannot_be_resumed_blind(self, tmp_path: Path) -> None:
        journal = tmp_path / "journal.json"
        conn, store, pid, client = self._ready([1, 2], capabilities=UIDPLUS_ONLY)
        client.copy.side_effect = lambda chunk, dest: [None]  # no COPYUID filed
        client.add_flags.side_effect = IMAPClientAbortError("dropped")
        qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        client.add_flags.side_effect = None

        out = qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        assert out["held_back_uncertain"] == 2
        assert client._present == {1, 2}

    def test_refused_copy_leaves_no_hold(self, tmp_path: Path) -> None:
        """A tagged NO (quota, missing folder) means nothing was copied."""
        journal = tmp_path / "journal.json"
        conn, store, pid, client = self._ready([1, 2], capabilities=UIDPLUS_ONLY)
        real_copy = client.copy.side_effect
        client.copy.side_effect = IMAPClientError("[OVERQUOTA] no room")
        first = qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        assert first["outcome"] == "failed"
        assert json.loads(journal.read_text()) == []

        client.copy.side_effect = real_copy
        second = qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        assert (second["moved"], second["outcome"]) == (2, "complete")

    def test_unreadable_journal_refuses_to_move(self, tmp_path: Path) -> None:
        journal = tmp_path / "journal.json"
        journal.write_text("{not json")
        conn, store, pid, client = self._ready([1], capabilities=UIDPLUS_ONLY)
        with pytest.raises(qm.JournalUnreadableError):
            qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        client.copy.assert_not_called()

    def test_journal_matches_on_server_identity_not_account_label(
        self, tmp_path: Path
    ) -> None:
        """The same account can be named by label or UUID. A held chunk must
        still be held when the account is addressed the other way."""
        journal = tmp_path / "journal.json"
        conn, store, pid, client = self._ready([1, 2], capabilities=UIDPLUS_ONLY)
        real_copy = client.copy.side_effect
        client.copy.side_effect = IMAPClientAbortError("connection lost")
        qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        client.copy.side_effect = real_copy

        fresh = qm.PreviewStore()
        new_pid = _preview(conn, fresh, account="8901E7A9-UUID", journal_path=journal)["preview_id"]
        out = qm.commit(conn, fresh, preview_id=new_pid, max_count=10, journal_path=journal)
        assert out["held_back_uncertain"] == 2
        assert client.copy.call_count == 1

    def test_uncertain_copy_is_held_back_not_retried(self, tmp_path: Path) -> None:
        journal = tmp_path / "journal.json"
        conn, store, pid, client = self._ready([1, 2], capabilities=UIDPLUS_ONLY)
        real_copy = client.copy.side_effect
        client.copy.side_effect = IMAPClientAbortError("connection lost")

        first = qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        assert first["outcome"] == "failed"
        assert json.loads(journal.read_text())[0]["stage"] == "copying"

        client.copy.side_effect = real_copy
        second = qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)

        assert client.copy.call_count == 1  # never blindly copied again
        assert second["held_back_uncertain"] == 2
        assert second["moved"] == 0
        assert client._present == {1, 2}

    def test_uncertain_copy_clears_once_the_messages_are_gone(
        self, tmp_path: Path
    ) -> None:
        journal = tmp_path / "journal.json"
        conn, store, pid, client = self._ready([1, 2, 3], capabilities=UIDPLUS_ONLY)
        real_copy = client.copy.side_effect
        client.copy.side_effect = TimeoutError("timed out")
        qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        client.copy.side_effect = real_copy
        client._present.clear()  # the user dealt with them by hand

        fresh = qm.PreviewStore()
        client._matches = [7]
        client._present.add(7)
        new_pid = _preview(conn, fresh)["preview_id"]
        out = qm.commit(conn, fresh, preview_id=new_pid, max_count=10, journal_path=journal)
        assert "held_back_uncertain" not in out
        assert json.loads(journal.read_text()) == []
        assert out["moved"] == 1

    def test_journal_for_a_rebuilt_mailbox_is_dropped(self, tmp_path: Path) -> None:
        journal = tmp_path / "journal.json"
        journal.write_text(json.dumps([{
            "host": "imap.example.com", "login": "me@example.com",
            "source_mailbox": "News",
            "destination_mailbox": "_To Delete", "uidvalidity": 999,
            "account_id": "fixture-account", "port": 993,
            "uids": [1, 2], "stage": "copied",
        }]))
        conn, store, pid, client = self._ready([1, 2], capabilities=UIDPLUS_ONLY)
        out = qm.commit(conn, store, preview_id=pid, max_count=10, journal_path=journal)
        assert "resumed" not in out
        assert out["moved"] == 2
        assert json.loads(journal.read_text()) == []

    def test_changed_connection_identity_refused(self) -> None:
        conn, store, pid, client = self._ready([1])
        conn._host, conn._email = "imap.other.example", "someone@example.com"
        with pytest.raises(qm.SelectionStaleError, match="account"):
            qm.commit(conn, store, preview_id=pid, max_count=10)
        client.move.assert_not_called()

    def test_recount_distrusts_a_rebuilt_mailbox(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn, store, pid, client = self._ready([1, 2])
        calls = {"n": 0}

        def select(_name: str, readonly: bool = False) -> dict[bytes, int]:
            calls["n"] += 1
            return {b"UIDVALIDITY": 7 if calls["n"] == 1 else 8}

        client.select_folder.side_effect = select
        client.move.side_effect = IMAPClientError("down")
        out = qm.commit(conn, store, preview_id=pid, max_count=10)
        assert out["outcome"] == "failed"
        assert out["remaining"] is None
        assert out["remaining_unverified"] is True

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


@pytest.mark.parametrize(("field", "value"), [("_port", 1993), ("_account_id", "replacement")])
def test_preview_rejects_changed_account_or_port_before_session(field, value):
    conn = FakeConnector(_client([1]))
    store = qm.PreviewStore()
    pid = _preview(conn, store)["preview_id"]
    sessions = conn.sessions
    setattr(conn, field, value)
    with pytest.raises(qm.SelectionStaleError):
        qm.commit(conn, store, preview_id=pid, max_count=1)
    assert conn.sessions == sessions


@pytest.mark.parametrize("stage", ["copying", "copied"])
@pytest.mark.parametrize("extra", [{}, {"port": 993, "account_id": "former-account"}])
def test_unbound_journal_refuses_recovery_and_leaves_record(tmp_path, stage, extra):
    conn = FakeConnector(_client([1], capabilities=UIDPLUS_ONLY))
    store = qm.PreviewStore()
    pid = _preview(conn, store)["preview_id"]
    path = tmp_path / "journal.json"
    record = {"host": conn._host, "login": conn._email, "source_mailbox": "News",
              "uidvalidity": 7, "uids": [1], "stage": stage, **extra}
    original = json.dumps([record])
    path.write_text(original)
    with pytest.raises(qm.JournalUnreadableError):
        qm.commit(conn, store, preview_id=pid, max_count=1, journal_path=path)
    assert path.read_text() == original
    conn.client.copy.assert_not_called()
    conn.client.add_flags.assert_not_called()
    conn.client.uid_expunge.assert_not_called()


def test_recovery_refuses_rebuilt_source_after_verifying_copies(tmp_path):
    conn = FakeConnector(_client([1], capabilities=UIDPLUS_ONLY))
    store = qm.PreviewStore()
    pid = _preview(conn, store)["preview_id"]
    journal = tmp_path / "journal.json"
    client = conn.client
    client.add_flags.side_effect = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        qm.commit(conn, store, preview_id=pid, max_count=1, journal_path=journal)
    record = journal.read_bytes()
    client.reset_mock()
    client.select_folder.side_effect = [{b"UIDVALIDITY": value} for value in (7, 7, 8)]
    client.add_flags.side_effect = AssertionError("stale source UID used")
    with pytest.raises(qm.SelectionStaleError):
        qm.commit(conn, store, preview_id=pid, max_count=1, journal_path=journal)
    client.add_flags.assert_not_called()
    client.uid_expunge.assert_not_called()
    assert journal.read_bytes() == record


@pytest.mark.parametrize("binding", [{}, {"account_id": "other", "port": 993}])
def test_destination_guard_preserves_ambiguously_bound_copy(binding):
    conn = FakeConnector(_client([1]))
    store = qm.PreviewStore()
    entry = store.get(_preview(conn, store)["preview_id"])
    journal = qm.MoveJournal(None)
    record = {"host": conn._host, "login": conn._email, "stage": "copying",
              "source_mailbox": "another-source", "destination_mailbox": entry.destination_mailbox,
              "dest_uidnext": 100, "uids": [1], **binding}
    journal.add(record)
    with pytest.raises(qm.JournalUnreadableError):
        qm._guard_destination(journal, entry, (7, 110))
    assert journal.load() == [record]


def test_lost_copy_lookup_refuses_rebuilt_source_on_return(monkeypatch):
    client = _client([1])
    monkeypatch.setattr(qm, "_message_ids", lambda *_: {1: b"<fixture@test>"})
    client.select_folder.side_effect = [{b"UIDVALIDITY": 7}, {b"UIDVALIDITY": 8}]
    record = {"destination_mailbox": "dest", "dest_uidvalidity": 7, "uidvalidity": 7,
              "dest_uidnext": 100, "dest_uidceil": 100}
    with pytest.raises(qm.SelectionStaleError):
        qm._locate_copies(client, record, [1], "News", set())
    client.add_flags.assert_not_called()
    client.uid_expunge.assert_not_called()
