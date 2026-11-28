"""Read-only account probes; never reuse a pooled connection or fetch messages."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError, LoginError

from .exceptions import (
    MailKeychainAccessDeniedError,
    MailKeychainEntryNotFoundError,
    MailKeychainError,
)
from .imap_connector import CONNECT_TIMEOUT_S, _apply_operation_timeout, _reject_control_chars
from .keychain import ImapCredential, get_imap_credential

if TYPE_CHECKING:
    from .mail_connector import AppleMailConnector

DIAGNOSTIC_KEYCHAIN_TIMEOUT_S = 30.0
_CAPABILITIES = frozenset({
    "IMAP4REV1", "IMAP4REV2", "MOVE", "UIDPLUS", "IDLE", "NAMESPACE",
    "SPECIAL-USE", "THREAD=REFERENCES", "THREAD=ORDEREDSUBJECT", "X-GM-EXT-1",
})


def _failure(error_type: str, guidance: str) -> dict[str, Any]:
    return {"status": "unavailable", "error_type": error_type, "guidance": guidance}


def _credential(connector: AppleMailConnector, account: str, login: str) -> tuple[ImapCredential, str]:
    return connector._get_account_credential(
        account, login,
        lambda name, email: get_imap_credential(name, email, timeout=DIAGNOSTIC_KEYCHAIN_TIMEOUT_S),
    )


def _probe(result: dict[str, Any], endpoint: tuple[str, int, str], password: str, mailbox: str) -> None:
    host, port, login = endpoint
    client = None
    phase = "connect"
    try:
        client = IMAPClient(host, port=port, ssl=True, timeout=CONNECT_TIMEOUT_S)
        phase = "authentication"
        client.login(login, password)
        _apply_operation_timeout(client)
        result["imap"] = {"status": "authenticated", "authentication": "passed"}
        phase = "capabilities"
        capabilities = {c.decode("ascii", errors="replace").upper() if isinstance(c, bytes)
                        else str(c).upper() for c in client.capabilities()}
        result["imap"]["capabilities"] = sorted(capabilities & _CAPABILITIES)
        phase = "mailbox"
        client.select_folder(mailbox, readonly=True)
        result["imap"]["status"] = "available"
        result["mailbox"] = {"name": mailbox, "status": "readable"}
    except LoginError:
        result["imap"] = _failure("imap_auth_failed", "Verify the selected login and credential; rerun setup-imap for this account.")
    except (IMAPClientError, OSError):
        if phase == "mailbox":
            result["mailbox"] = {"name": mailbox, **_failure("mailbox_unavailable", "Check the exact mailbox path and server access permissions.")}
        else:
            result["imap"].update(_failure("imap_connection_failed", "Check the endpoint, network, TLS, and server availability."))
    except Exception:
        result["imap"].update(_failure("diagnostic_error", "The probe could not complete; inspect local diagnostics without exposing credentials."))
    finally:
        result["imap"]["phase"] = phase
        if client is not None:
            try:
                client.logout()
            except Exception:
                result["imap"]["cleanup_warning"] = "Connection cleanup failed; probe results are retained."


def check_account(connector: AppleMailConnector, account: str, mailbox: str) -> dict[str, Any]:
    """Return component health, not a guarantee that every mail operation works.

    Keychain subprocesses and network operations have individual timeouts. They
    do not establish a hard deadline for the whole diagnostic or DNS/TLS setup.
    Account resolution errors propagate to the MCP validation/error boundary.
    """
    for field, value in (("account", account), ("mailbox", mailbox)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")
        _reject_control_chars(value, field)
    identity = connector._resolve_account_identity(account)
    result: dict[str, Any] = {
        "success": True, "account": {"id": identity.id, "name": identity.name},
        "mail": {"status": "available"},
        "imap": {"status": "not_checked"},
        "mailbox": {"name": mailbox, "status": "not_checked"},
        "credential": {"source": "not_checked"},
        "breaker": {"open": connector._imap_breaker_open(identity.id)},
    }
    try:
        endpoint = connector._resolve_imap_config(identity.id)
    except Exception:
        result["mail"] = _failure("account_configuration_unavailable", "Check Mail.app availability and Automation access, then retry.")
        return result
    host, port, login = endpoint
    result["endpoint"] = {"host": host, "port": port}
    if not host or not login or not isinstance(port, int) or not 1 <= port <= 65535:
        result["imap"] = _failure("imap_not_configured", "Configure an IMAP endpoint and login for this account using setup-imap.")
        return result
    try:
        credential, identifier = _credential(connector, identity.id, login)
    except MailKeychainEntryNotFoundError:
        result["credential"] = _failure("imap_not_configured", "No credential was found; run setup-imap for this account.")
        return result
    except MailKeychainAccessDeniedError:
        result["credential"] = _failure("keychain_access_denied", "Allow this server to read the configured Keychain entry; no alternate credential was selected.")
        return result
    except MailKeychainError:
        result["credential"] = _failure("keychain_unavailable", "Keychain lookup failed or timed out; check Keychain access and retry.")
        return result
    result["credential"] = {"source": credential.source, "identifier": identifier}
    _probe(result, endpoint, credential.password, mailbox)
    return result
