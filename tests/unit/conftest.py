"""Shared fixtures for unit tests."""

import socket
import subprocess

import pytest

from apple_mail_fast_mcp.mail_connector import AppleMailConnector, _AccountIdentity
from apple_mail_fast_mcp.security import rate_limiter


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    """Reset rate limiter state between tests to prevent cross-contamination."""
    rate_limiter.reset()


@pytest.fixture(autouse=True)
def _block_real_io(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail fast on real network / DNS / subprocess in the unit suite (#408).

    A unit test that reaches real DNS, a real socket, or a real subprocess
    (``osascript`` / the ``security`` Keychain CLI) is a bug: it's fast on a
    dev machine but blocks for seconds in CI, silently ballooning the suite.
    That's exactly what #408 was — ``build_draft_mime`` → ``make_msgid()`` →
    ``socket.getfqdn()`` reverse-DNS, ~5s per call in CI — and #298 before it
    (an un-mocked osascript fallback). A first osascript can also trigger a
    ~20-30s Mail.app cold-launch in CI. Surface such leaks as an immediate
    ``RuntimeError`` instead of a multi-minute hang.

    Everything that touches the OS in production is already mocked in the unit
    suite (``IMAPClient`` at the module symbol, ``smtplib``, ``_run_applescript``,
    ``subprocess.run``), so this breaks nothing; an in-test ``mock.patch`` of a
    seam shadows this fixture. Tests that genuinely need a live seam opt out
    with ``@pytest.mark.allow_real_io``.
    """
    if request.node.get_closest_marker("allow_real_io"):
        return

    def _blocked(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError(
            "Real network/DNS/subprocess in a unit test (#408): mock it, or "
            "mark the test @pytest.mark.allow_real_io."
        )

    for name in (
        "getfqdn",
        "getaddrinfo",
        "gethostbyaddr",
        "gethostbyname",
        "create_connection",
    ):
        monkeypatch.setattr(socket, name, _blocked)
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(subprocess, "run", _blocked)


@pytest.fixture
def _stub_transport_account_identity(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy transport tests isolate account resolution from their I/O seam.

    Identity contract tests opt out with real_account_fallback and supply a
    real account snapshot. Here name=id preserves the generic account labels
    used by pre-existing transport/configuration tests without live Mail reads.
    """
    if request.node.get_closest_marker("real_account_fallback"):
        return
    monkeypatch.setattr(
        AppleMailConnector, "_resolve_account_identity",
        lambda self, account: _AccountIdentity(name=account, id=account),
    )


@pytest.fixture(autouse=True)
def _isolate_application_state(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Unit tests must not inherit a developer's persisted account overrides."""
    monkeypatch.setenv("APPLE_MAIL_MCP_HOME", str(tmp_path / "application-state"))
