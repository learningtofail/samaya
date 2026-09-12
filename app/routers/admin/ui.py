"""Serves the admin single-page app shell.

This router has no require_admin_key dependency, unlike every other
router in this package — deliberately: the HTML shell itself carries no
data, and gating it would just mean an extra round trip before the page
can even show the key prompt. Every /api/* call the page then makes goes
through the other routers' require_admin_key dependency; Cloudflare
Access at the network edge is the first gate in front of all of this
(see services/auth.py).
"""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def admin_home():
    with open("/app/static/admin.html") as f:
        return HTMLResponse(f.read())
