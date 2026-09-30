"""Unit tests for services/rate_limit.py (audit remediation, Phase 5).

tests/conftest.py sets SAMAYA_DISABLE_RATE_LIMIT=1 so the rest of the
suite's real HTTP requests through the `client` fixture never trip it —
otherwise every test that legitimately POSTs to /api/tickets or votes a
few times across the whole pytest process would risk hitting the same
module-level in-memory counter. These tests exercise RateLimiter's actual
enforcement logic directly instead, bypassing that flag by constructing
their own instances and calling them with a fake Request — proving the
limiter itself works, independent of whether it's wired into the app.
"""
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from services.rate_limit import RateLimiter, _client_ip


def _fake_request(ip: str = "1.2.3.4", forwarded_for: str | None = None) -> MagicMock:
    req = MagicMock()
    req.headers = {"x-forwarded-for": forwarded_for} if forwarded_for else {}
    req.client.host = ip
    return req


class TestClientIp:

    def test_uses_client_host_when_no_forwarded_header(self):
        assert _client_ip(_fake_request(ip="10.0.0.5")) == "10.0.0.5"

    def test_prefers_x_forwarded_for_when_present(self):
        assert _client_ip(_fake_request(ip="10.0.0.5", forwarded_for="203.0.113.9")) == "203.0.113.9"

    def test_takes_first_hop_of_a_comma_separated_forwarded_chain(self):
        assert _client_ip(_fake_request(forwarded_for="203.0.113.9, 10.0.0.1")) == "203.0.113.9"

    def test_falls_back_to_unknown_when_no_client_at_all(self):
        req = MagicMock()
        req.headers = {}
        req.client = None
        assert _client_ip(req) == "unknown"


class TestRateLimiterEnforcement:

    def test_allows_requests_under_the_limit(self, monkeypatch):
        monkeypatch.setattr("services.rate_limit.DISABLED", False)
        limiter = RateLimiter("test-a", max_requests=3, window_seconds=60)
        req = _fake_request()
        limiter(req)
        limiter(req)
        limiter(req)  # exactly at the limit — must not raise yet

    def test_blocks_the_request_that_exceeds_the_limit(self, monkeypatch):
        monkeypatch.setattr("services.rate_limit.DISABLED", False)
        limiter = RateLimiter("test-b", max_requests=2, window_seconds=60)
        req = _fake_request()
        limiter(req)
        limiter(req)
        with pytest.raises(HTTPException) as excinfo:
            limiter(req)
        assert excinfo.value.status_code == 429

    def test_limits_are_independent_per_client_ip(self, monkeypatch):
        monkeypatch.setattr("services.rate_limit.DISABLED", False)
        limiter = RateLimiter("test-c", max_requests=1, window_seconds=60)
        limiter(_fake_request(ip="1.1.1.1"))
        # A different IP has its own untouched budget.
        limiter(_fake_request(ip="2.2.2.2"))
        with pytest.raises(HTTPException):
            limiter(_fake_request(ip="1.1.1.1"))

    def test_limits_are_independent_per_limiter_instance(self, monkeypatch):
        # Two call sites (e.g. create vs. vote) never share a budget, even
        # for the same client IP.
        monkeypatch.setattr("services.rate_limit.DISABLED", False)
        limiter_a = RateLimiter("test-d-a", max_requests=1, window_seconds=60)
        limiter_b = RateLimiter("test-d-b", max_requests=1, window_seconds=60)
        req = _fake_request()
        limiter_a(req)
        limiter_b(req)  # must not raise — separate instance, separate history

    def test_old_hits_expire_out_of_the_window(self, monkeypatch):
        monkeypatch.setattr("services.rate_limit.DISABLED", False)
        limiter = RateLimiter("test-e", max_requests=1, window_seconds=60)
        req = _fake_request()

        fake_now = [1000.0]
        monkeypatch.setattr("services.rate_limit.time.monotonic", lambda: fake_now[0])

        limiter(req)
        with pytest.raises(HTTPException):
            limiter(req)

        fake_now[0] += 61  # past the window
        limiter(req)  # must not raise — the earlier hit has expired

    def test_disabled_flag_bypasses_enforcement_entirely(self, monkeypatch):
        monkeypatch.setattr("services.rate_limit.DISABLED", True)
        limiter = RateLimiter("test-f", max_requests=1, window_seconds=60)
        req = _fake_request()
        for _ in range(10):
            limiter(req)  # must never raise while disabled
