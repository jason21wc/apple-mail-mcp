"""Native move acknowledgments must not invent success or invite replay."""
import json
from unittest.mock import MagicMock

import pytest

from apple_mail_fast_mcp.exceptions import MailAppleScriptError, MailMutationOutcomeUnknownError
from apple_mail_fast_mcp.mail_connector import AppleMailConnector


def payload(**changes):
    data = {"moved": 0, "not_moved": 0, "unknown": 0, "not_found": 0, "read_status": 0,
            "flag_index": 0, "flagged_status": 0, "not_moved_ids": []}
    data.update(changes)
    return json.dumps(data)


@pytest.fixture
def connector(monkeypatch):
    connector = AppleMailConnector()
    monkeypatch.setattr(connector, "list_accounts", lambda: [{
        "id": "11111111-1111-1111-1111-111111111111", "name": "Fixture",
        "email_addresses": ["fixture@example.test"], "enabled": True,
    }])
    monkeypatch.setattr(connector, "_try_imap_fast_paths", lambda *a, **k: None)
    return connector


def run_update(connector, monkeypatch, result, **changes):
    runner = MagicMock(return_value=result)
    monkeypatch.setattr(connector, "_run_applescript", runner)
    count = connector.update_message(["41", "42"], account="Fixture", source_mailbox="INBOX",
                                     destination_mailbox="Archive", **changes)
    return count, runner.call_args.args[0]


@pytest.mark.parametrize("changes", [{}, {"read_status": True}, {"flagged": False},
                                    {"flag_color": "green"}, {"read_status": False, "flagged": True}])
def test_all_move_patches_use_verified_result(connector, monkeypatch, changes):
    count, script = run_update(connector, monkeypatch, payload(moved=2), **changes)
    assert count == 2
    assert "set mailbox of msg to destMailbox" in script
    assert "if actualMailbox is destMailbox" in script
    assert "else if actualMailbox is originalMailbox" in script
    assert "set unknownCount to unknownCount + 1" in script
    assert "NSJSONSerialization" in script
    assert "name of mailbox of msg" not in script
    if "read_status" in changes:
        assert script.index("set read status of msg") < script.index("set mailbox of msg")
    if "flagged" in changes or "flag_color" in changes:
        assert script.index("set flag index of msg") < script.index("set mailbox of msg")


def test_verified_noop_preserves_acknowledged_preactions(connector, monkeypatch):
    from apple_mail_fast_mcp.exceptions import MailImapRequiredError
    with pytest.raises(MailImapRequiredError) as caught:
        run_update(connector, monkeypatch, payload(moved=1, not_moved=1, read_status=2,
                   flag_index=2, flagged_status=2, not_moved_ids=["42"]), read_status=True, flagged=True)
    error = caught.value
    assert error.confirmed_progress == {"moved": 1, "not_moved": 1, "unknown": 0, "not_found": 0,
                                        "read_status": 2, "flag_index": 2, "flagged_status": 2}
    assert error.retry_message_ids == ["42"]
    assert "move-only" in str(error)


def test_unknown_preserves_partial_property_acknowledgments(connector, monkeypatch):
    with pytest.raises(MailMutationOutcomeUnknownError) as caught:
        run_update(connector, monkeypatch, payload(moved=1, unknown=1, read_status=2,
                   flag_index=2, flagged_status=1), read_status=True, flagged=True)
    assert caught.value.confirmed_progress["moved"] == 1
    assert caught.value.confirmed_progress["flag_index"] == 2
    assert caught.value.confirmed_progress["flagged_status"] == 1


@pytest.mark.parametrize("result", ["", "0", "1,0", "garbage", "{}", payload(moved=-1),
                                    payload(moved=True), payload(moved=3),
                                    payload(not_moved=1, not_moved_ids=["999"]),
                                    payload(moved=2, unknown=1)])
def test_malformed_progress_is_unknown_not_zero(connector, monkeypatch, result):
    with pytest.raises(MailMutationOutcomeUnknownError):
        run_update(connector, monkeypatch, result)


def test_subprocess_interruption_is_unknown(connector, monkeypatch):
    runner = MagicMock(side_effect=MailAppleScriptError("Script execution timeout"))
    monkeypatch.setattr(connector, "_run_applescript", runner)
    with pytest.raises(MailMutationOutcomeUnknownError) as caught:
        connector.move_messages(["41", "42"], "Archive", "Fixture", source_mailbox="INBOX")
    assert caught.value.confirmed_progress is None
    assert runner.call_count == 1


