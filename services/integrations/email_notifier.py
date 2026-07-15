from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config.setting import get_settings

logger = logging.getLogger(__name__)


def send_meeting_analysis_email(
    email_to: str,
    rep_name: str,
    meeting_id: str,
    status: str,
    rejection_reason: str | None = None,
    report_data: dict | None = None,
) -> bool:
    """
    Send an automated email notification to the sales representative
    regarding the status of their meeting analysis.
    """
    settings = get_settings()

    username = settings.SMTP_USERNAME
    password = settings.SMTP_PASSWORD.get_secret_value() if settings.SMTP_PASSWORD else None

    subject = (
        f"Meeting Analysis Completed - ID: {meeting_id}"
        if status == "completed"
        else f"Meeting Analysis Failed - ID: {meeting_id}"
    )

    if status == "completed":
        grade = report_data.get("grade", "N/A") if report_data else "N/A"
        score = report_data.get("total_score", "N/A") if report_data else "N/A"
        summary = (
            report_data.get("executive_summary", "No summary available.")
            if report_data
            else "No summary available."
        )

        body_html = f"""
        <html>
        <body>
            <h3>Hello {rep_name},</h3>
            <p>Great news! The AI analysis for your meeting (ID: <strong>{meeting_id}</strong>) is complete.</p>
            <hr/>
            <p><strong>Overall Score:</strong> {score}/100</p>
            <p><strong>Grade:</strong> {grade}</p>
            <p><strong>Executive Summary:</strong></p>
            <p>{summary}</p>
            <hr/>
            <p>Log in to the dashboard to view the full report, transcript, and action items.</p>
            <br/>
            <p>Best regards,<br/>Sales Intelligence Team</p>
        </body>
        </html>
        """
        body_text = (
            f"Hello {rep_name},\n\n"
            f"Great news! The AI analysis for your meeting (ID: {meeting_id}) is complete.\n\n"
            f"Overall Score: {score}/100\n"
            f"Grade: {grade}\n\n"
            f"Executive Summary:\n{summary}\n\n"
            f"Log in to the dashboard to view the full report.\n\n"
            f"Best regards,\nSales Intelligence Team"
        )
    else:
        reason = rejection_reason or "Internal pipeline processing error."
        body_html = f"""
        <html>
        <body>
            <h3>Hello {rep_name},</h3>
            <p>We encountered an issue while analysing your meeting (ID: <strong>{meeting_id}</strong>).</p>
            <hr/>
            <p><strong>Status:</strong> Failed / Rejected</p>
            <p><strong>Reason:</strong> {reason}</p>
            <hr/>
            <p>Please review the file or contact support if the issue persists.</p>
            <br/>
            <p>Best regards,<br/>Sales Intelligence Team</p>
        </body>
        </html>
        """
        body_text = (
            f"Hello {rep_name},\n\n"
            f"We encountered an issue while analysing your meeting (ID: {meeting_id}).\n\n"
            f"Status: Failed / Rejected\n"
            f"Reason: {reason}\n\n"
            f"Please review the file or contact support if the issue persists.\n\n"
            f"Best regards,\nSales Intelligence Team"
        )

    if not username or not password:
        logger.warning(
            "SMTP credentials not fully configured (SMTP_USERNAME / SMTP_PASSWORD). "
            "Simulating email delivery to %s:\n%s",
            email_to,
            body_text,
        )
        return True

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = settings.SMTP_FROM_EMAIL
        msg["To"] = email_to

        msg.attach(MIMEText(body_text, "plain"))
        msg.attach(MIMEText(body_html, "html"))

        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
            if settings.SMTP_PORT == 587:
                server.starttls()
            server.login(username, password)
            server.sendmail(settings.SMTP_FROM_EMAIL, [email_to], msg.as_string())

        logger.info(
            "Successfully sent email notification to %s for meeting %s",
            email_to,
            meeting_id,
        )
        return True
    except Exception as exc:
        logger.error(
            "Failed to send email to %s for meeting %s: %s",
            email_to,
            meeting_id,
            exc,
        )
        return False