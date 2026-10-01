"""SMTP submission for wrapper-free immediate send (issue #322).

``create_draft(send_now=True)`` historically routed through Mail.app's
AppleScript ``tell theMessage to send``, whose ``content`` setter wraps the
body in an ``Apple-Mail-URLShareWrapper`` cite-blockquote that renders as a
quote on iOS (Mail.app bug FB11734014, #245). IMAP ``APPEND`` fixed the
draft case (#246/#292) but can only create drafts, never send. This module
submits a clean RFC 822 message (built by
:func:`apple_mail_fast_mcp.draft_builder.build_draft_mime`) over SMTP, so a
direct send never touches the AppleScript ``content`` setter.

Credential model: the account's IMAP app-password (see :mod:`keychain`) is
reused for providers supporting password authentication (such as iCloud).
Microsoft's OAuth flow is not implemented. The same configured credential
authenticates IMAP and SMTP submission where the provider permits it, so
we deliberately reuse the existing Keychain entry rather than building a
parallel SMTP credential store (issue #322 "extend the keychain module").

``smtplib`` is the single point of external I/O for this module and the mock
boundary for unit tests, mirroring ``_run_applescript`` (AppleScript) and
``IMAPClient`` (IMAP).
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email import message_from_bytes
from email.message import Message
from email.policy import default as _default_policy
from email.utils import parseaddr

from .exceptions import MailError

logger = logging.getLogger(__name__)

# Implicit-TLS submission port (RFC 8314). Anything else (587 submission,
# 25) is treated as explicit-TLS: connect cleartext, then STARTTLS.
_SMTP_SSL_PORT = 465

_DEFAULT_TIMEOUT_S = 30.0


class MailSmtpSendOutcomeUnknownError(MailError):
    """Submission started but its outcome is unknown; never resend via fallback."""

    def __init__(self, requested: int, candidate_message_id: str | None) -> None:
        super().__init__(
            "SMTP submission may have delivered the message, but no conclusive "
            "response was received. Do not resend without checking delivery."
        )
        self.requested = requested
        self.candidate_message_id = candidate_message_id
        self.phase = "submission"


class MailSmtpSendRejectedError(MailError):
    """Explicit SMTP rejection; switching transport must not override it."""

    def __init__(self, cause: smtplib.SMTPException, requested: int) -> None:
        super().__init__("The SMTP server rejected the submission; it was not resent.")
        self.requested = requested
        self.smtp_code: int | None = getattr(cause, "smtp_code", None)
        self.refused_recipients: dict[str, tuple[int, bytes]] = getattr(cause, "recipients", {})
        self.phase = (
            "sender" if isinstance(cause, smtplib.SMTPSenderRefused)
            else "recipients" if isinstance(cause, smtplib.SMTPRecipientsRefused)
            else "data"
        )


class SmtpSender:
    """Submit a pre-built RFC 822 message over authenticated SMTP.

    One instance targets one account's outgoing server. The message bytes
    come from ``build_draft_mime`` (the same builder the clean IMAP draft
    path uses), so message construction is already correct and tested — this
    class only adds the send transport.
    """

    def __init__(
        self,
        host: str,
        port: int,
        email: str,
        password: str,
        *,
        timeout: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        """Store connection parameters.

        Args:
            host: SMTP server hostname (e.g. ``smtp.mail.me.com``).
            port: SMTP submission port. ``465`` uses implicit TLS
                (``SMTP_SSL``); any other value connects cleartext then
                issues ``STARTTLS``.
            email: SMTP AUTH username (the login credential). This is *not*
                necessarily the envelope ``MAIL FROM`` address — for a
                custom-domain iCloud account, or a Gmail/Outlook send-as
                alias, the login and the sending identity legitimately
                differ. The envelope-from is resolved separately in
                :meth:`send`.
            password: App-specific password (shared with IMAP; see module
                docstring).
            timeout: Socket timeout in seconds.
        """
        self._host = host
        self._port = port
        self._email = email
        self._password = password
        self._timeout = timeout

    def send(
        self,
        raw_message: bytes,
        recipients: list[str],
        *,
        envelope_from: str | None = None,
    ) -> dict[str, tuple[int, bytes]]:
        """Authenticate and submit ``raw_message`` to ``recipients``.

        The ``Bcc`` header is stripped from the transmitted message — blind
        recipients are carried only in the envelope ``RCPT TO`` list, never
        on the wire — while the caller-supplied ``recipients`` list is used
        verbatim as the envelope recipients (it already includes any Bcc).

        Envelope ``MAIL FROM`` is the *sender's own address*, kept separate
        from the SMTP AUTH login (``self._email``). The two frequently
        differ: a custom-domain iCloud account authenticates as its Apple ID
        login (e.g. ``appleid@example.com``) but sends *as* its configured
        send-as address (e.g. ``you@example.com``); Gmail and Outlook send-as
        aliases behave the same way. Reusing the login as the envelope sender
        makes iCloud reject the message with ``550 5.7.0 From address is not
        one of your addresses`` (issue #322 / PR #404). When ``envelope_from``
        is ``None`` the address is taken from the message ``From:`` header —
        the same address the recipient sees.

        Only failures before submission remain eligible for another transport.
        An explicit sender/recipient/DATA rejection is returned as a typed
        rejection; a lost response after submission starts is an unknown
        outcome, not permission to send again. Cleanup cannot replace either
        an acknowledged result or the primary submission error.

        Returns:
            Refused recipients and SMTP status; an empty mapping means all
            recipients were accepted. Partial acceptance must never be retried
            for the full recipient list.

        Args:
            raw_message: A serialized RFC 822 message from
                ``build_draft_mime``.
            recipients: The full envelope recipient list (to + cc + bcc).
                Must be non-empty.
            envelope_from: Explicit envelope ``MAIL FROM`` address. When
                ``None``, it is parsed from the message ``From:`` header.

        Raises:
            ValueError: ``recipients`` is empty, or no envelope-from address
                could be resolved (no override and no parseable ``From:``).
            smtplib.SMTPException: Setup/authentication failed before submission.
            OSError: Connection failure before submission.
            MailSmtpSendRejectedError: Explicit sender, recipient, or DATA rejection.
            MailSmtpSendOutcomeUnknownError: Submission was dispatched but its
                outcome is uncertain. Neither typed error permits native replay.
        """
        if not recipients:
            raise ValueError("SMTP send requires at least one recipient")

        msg = message_from_bytes(raw_message, policy=_default_policy)
        # A Bcc header must never travel on the wire; the envelope RCPT list
        # (passed explicitly below) is what actually delivers to blind
        # recipients. ``del`` is a no-op when no Bcc header is present.
        del msg["Bcc"]
        env_from = self._resolve_envelope_from(msg, envelope_from)

        context = ssl.create_default_context()
        client = (
            smtplib.SMTP_SSL(
                self._host, self._port, timeout=self._timeout, context=context,
            ) if self._port == _SMTP_SSL_PORT else smtplib.SMTP(
                self._host, self._port, timeout=self._timeout,
            )
        )
        try:
            if self._port != _SMTP_SSL_PORT:
                client.ehlo()
                client.starttls(context=context)
                client.ehlo()
            return self._authenticate_and_send(client, msg, recipients, env_from)
        finally:
            self._finish_session(client)

    @staticmethod
    def _finish_session(client: smtplib.SMTP) -> None:
        """Never let QUIT/close or cleanup logging replace a submission outcome."""
        for action in (client.quit, client.close):
            try:
                action()
            except Exception:
                try:
                    logger.warning("SMTP cleanup failed; preserving the submission outcome")
                except Exception:
                    pass

    def _resolve_envelope_from(self, msg: Message, override: str | None) -> str:
        """Resolve the envelope ``MAIL FROM`` address (issue #322 / PR #404).

        Precedence: an explicit ``override``, else the message ``From:``
        header. Either way the bare address is extracted with
        ``email.utils.parseaddr``, so a display-name form (``Name <addr>``)
        yields just ``addr``. This is deliberately independent of the SMTP
        AUTH login (``self._email``): the two legitimately differ for
        custom-domain iCloud and send-as-alias setups.

        Args:
            msg: The parsed message whose ``From:`` header is the fallback
                source of the envelope sender.
            override: An explicit envelope-from address, or ``None``.

        Returns:
            The bare envelope ``MAIL FROM`` address.

        Raises:
            ValueError: Neither an override nor a parseable ``From:`` address
                is available.
        """
        source = override if override else msg.get("From", "")
        address = parseaddr(source)[1]
        if not address:
            raise ValueError(
                "SMTP send requires an envelope-from address "
                "(no override and no parseable From: header)"
            )
        return address

    def _authenticate_and_send(
        self,
        client: smtplib.SMTP,
        msg: Message,
        recipients: list[str],
        envelope_from: str,
    ) -> dict[str, tuple[int, bytes]]:
        """Log in with the AUTH credential and submit the message.

        The SMTP AUTH login (``self._email``) and the envelope ``MAIL FROM``
        (``envelope_from``) are passed as two distinct values — see
        :meth:`send` for why they legitimately differ. ``RCPT TO`` is the
        caller-supplied recipient list.
        """
        client.login(self._email, self._password)
        candidate = str(msg.get("Message-ID", "")).strip().strip("<>") or None
        # Entering send_message is the conservative dispatch boundary. A
        # disconnect can occur after DATA was accepted but before its reply.
        try:
            return client.send_message(msg, from_addr=envelope_from, to_addrs=recipients)
        except (smtplib.SMTPSenderRefused, smtplib.SMTPRecipientsRefused,
                smtplib.SMTPDataError) as exc:
            raise MailSmtpSendRejectedError(exc, len(recipients)) from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise MailSmtpSendOutcomeUnknownError(len(recipients), candidate) from exc
