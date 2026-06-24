"""
Module: Email Sender (auth flows)
Purpose: Sends transactional auth emails (2FA codes, invites). Uses SMTP when
         configured; otherwise logs the message body so development works with
         zero infrastructure.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from config.setting import get_settings

logger = logging.getLogger(__name__)


def _smtp_configured() -> bool:
    s = get_settings()
    return bool(s.SMTP_HOST)


def send_email(*, to: str, subject: str, body: str) -> None:
    """Send a plaintext email, or log it in development if SMTP is absent."""
    s = get_settings()

    if not _smtp_configured():
        logger.info(
            "[email:dev] To=%s | Subject=%s\n%s", to, subject, body
        )
        return

    msg = EmailMessage()
    msg["From"] = s.SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        with smtplib.SMTP(s.SMTP_HOST, s.SMTP_PORT, timeout=15) as server:
            if s.SMTP_USE_TLS:
                server.starttls()
            if s.SMTP_USER and s.SMTP_PASSWORD:
                server.login(s.SMTP_USER, s.SMTP_PASSWORD.get_secret_value())
            server.send_message(msg)
        logger.info("Email sent to %s (%s)", to, subject)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to send email to %s: %s", to, exc)
        # Fall back to logging the body so the flow is not blocked in dev.
        logger.info("[email:fallback] To=%s | Subject=%s\n%s", to, subject, body)


def send_two_factor_code(*, to: str, code: str) -> None:
    send_email(
        to=to,
        subject="Your verification code",
        body=(
            f"Your sign-in verification code is: {code}\n\n"
            "It expires in 5 minutes. If you did not try to sign in, ignore "
            "this email."
        ),
    )


def send_invite_email(*, to: str, invite_link: str, role: str) -> None:
    send_email(
        to=to,
        subject="You've been invited to Sales Intelligence",
        body=(
            f"You have been invited to join as '{role}'.\n\n"
            f"Accept your invite and create your account here:\n{invite_link}\n\n"
            "This link expires soon."
        ),
    )
