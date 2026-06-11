"""Static parity between the test-mode safety gate's registered operations and
its server.py call sites.

The gate (check_test_mode_safety) failed open historically because the
membership sets in security.py and the call sites in server.py were maintained
independently: delete_messages never called the gate, and
update_mailbox/delete_mailbox called it while absent from ACCOUNT_GATED_OPERATIONS,
so the gate fell through every branch and allowed them on real accounts (P0-2).

These tests are the analogue of check_client_server_parity.sh: they fail the
build if the two sides drift again.
"""

import re
from pathlib import Path

from apple_mail_mcp.security import (
    _HANDLED_OPERATIONS,
    DESTRUCTIVE_ACCOUNT_OPERATIONS,
    MUTATING_OPERATIONS,
)

_SERVER = Path(__file__).resolve().parents[2] / "src" / "apple_mail_mcp" / "server.py"

# String-literal call sites: check_test_mode_safety("op", ...). create_draft /
# update_draft pass a variable `operation`, not a literal, so they don't appear
# here — they're covered by the SEND_OPERATIONS membership tests in test_security.
_LITERAL_CALL = re.compile(r'check_test_mode_safety\(\s*"([a-z_]+)"')

# Read-only ops legitimately consult the gate for account-match only.
_READ_ONLY_GATED = {"list_mailboxes", "search_messages"}


def _server_gate_call_ops() -> set[str]:
    return set(_LITERAL_CALL.findall(_SERVER.read_text()))


def test_mutating_ops_are_all_handled() -> None:
    """Every state-changing op the gate knows about must have a concrete
    handler — otherwise the fail-closed net would reject a real operation."""
    unhandled = MUTATING_OPERATIONS - _HANDLED_OPERATIONS
    assert not unhandled, (
        f"MUTATING_OPERATIONS not covered by a handler set: {unhandled}"
    )


def test_every_gate_call_site_is_registered() -> None:
    """Each check_test_mode_safety("op") literal in server.py must name a
    registered operation. An unregistered literal (typo, or a new op that
    forgot to join a set) would silently fall through to 'allowed'."""
    known = _HANDLED_OPERATIONS | _READ_ONLY_GATED
    for op in _server_gate_call_ops():
        assert op in known, (
            f'server.py calls check_test_mode_safety("{op}") but {op!r} is in '
            f"no security.py operation set — the gate would fail open for it."
        )


def test_destructive_ops_have_a_server_call_site() -> None:
    """Every destructive account op must actually invoke the gate in server.py.
    delete_messages regressed precisely by being registered-but-never-called in
    earlier states; this asserts the call exists."""
    called = _server_gate_call_ops()
    missing = DESTRUCTIVE_ACCOUNT_OPERATIONS - called
    assert not missing, (
        f"Destructive ops with no check_test_mode_safety call site in server.py: "
        f"{missing}"
    )
