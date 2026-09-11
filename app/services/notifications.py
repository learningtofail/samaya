import asyncio
import logging
import smtplib
from email.mime.text import MIMEText
from os import environ

logger = logging.getLogger(__name__)

NOTIFY_EMAIL = environ.get("NOTIFY_EMAIL", "")
SMTP_HOST    = environ.get("SMTP_HOST", "")
SMTP_PORT    = int(environ.get("SMTP_PORT", "587"))
SMTP_USER    = environ.get("SMTP_USER", "")
SMTP_PASS    = environ.get("SMTP_PASS", "")


def _send_sync(subject: str, body: str) -> bool:
    """The actual blocking smtplib call — always run via asyncio.to_thread."""
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"]    = SMTP_USER
        msg["To"]      = NOTIFY_EMAIL

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USER, SMTP_PASS)
            smtp.send_message(msg)

        logger.info(f"Notification sent: {subject}")
        return True

    except Exception as e:
        logger.error(f"Notification failed: {e}")
        return False


async def send_notification(subject: str, body: str) -> bool:
    """
    Sends an email notification if NOTIFY_EMAIL and SMTP credentials are configured.
    Returns True on success, False on failure or if notifications are disabled.

    smtplib is synchronous — this used to block the whole event loop (all
    request handling, not just this job) for the duration of the SMTP
    connection. asyncio.to_thread runs it off-loop instead.
    """
    if not NOTIFY_EMAIL or not SMTP_HOST or not SMTP_USER:
        logger.debug("Email notifications not configured — skipping")
        return False

    return await asyncio.to_thread(_send_sync, subject, body)
