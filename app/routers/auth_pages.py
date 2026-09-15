"""The two error pages routers/auth.py's flow redirects to on failure —
split out because they're pure presentation (no OAuth logic, no DB
access), unlike everything else in routers/auth.py.
"""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/invite-invalid", response_class=HTMLResponse)
async def invite_invalid_page():
    return HTMLResponse(
        "<!DOCTYPE html><html><head><title>Invite invalid</title>"
        "<meta name='viewport' content='width=device-width, initial-scale=1.0'></head>"
        "<body style='font-family:sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem'>"
        "<h1>This invite link isn't valid</h1>"
        "<p>It may have expired, already been used, or been revoked. "
        "Ask whoever sent it to you for a new one.</p>"
        "</body></html>"
    )


@router.get("/auth/login-failed", response_class=HTMLResponse)
async def login_failed_page(reason: str = ""):
    messages = {
        "state_mismatch": "The login attempt didn't match what was expected — this can happen if the "
                           "link sat open too long, or if it was opened in a different browser than it started in.",
        "no_code":        "Discord didn't return a login code. Try again.",
        "token_exchange":  "Discord rejected the login exchange. Try again.",
        "identity_fetch":  "Couldn't confirm your Discord identity. Try again.",
    }
    detail = messages.get(reason, "Something went wrong during login.")
    return HTMLResponse(
        "<!DOCTYPE html><html><head><title>Login failed</title>"
        "<meta name='viewport' content='width=device-width, initial-scale=1.0'></head>"
        "<body style='font-family:sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem'>"
        f"<h1>Login didn't work</h1><p>{detail}</p>"
        "<p><a href='/auth/login'>Try again</a></p>"
        "</body></html>"
    )
