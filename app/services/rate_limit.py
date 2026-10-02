"""In-process rate limiting for the public, unauthenticated ticket-board
endpoints (spec §40-43) — audit remediation, Phase 5. Before this, a
script could hammer POST /api/tickets or the anonymous vote endpoint with
no limit at all.

A plain sliding-window counter keyed by client IP, held in memory — no new
runtime dependency (no slowapi/Redis) for what's a low-traffic community
feedback board pre-beta. This is safe specifically because Samaya runs a
single uvicorn worker by design (see CLAUDE.md's own note on why —
APScheduler's jobs would otherwise double-run): a second worker would give
each process its own independent counter, at which point a shared store
would be needed instead. If this app is ever scaled to multiple workers,
this module needs to move to something like Redis at the same time.
"""
import time
from collections import defaultdict, deque
from os import environ

from fastapi import HTTPException, Request

# Set by tests/conftest.py before main.py (and this module, transitively)
# is imported, the same way it already sets SECRET_KEY/DISCORD_OAUTH_*.
# Without this, in-memory hit counts would accumulate across every test in
# the same pytest process (this module is only ever imported once), and a
# generous-but-finite limit could still eventually trip across the whole
# suite. tests/test_rate_limit.py flips this back on to test the real
# enforcement logic directly, bypassing the app entirely.
DISABLED = environ.get("SAMAYA_DISABLE_RATE_LIMIT") == "1"


def _client_ip(request: Request) -> str:
    # This deployment is only ever reached through Cloudflare Tunnel +
    # Caddy in front of it (see README's stack/deploy notes) — there is no
    # path to this app that doesn't go through that proxy, so trusting its
    # X-Forwarded-For is safe here (a self-hosted single-tenant-network
    # deployment, not a shared/multi-tenant-hosting situation where a
    # client could set this header directly and spoof another IP).
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class RateLimiter:
    """A FastAPI dependency: `_: None = Depends(some_rate_limiter)`. Each
    instance is its own independent limit with its own hit history — give
    each call site its own instance (see tickets_public.py) rather than
    sharing one, so a burst against one endpoint never eats into another's
    budget. Also directly callable/instantiable from a test without going
    through the app, for tests/test_rate_limit.py's own use.
    """

    def __init__(self, name: str, max_requests: int, window_seconds: int):
        self.name = name
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        """Records one hit for `key` and returns False when it is over the
        limit. Also used with a Discord user ID, where the per-IP key cannot
        work (every interaction comes from Discord's own addresses)."""
        if DISABLED:
            return True
        hits = self._hits[key]
        now = time.monotonic()
        while hits and hits[0] <= now - self.window_seconds:
            hits.popleft()
        if len(hits) >= self.max_requests:
            return False
        hits.append(now)
        return True

    def __call__(self, request: Request) -> None:
        if not self.allow(_client_ip(request)):
            raise HTTPException(
                status_code=429,
                detail="Too many requests — please slow down and try again in a few minutes.",
            )
