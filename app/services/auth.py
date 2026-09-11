"""
Application-level admin auth.

Cloudflare Access is the primary gate on /admin in production (see README),
but the app previously had no auth of its own: anything that could reach
the container on the network — another container, a misconfigured tunnel,
a future deployment without Access in front of it — got full admin control.

This adds a second, independent layer: a shared-secret header checked on
every /admin/api/* call. It does not replace Access; it means the app is
still safe if Access is ever bypassed or misconfigured.

ADMIN_API_KEY must be set. The app refuses to start without it (see
main.py's startup check) rather than silently running unauthenticated.
"""
import hmac
import os

from fastapi import Header, HTTPException

ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")


def require_admin_key(x_admin_key: str = Header(default="")):
    if not ADMIN_API_KEY:
        # Should be unreachable in practice — main.py's startup check stops
        # the app from booting without ADMIN_API_KEY set. Fail closed here
        # too, in case this dependency is ever reused elsewhere.
        raise HTTPException(status_code=503, detail="Admin auth not configured")
    if not x_admin_key or not hmac.compare_digest(x_admin_key, ADMIN_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing admin key")
