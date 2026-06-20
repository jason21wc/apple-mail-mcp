#!/usr/bin/env python3
"""Undo log for the attachment-retrieval skill.

The skill (``SKILL.md``) orchestrates the grab: search → present → approve →
``save_attachments``. This helper owns the ONE piece of durable state that the
inbox and the destination folder cannot provide on their own — a per-run record
of exactly which files a grab wrote — so a run can be reversed cleanly.

Deliberately NOT here: dedup state. "Already grabbed?" is answered by the
destination filesystem (a deterministic dest filename either exists or it
doesn't), so there is no second source of truth to drift. This file exists only
for undo.

Trust boundary: ``record`` is the gate — it stores the *resolved* ``dest_path``;
``undo`` only acts on a logged path whose content still matches (sha256) AND
whose resolved path is unchanged since record. The skill is responsible for only
recording paths inside a recipe's destination directory.

stdlib only. Invoke via::

    uv run python .claude/skills/attachment-retrieval/undo_log.py <cmd> ...

State lives at ``<root>/retrieval_runs/<recipe>.json`` where ``root`` is
``$APPLE_MAIL_MCP_HOME`` or ``~/.apple_mail_mcp`` — mirroring the server's
storage conventions (``templates.default_root`` / ``_NAME_RE`` /
``TemplateStore._path_for``) without importing ``apple_mail_mcp``, to keep the
skill self-contained and the fork thin. Every write is atomic (temp +
``os.replace``) so the audit trail is never left half-written.

All commands print a single ``{"success": bool, ...}`` JSON object to stdout and
exit non-zero when ``success`` is false, so the skill can branch on it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Recipe-name validation, mirrored from apple_mail_mcp.templates._NAME_RE
# (templates.py:41). Names are used as filename stems, so anything that could
# escape the retrieval_runs directory must be rejected before building a path.
_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

_STATUS_SAVED = "saved"
_STATUS_REVERTED = "reverted"


class UndoLogError(Exception):
    """Operational error with a machine-readable type for the JSON envelope."""

    def __init__(self, message: str, error_type: str = "undo_log_error") -> None:
        super().__init__(message)
        self.error_type = error_type


def default_root() -> Path:
    """Run-log directory, honoring APPLE_MAIL_MCP_HOME. Resolved at use time so
    env overrides / test isolation take effect (mirrors templates.default_root)."""
    home_override = os.environ.get("APPLE_MAIL_MCP_HOME")
    base = Path(home_override) if home_override else Path.home() / ".apple_mail_mcp"
    return base / "retrieval_runs"


def _validate_recipe(name: str) -> None:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise UndoLogError(
            f"recipe name {name!r} must match {_NAME_RE.pattern}",
            error_type="invalid_recipe_name",
        )


def _path_for(recipe: str) -> Path:
    _validate_recipe(recipe)
    return default_root() / f"{recipe}.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _gen_run_id() -> str:
    """Time-sortable, collision-safe per single user: <UTC compact>-<6 hex>."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = hashlib.sha1(os.urandom(8)).hexdigest()[:6]
    return f"{stamp}-{suffix}"


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _load(recipe: str) -> dict[str, Any]:
    path = _path_for(recipe)
    if not path.is_file():
        return {"recipe": recipe, "records": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise UndoLogError(
            f"run-log for {recipe!r} is unreadable: {exc}", error_type="corrupt_log"
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("records"), list):
        raise UndoLogError(f"run-log for {recipe!r} has unexpected shape", error_type="corrupt_log")
    return data


def _save(recipe: str, data: dict[str, Any]) -> None:
    path = _path_for(recipe)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp"
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)  # atomic on the same filesystem


def _resolve_target_run(records: list[dict[str, Any]], run_id: str | None, last: bool) -> str:
    saved = [r for r in records if r.get("status") == _STATUS_SAVED]
    if last:
        if not saved:
            raise UndoLogError("no run with saved files to undo", error_type="nothing_to_undo")
        return max(saved, key=lambda r: r.get("run_id", ""))["run_id"]
    if not run_id:
        raise UndoLogError("provide --run-id or --last", error_type="invalid_arguments")
    if not any(r.get("run_id") == run_id for r in records):
        raise UndoLogError(f"unknown run_id {run_id!r}", error_type="unknown_run_id")
    return run_id


# --- commands -------------------------------------------------------------


def cmd_new_run_id(args: argparse.Namespace) -> dict[str, Any]:
    _validate_recipe(args.recipe)
    return {"success": True, "run_id": _gen_run_id()}


