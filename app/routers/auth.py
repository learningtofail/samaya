"""Login, invite-claim, and logout. Public routes — the only way to reach
Discord OAuth at all is through here, and the only way OAuth ever grants
real access is by consuming a pending Invite (see services/sessions.py's
docstring for the CSRF design, and models.db.Invite for why OAuth alone
never creates access).

The two error pages this flow redirects to on failure live in
routers/auth_pages.py, not here — pure presentation, no OAuth logic.
"""
import os
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import Invite, User, UserKingdom, UserTenant
from services.audit import log_change
from services.discord_oauth import build_authorize_url, exchange_code_for_token, get_discord_identity
from services.sessions import (
    OAUTH_FLOW_MAX_AGE_SECONDS, OAUTH_STATE_COOKIE, PENDING_INVITE_COOKIE,
    SESSION_COOKIE_NAME, SESSION_MAX_AGE_SECONDS, create_session_token,
)
from services.time_utils import ensure_utc

router = APIRouter()

SUPERADMIN_DISCORD_IDS = set(
    d.strip() for d in os.environ.get("SUPERADMIN_DISCORD_IDS", "").split(",") if d.strip()
)


def _redirect_to_discord(state: str) -> RedirectResponse:
    resp = RedirectResponse(url=build_authorize_url(state))
    resp.set_cookie(
        OAUTH_STATE_COOKIE, state, max_age=OAUTH_FLOW_MAX_AGE_SECONDS,
        httponly=True, secure=True, samesite="lax",
        # samesite=lax (not strict) specifically here, unlike the session
        # cookie itself — Discord's own redirect back to /auth/discord/callback
        # is a top-level cross-site navigation, and Strict would drop this
        # cookie before the callback ever saw it. Lax still blocks the
        # cases that matter (forged cross-site POSTs, embedded requests).
    )
    return resp


@router.get("/auth/login")
async def login():
    state = secrets.token_urlsafe(24)
    return _redirect_to_discord(state)


@router.get("/invite/{token}")
async def claim_invite_start(token: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Invite).where(Invite.token == token))
    invite = result.scalar_one_or_none()

    now = datetime.now(timezone.utc)
    if not invite or invite.used_at or invite.revoked_at or ensure_utc(invite.expires_at) < now:
        return RedirectResponse(url="/invite-invalid")

    state = secrets.token_urlsafe(24)
    resp = _redirect_to_discord(state)
    resp.set_cookie(
        PENDING_INVITE_COOKIE, token, max_age=OAUTH_FLOW_MAX_AGE_SECONDS,
        httponly=True, secure=True, samesite="lax",
    )
    return resp


@router.get("/auth/discord/callback")
async def discord_callback(request: Request, code: str = "", state: str = "", db: AsyncSession = Depends(get_db)):
    expected_state = request.cookies.get(OAUTH_STATE_COOKIE, "")
    if not state or not expected_state or state != expected_state:
        return RedirectResponse(url="/auth/login-failed?reason=state_mismatch")

    if not code:
        return RedirectResponse(url="/auth/login-failed?reason=no_code")

    access_token, error = await exchange_code_for_token(code)
    if error:
        return RedirectResponse(url="/auth/login-failed?reason=token_exchange")

    identity, error = await get_discord_identity(access_token)
    if error or not identity.get("id"):
        return RedirectResponse(url="/auth/login-failed?reason=identity_fetch")

    discord_id = identity["id"]
    username   = identity["username"]

    result = await db.execute(select(User).where(User.discord_id == discord_id))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(discord_id=discord_id, discord_username=username)
        db.add(user)
        await db.flush()
    else:
        user.discord_username = username  # keep display name current

    user.last_login_at = datetime.now(timezone.utc)
    user.is_superadmin = discord_id in SUPERADMIN_DISCORD_IDS or user.is_superadmin
    await db.commit()
    await db.refresh(user)

    pending_invite_token = request.cookies.get(PENDING_INVITE_COOKIE, "")
    if pending_invite_token:
        await _claim_invite(db, pending_invite_token, user)

    session_token = create_session_token(user.id)
    resp = RedirectResponse(url="/admin")
    resp.set_cookie(
        SESSION_COOKIE_NAME, session_token, max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True, secure=True, samesite="strict",
    )
    resp.delete_cookie(OAUTH_STATE_COOKIE)
    resp.delete_cookie(PENDING_INVITE_COOKIE)
    return resp


async def _claim_invite(db: AsyncSession, token: str, user: User):
    """Atomically marks the invite used and creates the grant it promised.
    The UPDATE ... WHERE used_at IS NULL is the actual race guard — if two
    people click the same link near-simultaneously, exactly one UPDATE
    affects a row; the loser's rowcount is 0 and it silently does nothing
    rather than double-granting."""
    now = datetime.now(timezone.utc)
    result = await db.execute(select(Invite).where(Invite.token == token))
    invite = result.scalar_one_or_none()
    if not invite or invite.revoked_at or ensure_utc(invite.expires_at) < now:
        return

    claim_result = await db.execute(
        update(Invite)
        .where(Invite.id == invite.id, Invite.used_at.is_(None))
        .values(used_at=now, used_by=user.id)
    )
    if claim_result.rowcount != 1:
        await db.commit()
        return  # already claimed by someone else — the race guard fired

    if invite.role == "kingdom_coordinator":
        existing = await db.execute(
            select(UserKingdom).where(UserKingdom.user_id == user.id, UserKingdom.kingdom_id == invite.kingdom_id)
        )
        if not existing.scalar_one_or_none():
            grant = UserKingdom(user_id=user.id, kingdom_id=invite.kingdom_id)
            db.add(grant)
            await db.flush()
            await log_change(
                db, user_id=invite.created_by, tenant_id=None,
                table_name="user_kingdoms", row_id=grant.id, action="create",
                after={"user_id": user.id, "kingdom_id": invite.kingdom_id, "granted_via_invite": invite.id},
            )
    else:
        existing = await db.execute(
            select(UserTenant).where(UserTenant.user_id == user.id, UserTenant.tenant_id == invite.tenant_id)
        )
        row = existing.scalar_one_or_none()
        if row:
            row.role = invite.role  # re-claiming a second invite for the same tenant just updates the role
        else:
            grant = UserTenant(user_id=user.id, tenant_id=invite.tenant_id, role=invite.role)
            db.add(grant)
            await db.flush()
            await log_change(
                db, user_id=invite.created_by, tenant_id=invite.tenant_id,
                table_name="user_tenants", row_id=grant.id, action="create",
                after={"user_id": user.id, "tenant_id": invite.tenant_id, "role": invite.role, "granted_via_invite": invite.id},
            )

    await db.commit()


@router.post("/auth/logout")
async def logout():
    resp = RedirectResponse(url="/admin", status_code=303)
    resp.delete_cookie(SESSION_COOKIE_NAME)
    return resp
