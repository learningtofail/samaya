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


def send_notification(subject: str, body: str) -> bool:
    """
    Sends an email notification if NOTIFY_EMAIL and SMTP credentials are configured.
    Returns True on success, False on failure or if notifications are disabled.
    """
    if not NOTIFY_EMAIL or not SMTP_HOST or not SMTP_USER:
        logger.debug("Email notifications not configured — skipping")
        return False

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
