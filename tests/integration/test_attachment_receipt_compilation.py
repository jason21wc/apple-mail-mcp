"""Compile native save source and execute Foundation only; never operate Mail."""
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from apple_mail_fast_mcp.mail_connector import AppleMailConnector

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="AppleScript compiler requires macOS")


def generated_script(tmp_path):
    connector = AppleMailConnector()
    (tmp_path / "a.txt").write_bytes(b"fixture")
    with patch.object(connector, "_get_attachments_applescript", return_value=[{"name": "a.txt"}]), patch.object(
        connector, "_run_applescript", return_value='{"saved_positions":[0],"failures":[]}'
    ) as run:
        connector.save_attachments("123", tmp_path)
    return run.call_args.args[0]


def test_native_attachment_receipt_compiles_without_execution(tmp_path):
    bundle = next((path for path in (Path("/System/Applications/Mail.app"), Path("/Applications/Mail.app")) if path.is_dir()), None)
    if bundle is None:
        pytest.skip("Mail.app terminology required for compile-only check")
    source = generated_script(tmp_path).replace('application "Mail"', f'application "{bundle}"')
    path = tmp_path / "save.applescript"
    path.write_text(source)
    compiled = subprocess.run(["/usr/bin/osacompile", "-o", str(tmp_path / "save.scpt"), str(path)], capture_output=True, text=True, timeout=30)
    assert compiled.returncode == 0, compiled.stderr


@pytest.mark.parametrize("additions,expected", [
    ("", {"saved_positions": [], "failures": []}),
    ("savedPositions's addObject:0", {"saved_positions": [0], "failures": []}),
    ("failedSaves's addObject:{|position|:0, |error_code|:-10000}", {"saved_positions": [], "failures": [{"position": 0, "error_code": -10000}]}),
])
def test_foundation_receipt_arrays_without_mail(tmp_path, additions, expected):
    generated = generated_script(tmp_path)
    # Extract only Foundation initialization and serialization, not Mail body
    # or resolver handlers. Assert the executed source has no application tell.
    initialization = generated[generated.index("set savedPositions"):generated.index('tell application "Mail"', generated.index("set savedPositions"))]
    serialization = generated[generated.index("set resultData to {|saved_positions|"):].replace("end timeout", "")
    source = 'use framework "Foundation"\nuse scripting additions\n' + initialization + additions + "\n" + serialization
    assert 'tell application' not in source and '"Mail"' not in source
    run = subprocess.run(["/usr/bin/osascript", "-e", source], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == expected
