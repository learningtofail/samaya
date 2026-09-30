"""Unit tests for services/notifications.py — SMTP email notifications
(audit remediation, Phase 4). No test previously covered this module at
all. smtplib is synchronous, so send_notification() shells out to it via
asyncio.to_thread(); these tests monkeypatch smtplib.SMTP directly (the
same "monkeypatch the one blocking call, not the whole module" style
test_event_cover_image.py's TestDiscordApiImageField already uses for
httpx.AsyncClient.post) rather than mocking send_notification() itself,
so the actual message-building and error-handling logic in _send_sync()
is what's under test.
"""
import smtplib

import pytest

import services.notifications as notifications


@pytest.fixture(autouse=True)
def configured_smtp(monkeypatch):
    """Every test gets full SMTP config by default — the "not configured"
    tests below override individual settings back to empty explicitly,
    since send_notification() short-circuits (and never touches smtplib
    at all) unless NOTIFY_EMAIL/SMTP_HOST/SMTP_USER are all truthy."""
    monkeypatch.setattr(notifications, "NOTIFY_EMAIL", "ops@example.com")
    monkeypatch.setattr(notifications, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(notifications, "SMTP_PORT", 587)
    monkeypatch.setattr(notifications, "SMTP_USER", "bot@example.com")
    monkeypatch.setattr(notifications, "SMTP_PASS", "app-password")


class _FakeSMTP:
    """Stands in for smtplib.SMTP as a context manager — records what
    starttls/login/send_message were called with so tests can assert on
    the actual outgoing message, not just that "something" was sent."""
    instances = []

    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.starttls_called = False
        self.login_args = None
        self.sent_message = None
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def starttls(self):
        self.starttls_called = True

    def login(self, user, password):
        self.login_args = (user, password)

    def send_message(self, msg):
        self.sent_message = msg


@pytest.fixture(autouse=True)
def _reset_fake_smtp_instances():
    _FakeSMTP.instances = []
    yield
    _FakeSMTP.instances = []


class TestSendNotificationSuccess:

    async def test_sends_via_smtp_with_configured_credentials(self, monkeypatch):
        monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)

        result = await notifications.send_notification("Subject line", "Body text")

        assert result is True
        assert len(_FakeSMTP.instances) == 1
        smtp = _FakeSMTP.instances[0]
        assert smtp.host == "smtp.example.com"
        assert smtp.port == 587
        assert smtp.starttls_called is True
        assert smtp.login_args == ("bot@example.com", "app-password")
        assert smtp.sent_message["Subject"] == "Subject line"
        assert smtp.sent_message["From"] == "bot@example.com"
        assert smtp.sent_message["To"] == "ops@example.com"
        assert smtp.sent_message.get_payload() == "Body text"


class TestSendNotificationNotConfigured:
    """send_notification() must never attempt an SMTP connection at all
    when any of the three required settings is missing — a half-configured
    deployment (e.g. NOTIFY_EMAIL set but no SMTP_HOST yet) should degrade
    to a silent no-op, not a connection attempt against an empty host."""

    async def test_no_notify_email_skips_smtp_entirely(self, monkeypatch):
        monkeypatch.setattr(notifications, "NOTIFY_EMAIL", "")
        monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)

        result = await notifications.send_notification("Subject", "Body")

        assert result is False
        assert _FakeSMTP.instances == []

    async def test_no_smtp_host_skips_smtp_entirely(self, monkeypatch):
        monkeypatch.setattr(notifications, "SMTP_HOST", "")
        monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)

        result = await notifications.send_notification("Subject", "Body")

        assert result is False
        assert _FakeSMTP.instances == []

    async def test_no_smtp_user_skips_smtp_entirely(self, monkeypatch):
        monkeypatch.setattr(notifications, "SMTP_USER", "")
        monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)

        result = await notifications.send_notification("Subject", "Body")

        assert result is False
        assert _FakeSMTP.instances == []


class TestSendNotificationFailure:

    async def test_smtp_exception_returns_false_not_raise(self, monkeypatch):
        class _ExplodingSMTP(_FakeSMTP):
            def login(self, user, password):
                raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

        monkeypatch.setattr(smtplib, "SMTP", _ExplodingSMTP)

        result = await notifications.send_notification("Subject", "Body")

        assert result is False
