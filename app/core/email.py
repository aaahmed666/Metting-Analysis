"""
Module: Email Service
Purpose: Sends plain-text notification emails via SMTP.
         Falls back to a warning log if SMTP is not configured in the environment.

Configure via .env:
    SMTP_HOST      — e.g. smtp.gmail.com
    SMTP_PORT      — default 587
    SMTP_USER      — sender address / login
    SMTP_PASSWORD  — sender password
    SMTP_TLS       — true (default) / false
    SMTP_FROM      — display address (defaults to SMTP_USER)
"""
from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


def send_email(to: str | list[str], subject: str, body: str) -> bool:
    """
    Sends a plain-text email to one or more recipients.

    Returns True on success.
    Returns False (with a warning log) when SMTP is not configured or sending fails.
    """
    from config.setting import get_settings
    settings = get_settings()

    smtp_host: str | None = getattr(settings, "SMTP_HOST", None)
    smtp_user: str | None = getattr(settings, "SMTP_USER", None)

    if not smtp_host or not smtp_user:
        logger.warning(
            "SMTP not configured — email skipped. To: %s | Subject: %s",
            to,
            subject,
        )
        return False

    recipients: list[str] = [to] if isinstance(to, str) else to
    smtp_port: int   = getattr(settings, "SMTP_PORT", 587)
    smtp_tls: bool   = getattr(settings, "SMTP_TLS", True)
    smtp_from: str   = getattr(settings, "SMTP_FROM", smtp_user) or smtp_user
    smtp_password    = getattr(settings, "SMTP_PASSWORD", None)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = smtp_from
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            if smtp_tls:
                server.starttls()
            if smtp_password:
                pw = (
                    smtp_password.get_secret_value()
                    if hasattr(smtp_password, "get_secret_value")
                    else smtp_password
                )
                server.login(smtp_user, pw)
            server.sendmail(smtp_from, recipients, msg.as_string())

        logger.info("Email sent → %s | %s", recipients, subject)
        return True

    except Exception as exc:
        logger.error("Failed to send email to %s: %s", recipients, exc)
        return False