def test_cross_scan_locates_once_before_mutating(connector, monkeypatch):
    runner = MagicMock(return_value=payload(moved=1))
    monkeypatch.setattr(connector, "_run_applescript", runner)
    assert connector.move_messages(["41"], "Archive", "Fixture") == 1
    script = runner.call_args.args[0]
    assert "repeat with acc in accounts" in script
    assert "if matched then exit repeat" in script
    assert script.index("if matched then exit repeat") < script.index("set mailbox of msg")


@pytest.mark.asyncio
async def test_mcp_verified_noop_reports_progress(connector, monkeypatch, tmp_path):
    from apple_mail_fast_mcp import server
    from apple_mail_fast_mcp.exceptions import MailNativeMoveIncompleteError
    error = MailNativeMoveIncompleteError(mailbox="Archive", requested=2,
            confirmed_progress={"moved": 1, "read_status": 2}, retry_message_ids=["42"])
    monkeypatch.setenv("MAIL_TEST_MODE", "false")
    monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
    monkeypatch.setattr(server, "check_rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(server, "mail", connector)
    monkeypatch.setattr(connector, "update_message", MagicMock(side_effect=error))
    result = await server.update_message(["41", "42"], read_status=True,
                                         destination_mailbox="Archive", account="Fixture")
    assert result["error_type"] == "imap_required"
    assert result["partial"] is True
    assert result["retry_safe"] is False
    assert result["retry_message_ids"] == ["42"]
    assert result["confirmed_progress"]["read_status"] == 2
    assert result["retry_operation"] == "move_only"


@pytest.mark.parametrize("changes", [{}, {"read_status": True}, {"read_status": False},
    {"flagged": True}, {"flagged": False}, {"flag_color": "none"},
    {"flag_color": "green"}, {"read_status": True, "flag_color": "red"}])
@pytest.mark.parametrize("outcome", ["not_moved", "unknown"])
def test_partial_batch_counts_survive_every_patch_shape(connector, monkeypatch, changes, outcome):
    from apple_mail_fast_mcp.exceptions import MailNativeMoveIncompleteError
    counts = {"moved": 1, outcome: 1,
              "read_status": 2 if "read_status" in changes else 0,
              "flag_index": 2 if ("flagged" in changes or "flag_color" in changes) else 0,
              "flagged_status": 2 if ("flagged" in changes or "flag_color" in changes) else 0}
    response = payload(**counts, not_moved_ids=["42"] if outcome == "not_moved" else [])
    expected = MailNativeMoveIncompleteError if outcome == "not_moved" else MailMutationOutcomeUnknownError
    with pytest.raises(expected) as caught:
        run_update(connector, monkeypatch, response, **changes)
    for key, count in counts.items():
        assert caught.value.confirmed_progress[key] == count


def test_account_failure_precedes_native_dispatch(connector, monkeypatch):
    from apple_mail_fast_mcp.exceptions import MailAccountNotFoundError
    runner = MagicMock()
    monkeypatch.setattr(connector, "_run_applescript", runner)
    with pytest.raises(MailAccountNotFoundError):
        connector.move_messages(["41"], "Archive", "MissingFixture")
    runner.assert_not_called()


def test_duplicate_ids_are_mutated_only_once(connector, monkeypatch):
    runner = MagicMock(return_value=payload(moved=1))
    monkeypatch.setattr(connector, "_run_applescript", runner)
    assert connector.move_messages(["41", "41"], "Archive", "Fixture") == 1
    assert 'set idList to {"41"}' in runner.call_args.args[0]


async def test_mcp_unknown_retains_native_progress(connector, monkeypatch, tmp_path):
    from apple_mail_fast_mcp import server
    error = MailMutationOutcomeUnknownError(operation="update_message", phase="native_move_verification",
        requested=2, confirmed_progress={"moved": 1, "read_status": 2, "flag_index": 2, "flagged_status": 1})
    monkeypatch.setenv("MAIL_TEST_MODE", "false")
    monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path))
    monkeypatch.setattr(server, "check_rate_limit", lambda *a, **k: None)
    monkeypatch.setattr(server, "mail", connector)
    monkeypatch.setattr(connector, "update_message", MagicMock(side_effect=error))
    result = await server.update_message(["41", "42"], read_status=True,
                                         destination_mailbox="Archive", account="Fixture")
    assert result["error_type"] == "mutation_outcome_unknown"
    assert result["confirmed_progress"] == error.confirmed_progress
    assert result["retry_safe"] is False
    assert "updated" not in result
