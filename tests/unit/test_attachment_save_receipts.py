"""Save acknowledgments must survive partial writes and bookkeeping failures."""
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from apple_mail_fast_mcp.mail_connector import AppleMailConnector

pytestmark = pytest.mark.usefixtures("_stub_transport_account_identity")


def test_failed_native_target_cannot_prune_existing_file(tmp_path):
    old = tmp_path / "old.bin"
    old.write_bytes(b"pre-existing file bigger than cap")
    connector = AppleMailConnector(max_attachment_bytes=3, max_total_attachment_bytes=3)
    with patch.object(connector, "_get_attachments_applescript", return_value=[{"name": "old.bin", "size": 0}]), patch.object(
        connector, "_run_applescript", return_value=json.dumps({"saved_positions": [], "failures": [{"position": 0, "error_code": -10000}]})
    ):
        result = connector.save_attachments("123", tmp_path)
    assert old.read_bytes() == b"pre-existing file bigger than cap"
    assert result["saved"] == 0 and result["failed"] == 1


def test_imap_local_write_failure_never_replays_native(tmp_path):
    connector = AppleMailConnector()
    native = Mock(side_effect=AssertionError("must not fall back after local write"))
    with patch.object(connector, "_resolve_imap_config", return_value=("imap.example.test", 993, "fixture@example.test")), patch.object(
        connector, "_get_imap_password_with_fallback", return_value="fixture-only"
    ), patch("apple_mail_fast_mcp.mail_connector.ImapConnector") as imap, patch.object(
        Path, "write_bytes", side_effect=OSError(13, "fixture denied")
    ), patch.object(connector, "_get_attachments_applescript", native):
        imap.return_value.fetch_attachment_payloads.return_value = (1, {0: ("a.txt", "text", "plain", b"fixture")})
        result = connector.save_attachments("fixture@example.test", tmp_path, account="Fixture", mailbox="INBOX")
    assert result["failed"] == 1 and result["saved"] == 0
    native.assert_not_called()


@pytest.mark.parametrize("acknowledged_target", ["missing", "directory"])
def test_acknowledged_nonfile_is_incomplete(tmp_path, acknowledged_target):
    from apple_mail_fast_mcp.attachment_saves import MailAttachmentSaveIncompleteError
    connector = AppleMailConnector()
    if acknowledged_target == "directory":
        (tmp_path / "a.txt").mkdir()
    with patch.object(connector, "_get_attachments_applescript", return_value=[{"name": "a.txt"}]), patch.object(
        connector, "_run_applescript", return_value='{"saved_positions":[0],"failures":[]}'
    ), pytest.raises(MailAttachmentSaveIncompleteError) as caught:
        connector.save_attachments("123", tmp_path)
    assert caught.value.phase == "postwrite_caps"
    assert caught.value.acknowledged_writes == 1
    assert caught.value.__cause__ is not None


def test_bookkeeping_failure_cannot_replay_completed_save(tmp_path):
    connector = AppleMailConnector()
    completed = {"saved": 1, "failed": 0, "rejected": []}
    with patch.object(connector, "_imap_save_attachments", return_value=completed), patch.object(
        connector, "_imap_clear_breaker", side_effect=OSError("audit unavailable")
    ), patch.object(connector, "_get_attachments_applescript") as native:
        assert connector.save_attachments("fixture@example.test", tmp_path, account="Fixture", mailbox="INBOX") == completed
    native.assert_not_called()


@pytest.mark.parametrize("malformed", ["", "0", "{}", '{"saved_positions":{},"failures":[]}'])
def test_native_invalid_receipt_is_incomplete_not_saved_zero(tmp_path, malformed):
    from apple_mail_fast_mcp.attachment_saves import MailAttachmentSaveIncompleteError
    connector = AppleMailConnector()
    with patch.object(connector, "_get_attachments_applescript", return_value=[{"name": "a.txt"}]), patch.object(
        connector, "_run_applescript", return_value=malformed
    ), pytest.raises(MailAttachmentSaveIncompleteError) as caught:
        connector.save_attachments("123", tmp_path)
    assert caught.value.phase == "native_receipt"


