"""Credential labels and diagnostic bounds share the runtime lookup order."""

import subprocess
from unittest.mock import patch

import pytest

from apple_mail_fast_mcp.exceptions import MailKeychainAccessDeniedError, MailKeychainError
from apple_mail_fast_mcp.keychain import get_imap_credential, get_imap_password


def test_environment_source_does_not_touch_keychain(monkeypatch):
    monkeypatch.setenv("APPLE_MAIL_MCP_IMAP_PASSWORD_WORK", " fixture-secret ")
    with patch("apple_mail_fast_mcp.keychain._run_security") as security:
        credential = get_imap_credential("Work", "login@example.invalid", timeout=0.5)
    assert credential.password == "fixture-secret"
    assert credential.source == "environment"
    assert "fixture-secret" not in repr(credential)
    security.assert_not_called()


@pytest.mark.parametrize("legacy", [False, True])
def test_source_and_timeout_follow_the_same_keychain_lookup(monkeypatch, legacy):
    monkeypatch.delenv("APPLE_MAIL_MCP_IMAP_PASSWORD_WORK", raising=False)
    found = subprocess.CompletedProcess([], 0, "fixture-secret\n", "")
    missing = subprocess.CompletedProcess([], 44, "", "not found")
    with patch("apple_mail_fast_mcp.keychain.subprocess.run", side_effect=(
        [missing, found] if legacy else [found]
    )) as run:
        credential = get_imap_credential("Work", "login@example.invalid", timeout=0.5)
    assert credential.password == "fixture-secret"
    assert credential.source == ("legacy_keychain" if legacy else "keychain")
    assert all(call.kwargs["timeout"] == 0.5 for call in run.call_args_list)


def test_access_denial_does_not_try_legacy(monkeypatch):
    monkeypatch.delenv("APPLE_MAIL_MCP_IMAP_PASSWORD_WORK", raising=False)
    with patch("apple_mail_fast_mcp.keychain.subprocess.run", return_value=
               subprocess.CompletedProcess([], 128, "", "user canceled")) as run:
        with pytest.raises(MailKeychainAccessDeniedError):
            get_imap_credential("Work", "login@example.invalid", timeout=0.5)
    run.assert_called_once()


def test_timeout_is_typed_without_subprocess_output(monkeypatch):
    monkeypatch.delenv("APPLE_MAIL_MCP_IMAP_PASSWORD_WORK", raising=False)
    failure = subprocess.TimeoutExpired(["security"], 0.5, output="private-output")
    with patch("apple_mail_fast_mcp.keychain.subprocess.run", side_effect=failure):
        with pytest.raises(MailKeychainError, match="timed out") as caught:
            get_imap_credential("Work", "login@example.invalid", timeout=0.5)
    assert "private-output" not in str(caught.value)


def test_runtime_password_getter_uses_source_aware_reader(monkeypatch):
    monkeypatch.setenv("APPLE_MAIL_MCP_IMAP_PASSWORD_WORK", "fixture-secret")
    assert get_imap_password("Work", "login@example.invalid") == "fixture-secret"
