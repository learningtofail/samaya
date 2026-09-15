"""Signed-cookie sessions — replaces the shared X-Admin-Key model for
anything a logged-in person does (see routers/auth.py for how a session
gets created).

Design choice worth being explicit about: SameSite=Strict on the cookie
is the CSRF defense here, not a separate CSRF-token scheme. A
Strict cookie is never sent on a cross-site request — including a
same-site link someone clicks from another tab — so a forged
cross-origin form/fetch can't ride the session at all. The tradeoff is
that following a link to Samaya from Discord itself won't carry the
cookie either; that's fine here, since the invite-claim flow
(routers/auth.py) never depended on an existing session in the first
place.
"""
import os

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

SESSION_COOKIE_NAME = "samaya_session"
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 30  # 30 days

# Falls back to a fixed dev-only value so tests and local runs don't need
# to set this — but a fixed fallback secret is a real problem in
# production, so main.py's startup check refuses to boot without a real
# SECRET_KEY set (see main.py), the same pattern ADMIN_API_KEY already
# used before Phase 4.
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-insecure-secret-key")

_serializer = URLSafeTimedSerializer(SECRET_KEY, salt="samaya-session")


def create_session_token(user_id: int) -> str:
    return _serializer.dumps({"user_id": user_id})


def read_session_token(token: str) -> int | None:
    """Returns the user_id, or None if the token is missing, malformed,
    tampered with, or expired. Never raises — every caller treats "no
    valid session" as a plain 401, not a 500."""
    if not token:
        return None
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    return data.get("user_id")


# ── OAuth-flow state cookies ─────────────────────────────────────
# Short-lived, separate from the session cookie itself. oauth_state guards
# the OAuth redirect round-trip against CSRF (Discord's callback must echo
# back the same value this app set before redirecting to Discord — see
# routers/auth.py). pending_invite carries an in-progress invite claim
# through that same round-trip, since Discord's callback has no other way
# to know which invite (if any) the person was trying to claim.
OAUTH_STATE_COOKIE = "samaya_oauth_state"
PENDING_INVITE_COOKIE = "samaya_pending_invite"
OAUTH_FLOW_MAX_AGE_SECONDS = 60 * 10  # 10 minutes — this cookie only needs to survive one redirect round-trip
