"""Truthful attachment-save receipts and result projection."""
from __future__ import annotations

import errno
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .exceptions import MailError
from .utils import parse_applescript_json


class MailAttachmentSaveIncompleteError(MailError):
    """Files may have changed, but final save progress is not established."""

    def __init__(self, phase: str, targets: int, *, acknowledged_writes: int | None = None):
        super().__init__(
            "Attachment saving did not return a conclusive result. Inspect the "
            "destination before retrying; no alternate transport was attempted."
        )
        self.phase = phase
        self.targets = targets
        self.acknowledged_writes = acknowledged_writes


@dataclass(frozen=True)
class SaveFailure:
    position: int
    error_code: int | None


@dataclass(frozen=True)
class SaveReceipt:
    saved_positions: tuple[int, ...]
    failures: tuple[SaveFailure, ...]


def _valid_position(value: Any, count: int) -> bool:
    return type(value) is int and 0 <= value < count


def parse_save_receipt(payload: str, count: int) -> SaveReceipt:
    """Require one acknowledgment or failure for each dispatched target position."""
    try:
        data = parse_applescript_json(payload)
        if not isinstance(data, dict):
            raise ValueError("Receipt is not an object")
        saved = data.get("saved_positions")
        failures = data.get("failures")
        if not isinstance(saved, list) or not isinstance(failures, list):
            raise ValueError("Receipt lists missing")
        if any(not _valid_position(pos, count) for pos in saved):
            raise ValueError("Invalid acknowledged position")
        failed = []
        for row in failures:
            if (not isinstance(row, dict)
                    or not _valid_position(row.get("position"), count)
                    or type(row.get("error_code")) is not int):
                raise ValueError("Invalid failed position/code")
            failed.append(SaveFailure(row["position"], row["error_code"]))
        positions = saved + [row.position for row in failed]
        if len(positions) != count or set(positions) != set(range(count)):
            raise ValueError("Receipt is incomplete or contains duplicate positions")
        return SaveReceipt(tuple(saved), tuple(failed))
    except (ValueError, TypeError, MailError) as exc:
        raise MailAttachmentSaveIncompleteError("native_receipt", count) from exc


def write_payloads(
    targets: list[tuple[int, Path]], payloads: Mapping[int, bytes],
) -> SaveReceipt:
    """Keep local write failures out of network fallback handling.

    Payload keys are original zero-based MIME indices; target entries retain
    the connector's one-based indexing. A failed write may leave partial bytes.
    """
    saved: list[int] = []
    failures: list[SaveFailure] = []
    for position, (one_based_index, path) in enumerate(targets):
        payload = payloads[one_based_index - 1]
        try:
            written = path.write_bytes(payload)
            if written != len(payload):
                raise OSError(errno.EIO, "Attachment write was short")
        except OSError as exc:
            failures.append(SaveFailure(position, exc.errno))
        else:
            saved.append(position)
    return SaveReceipt(tuple(saved), tuple(failures))


def _zero_reason(found: int, selected: int, failed: int, rejected: list[dict[str, Any]]) -> str:
    if failed:
        return "save_failed"
    if not found:
        return "no_attachments"
    if not selected:
        return "no_valid_selection"
    if rejected:
        return "all_rejected"
    return "no_safe_targets"


def completed_save_result(
    attachments: list[dict[str, Any]], selected: int, targets: list[tuple[int, Path]],
    receipt: SaveReceipt, rejected: list[dict[str, Any]], *,
    prune: Callable[[list[tuple[int, Path]]], tuple[int, list[dict[str, Any]]]],
) -> dict[str, Any]:
    """Prune only acknowledged writes, then report retained successes.

    The existing pruning helper remains responsible for disk/aggregate caps.
    Never stat/delete an unacknowledged target: it may be a pre-existing file.
    """
    acknowledged = [targets[position] for position in receipt.saved_positions]
    try:
        removed, post_rejected = prune(acknowledged)
        if type(removed) is not int or not 0 <= removed <= len(acknowledged):
            raise ValueError("Invalid prune progress")
    except Exception as exc:
        raise MailAttachmentSaveIncompleteError(
            "postwrite_caps", len(targets), acknowledged_writes=len(acknowledged),
        ) from exc
    saved = len(acknowledged) - removed
    failures = []
    for failure in receipt.failures:
        index = targets[failure.position][0] - 1
        row: dict[str, Any] = {
            "index": index, "name": str(attachments[index].get("name", "")),
            "outcome": "unconfirmed",
        }
        if failure.error_code is not None:
            row["error_code"] = failure.error_code
        failures.append(row)
    all_rejected = rejected + post_rejected
    return {
        "saved": saved, "rejected": all_rejected,
        "attachments_found": len(attachments), "selected": selected,
        "attempted": len(targets), "failed": len(failures), "failures": failures,
        "partial": bool(failures or all_rejected),
        "reason": None if saved else _zero_reason(len(attachments), selected, len(failures), all_rejected),
    }


def save_response(result: dict[str, Any], directory: str) -> dict[str, Any]:
    """Project final connector results; named saves call this only after commit."""
    response = {"success": not bool(result.get("failed", 0)), "directory": directory, **result}
    if result.get("failed", 0):
        response.update(
            error="One or more attachment saves were not acknowledged; inspect the destination before retrying.",
            error_type="attachment_save_failed", retry_safe=False,
        )
    elif result.get("reason") == "no_safe_targets":
        response.update(success=False, error="No selected attachment has a safe destination.",
                        error_type="invalid_destination")
    return response


def incomplete_response(error: MailAttachmentSaveIncompleteError) -> dict[str, Any]:
    response: dict[str, Any] = {
        "success": False, "error_type": "attachment_save_incomplete", "error": str(error),
        "partial": True, "retry_safe": False, "phase": error.phase,
        "targets": error.targets,
    }
    if error.acknowledged_writes is not None:
        response["acknowledged_writes"] = error.acknowledged_writes
    return response
