"""Serves the admin single-page app shell.

This router has no auth dependency, unlike every other router in this
package — deliberately: the HTML shell itself carries no data, and
gating it would just mean an extra round trip before the page can even
show the login link. Every /api/* call the page then makes goes through
the other routers' get_current_user/get_current_tenant dependency chain
(see routers/admin/deps.py); Cloudflare Access at the network edge remains
the first gate in front of all of this (see README).
"""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def admin_home():
    with open("/app/static/admin.html") as f:
        return HTMLResponse(f.read())
