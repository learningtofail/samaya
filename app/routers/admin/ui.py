"""Serves the admin single-page app shell.

This router has no auth dependency, unlike every other router in this
package — deliberately: the HTML shell itself carries no data, and
gating it would just mean an extra round trip before the page can even
show the login link. Every /api/* call the page then makes goes through
the other routers' get_current_user/get_current_tenant dependency chain
(see routers/admin/deps.py); Cloudflare Access at the network edge remains
the first gate in front of all of this (see README).
"""
import re

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

# Cloudflare (in front of this deployment) caches /static/*.js and
# /static/*.css at the edge by file extension for hours (see its default
# cache-control on this zone), independent of how fresh the origin's copy
# is — a plain redeploy of a JS file is not enough to bust it, and even a
# client-side `cache: "no-store"` fetch still hits the stale edge copy.
# Bump this whenever any file under /static changes, so every reference
# gets a new query string and Cloudflare (and browsers) treat it as a
# fresh URL instead of serving a stale cached one. Track this independently
# of the spec/API version — it only needs to move when a static asset
# actually changes.
STATIC_ASSET_VERSION = "1.20.0"

_STATIC_ASSET_REF = re.compile(r'(src|href)="(/static/[^"?]+)"')


def _bust_static_cache(html: str) -> str:
    return _STATIC_ASSET_REF.sub(
        lambda m: f'{m.group(1)}="{m.group(2)}?v={STATIC_ASSET_VERSION}"', html
    )


@router.get("/", response_class=HTMLResponse)
async def admin_home():
    with open("/app/static/admin.html") as f:
        return HTMLResponse(_bust_static_cache(f.read()))
