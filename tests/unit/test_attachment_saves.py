"""Offline tests of attachment-save result and pruning contracts."""
import json
from unittest.mock import Mock, patch

import pytest
from apple_mail_fast_mcp.attachment_saves import (
    MailAttachmentSaveIncompleteError,
    SaveFailure,
    SaveReceipt,
    completed_save_result,
    incomplete_response,
    parse_save_receipt,
    save_response,
    write_payloads,
)

from apple_mail_fast_mcp.mail_connector import _prune_oversized_written


def receipt(saved=(), failures=()):
    return json.dumps({"saved_positions": list(saved), "failures": list(failures)})


@pytest.mark.parametrize("raw", ["", "0", "1", "[]", "{}", receipt([True]), receipt([-1]),
    receipt([1]), receipt([0, 0]), receipt(), receipt([0], [{"position": 0, "error_code": -1}]),
    receipt([], [{"position": 0}]), receipt([], [{"position": 0, "error_code": True}])])
def test_bad_receipts_never_become_zero(raw):
    with pytest.raises(MailAttachmentSaveIncompleteError) as caught:
        parse_save_receipt(raw, 1)
    result = incomplete_response(caught.value)
    assert result["error_type"] == "attachment_save_incomplete"
    assert result["retry_safe"] is False
    assert "saved" not in result and "attempted" not in result
    assert caught.value.__cause__ is not None


def test_receipt_maps_positions_not_mime_indices(tmp_path):
    # The caller selected original index 2 before 0, with colliding names.
    targets = [(3, tmp_path / "same.txt"), (1, tmp_path / "0_same.txt")]
    metadata = [{"name": "same.txt"}, {"name": "unused"}, {"name": "same.txt"}]
    result = completed_save_result(metadata, 2, targets,
        parse_save_receipt(receipt([0], [{"position": 1, "error_code": -10000}]), 2), [],
        prune=lambda targets: (0, []))
    assert result["saved"] == 1 and result["attempted"] == 2 and result["failed"] == 1
    assert result["failures"] == [{"index": 0, "name": "same.txt", "outcome": "unconfirmed", "error_code": -10000}]
    assert save_response(result, str(tmp_path))["success"] is False


def test_failed_save_never_prunes_preexisting_oversized_file(tmp_path):
    failed = tmp_path / "old.bin"
    failed.write_bytes(b"existing contents are over cap")
    saved = tmp_path / "new.bin"
    saved.write_bytes(b"ok")
    result = completed_save_result(
        [{"name": "old.bin"}, {"name": "new.bin"}], 2, [(1, failed), (2, saved)],
        SaveReceipt((1,), (SaveFailure(0, -10000),)), [],
        prune=lambda targets: _prune_oversized_written(targets, per_cap=3, total_cap=3),
    )
    assert failed.read_bytes() == b"existing contents are over cap"
    assert saved.read_bytes() == b"ok"
    assert result["saved"] == 1 and result["failed"] == 1 and result["rejected"] == []


def test_successful_oversized_write_is_still_pruned(tmp_path):
    path = tmp_path / "new.bin"
    path.write_bytes(b"oversized")
    result = completed_save_result([{ "name": "new.bin"}], 1, [(1, path)], SaveReceipt((0,), ()), [],
        prune=lambda targets: _prune_oversized_written(targets, per_cap=3, total_cap=3))
    assert result["saved"] == 0 and result["failed"] == 0
    assert result["rejected"][0]["reason"] == "per_attachment_cap_postwrite"
    assert result["reason"] == "all_rejected" and result["partial"] is True
    assert not path.exists()


@pytest.mark.parametrize("count,selected,rejected,reason", [
    (0, 0, [], "no_attachments"), (2, 0, [], "no_valid_selection"),
    (1, 1, [{"name": "a", "reason": "per_attachment_cap", "size": 100}], "all_rejected"),
])
def test_confirmed_zero_outcomes_are_distinct(count, selected, rejected, reason):
    result = completed_save_result([{"name": "a"}] * count, selected, [], SaveReceipt((), ()), rejected,
                                   prune=lambda targets: (0, []))
    assert result["reason"] == reason and result["saved"] == result["attempted"] == result["failed"] == 0
    assert save_response(result, "/fixture")["success"] is True


def test_python_write_failure_is_data_not_network_fallback(tmp_path):
    from pathlib import Path
    original = Path.write_bytes
    def write(path, payload):
        if path.name == "fail.bin":
            raise OSError(13, "permission denied")
        return original(path, payload)
    with patch.object(Path, "write_bytes", write):
        result = write_payloads([(3, tmp_path / "fail.bin"), (1, tmp_path / "ok.bin")], {0: b"ok", 2: b"failed"})
    assert result == SaveReceipt((1,), (SaveFailure(0, 13),))
    assert (tmp_path / "ok.bin").read_bytes() == b"ok"


def test_short_write_is_not_acknowledged(tmp_path):
    with patch("pathlib.Path.write_bytes", return_value=1):
        result = write_payloads([(1, tmp_path / "short.bin")], {0: b"long"})
    assert result.saved_positions == () and len(result.failures) == 1


def test_prune_error_preserves_cause_and_does_not_invent_final_count(tmp_path):
    failure = OSError("stat failed")
    with pytest.raises(MailAttachmentSaveIncompleteError) as caught:
        completed_save_result([{"name": "a"}], 1, [(1, tmp_path / "a")], SaveReceipt((0,), ()), [],
                              prune=Mock(side_effect=failure))
    assert caught.value.__cause__ is failure
    assert incomplete_response(caught.value)["acknowledged_writes"] == 1
    assert "saved" not in incomplete_response(caught.value)