@pytest.mark.parametrize("error_kind", ["account", "mailbox", "message"])
def test_lookup_failures_before_write_keep_type(tmp_path, error_kind):
    from apple_mail_fast_mcp.exceptions import (
        MailAccountNotFoundError,
        MailMailboxNotFoundError,
        MailMessageNotFoundError,
    )
    error = {"account": MailAccountNotFoundError, "mailbox": MailMailboxNotFoundError, "message": MailMessageNotFoundError}[error_kind]("fixture")
    connector = AppleMailConnector()
    with patch.object(connector, "_get_attachments_applescript", return_value=[{"name": "a.txt"}]), patch.object(
        connector, "_run_applescript", side_effect=error
    ), pytest.raises(type(error)) as caught:
        connector.save_attachments("123", tmp_path)
    assert caught.value is error


def test_server_partial_failure_reports_known_counts(tmp_path):
    from apple_mail_fast_mcp import server
    result = {"saved": 1, "failed": 1, "attempted": 2, "selected": 2, "attachments_found": 2,
              "failures": [{"index": 1, "name": "b", "outcome": "unconfirmed"}], "rejected": [],
              "partial": True, "reason": None}
    with patch.object(server.mail, "save_attachments", return_value=result), patch.object(server, "check_rate_limit", return_value=None):
        response = server.save_attachments("123", str(tmp_path))
    assert response["success"] is False and response["error_type"] == "attachment_save_failed"
    assert response["saved"] == 1 and response["attempted"] == 2 and response["retry_safe"] is False


def test_server_incomplete_omits_unknown_counts(tmp_path):
    from apple_mail_fast_mcp import server
    from apple_mail_fast_mcp.attachment_saves import MailAttachmentSaveIncompleteError
    with patch.object(server.mail, "save_attachments", side_effect=MailAttachmentSaveIncompleteError("native_execution", 2)), patch.object(
        server, "check_rate_limit", return_value=None
    ):
        response = server.save_attachments("123", str(tmp_path))
    assert response["error_type"] == "attachment_save_incomplete"
    assert "saved" not in response and "attempted" not in response


@pytest.mark.parametrize("commit_error", [FileExistsError("occupied"), OSError("commit unknown")])
def test_named_commit_failure_never_reports_staged_saved_as_final(tmp_path, commit_error):
    from apple_mail_fast_mcp import server
    def stage(**kwargs):
        (kwargs["save_directory"] / "source.txt").write_bytes(b"fixture")
        return {"saved": 1, "failed": 0, "rejected": [], "reason": None}
    with patch.object(server.mail, "save_attachments", side_effect=stage), patch.object(server, "check_rate_limit", return_value=None), patch.object(
        server.os, "link", side_effect=commit_error
    ):
        response = server.save_attachments("123", str(tmp_path), [0], "final.txt")
    assert response["success"] is False
    if isinstance(commit_error, FileExistsError):
        assert response["error_type"] == "already_exists" and response["saved"] == 0
    else:
        assert response["error_type"] == "attachment_save_incomplete" and "saved" not in response
        assert response["phase"] == "final_commit"


def test_named_commit_survives_cleanup_and_audit_failure(tmp_path):
    import tempfile

    from apple_mail_fast_mcp import server
    staging = tempfile.TemporaryDirectory(dir=tmp_path)
    def stage(**kwargs):
        (kwargs["save_directory"] / "source.txt").write_bytes(b"fixture")
        return {"saved": 1, "failed": 0, "rejected": [], "reason": None}
    cleanup = staging.cleanup
    try:
        with patch.object(server.mail, "save_attachments", side_effect=stage), patch.object(server, "check_rate_limit", return_value=None), patch.object(
            server.tempfile, "TemporaryDirectory", return_value=staging
        ), patch.object(staging, "cleanup", side_effect=OSError("cleanup unavailable")), patch.object(
            server.operation_logger, "log_operation", side_effect=OSError("audit unavailable")
        ):
            response = server.save_attachments("123", str(tmp_path), [0], "final.txt")
        assert response["success"] is True and response["saved"] == 1 and response["audit_log_failed"] is True
        assert (tmp_path / "final.txt").read_bytes() == b"fixture"
    finally:
        cleanup()


def test_named_staging_inspection_preserves_acknowledgment(tmp_path):
    from apple_mail_fast_mcp import server

    with patch.object(server.mail, "save_attachments", return_value={"saved": 1}), patch.object(
        server, "check_rate_limit", return_value=None
    ), patch.object(Path, "iterdir", side_effect=PermissionError("inaccessible staging directory")):
        response = server.save_attachments("123", str(tmp_path), [0], "final.txt")

    assert response["error_type"] == "attachment_save_incomplete"
    assert response["phase"] == "staging_receipt"
    assert response["acknowledged_writes"] == 1
    assert response["retry_safe"] is False and "saved" not in response