def cmd_record(args: argparse.Namespace) -> dict[str, Any]:
    data = _load(args.recipe)
    dest = Path(args.dest_path).expanduser().resolve()
    if not dest.is_file():
        raise UndoLogError(
            f"dest_path does not exist (record only confirmed saves): {dest}",
            error_type="dest_missing",
        )
    record = {
        "run_id": args.run_id,
        "dest_path": str(dest),
        "sha256": _sha256_of(dest),
        "size": dest.stat().st_size,
        "saved_at": _utc_now(),
        "rfc_message_id": args.rfc_message_id,
        "sender": args.sender,
        "subject": args.subject,
        "date_received": args.date_received,
        "attachment_name": args.attachment_name,
        "status": _STATUS_SAVED,
        "reverted_at": None,
        "revert_note": None,
    }
    data["records"].append(record)
    _save(args.recipe, data)
    return {"success": True, "recorded": 1, "dest_path": str(dest)}


def cmd_undo(args: argparse.Namespace) -> dict[str, Any]:
    data = _load(args.recipe)
    records = data["records"]
    run_id = _resolve_target_run(records, args.run_id, args.last)
    deleted = missing = modified_skipped = 0
    details: list[dict[str, Any]] = []
    for rec in records:
        if rec.get("run_id") != run_id or rec.get("status") != _STATUS_SAVED:
            continue
        dest_str = rec.get("dest_path")
        if not dest_str:  # malformed/hand-edited record — skip, never crash undo
            continue
        dest = Path(dest_str)
        if not dest.exists():
            rec["status"] = _STATUS_REVERTED
            rec["reverted_at"] = _utc_now()
            rec["revert_note"] = "file already gone"
            missing += 1
            details.append({"dest_path": dest_str, "outcome": "missing"})
        elif str(dest.resolve()) != dest_str:
            # A parent component changed (e.g. became a symlink) since record;
            # `dest_str` is the resolved path written by `record`, so a mismatch
            # means this no longer points at the file we saved. Refuse to act —
            # the sha256 check alone can't tell a moved file from a byte-twin.
            modified_skipped += 1
            details.append({"dest_path": dest_str, "outcome": "path_changed"})
        elif _sha256_of(dest) != rec.get("sha256"):
            modified_skipped += 1
            details.append({"dest_path": dest_str, "outcome": "modified_skipped"})
        else:
            dest.unlink()
            rec["status"] = _STATUS_REVERTED
            rec["reverted_at"] = _utc_now()
            rec["revert_note"] = f"undo {run_id}"
            deleted += 1
            details.append({"dest_path": dest_str, "outcome": "deleted"})
    _save(args.recipe, data)
    return {
        "success": True,
        "run_id": run_id,
        "deleted": deleted,
        "missing": missing,
        "modified_skipped": modified_skipped,
        "marked_reverted": deleted + missing,
        "details": details,
    }


def cmd_list_runs(args: argparse.Namespace) -> dict[str, Any]:
    data = _load(args.recipe)
    runs: dict[str, dict[str, Any]] = {}
    for rec in data["records"]:
        rid = rec.get("run_id", "")
        run = runs.setdefault(
            rid, {"run_id": rid, "saved_at_first": rec.get("saved_at"), "saved": 0, "reverted": 0}
        )
        if rec.get("status") == _STATUS_REVERTED:
            run["reverted"] += 1
        else:
            run["saved"] += 1
    ordered = sorted(runs.values(), key=lambda r: r["run_id"], reverse=True)
    return {"success": True, "runs": ordered}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Undo log for attachment-retrieval grabs.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_new = sub.add_parser("new-run-id", help="Generate a run_id for a grab.")
    p_new.add_argument("--recipe", required=True)
    p_new.set_defaults(func=cmd_new_run_id)

    p_rec = sub.add_parser("record", help="Record one saved file (call after each confirmed save).")
    p_rec.add_argument("--recipe", required=True)
    p_rec.add_argument("--run-id", required=True)
    p_rec.add_argument("--dest-path", required=True)
    p_rec.add_argument("--rfc-message-id", default=None)
    p_rec.add_argument("--sender", default=None)
    p_rec.add_argument("--subject", default=None)
    p_rec.add_argument("--date-received", default=None)
    p_rec.add_argument("--attachment-name", default=None)
    p_rec.set_defaults(func=cmd_record)

    p_undo = sub.add_parser("undo", help="Reverse a run (delete the files it wrote).")
    p_undo.add_argument("--recipe", required=True)
    grp = p_undo.add_mutually_exclusive_group(required=True)
    grp.add_argument("--run-id", default=None)
    grp.add_argument("--last", action="store_true")
    p_undo.set_defaults(func=cmd_undo)

    p_list = sub.add_parser("list-runs", help="List runs newest-first with status counts.")
    p_list.add_argument("--recipe", required=True)
    p_list.set_defaults(func=cmd_list_runs)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = args.func(args)
    except UndoLogError as exc:
        json.dump({"success": False, "error": str(exc), "error_type": exc.error_type}, sys.stdout)
        sys.stdout.write("\n")
        return 1
    except Exception as exc:  # never break the JSON contract (e.g. PermissionError on undo)
        json.dump({"success": False, "error": str(exc), "error_type": "internal_error"}, sys.stdout)
        sys.stdout.write("\n")
        return 1
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
