"""Tests for the invite-claim mechanics (routers/auth.py) and session
tokens (services/sessions.py). The full Discord OAuth round-trip itself
isn't exercised end-to-end here — that would mean either hitting the real
Discord API or monkeypatching it so thoroughly the test would mostly be
checking the mock — instead this monkeypatches exchange_code_for_token/
get_discord_identity (the two calls that actually leave the process) and
exercises the real callback route and the real _claim_invite logic
against them.
"""
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import Invite, User, UserKingdom, UserTenant
from routers.auth import _claim_invite
from services.sessions import create_session_token, read_session_token


class TestSessionTokens:

    def test_valid_token_round_trips(self):
        token = create_session_token(42)
        assert read_session_token(token) == 42

    def test_tampered_token_rejected(self):
        token = create_session_token(42)
        # Flip a character in the middle of the token, not the last one —
        # the last base64 character can sit at a byte boundary where two
        # adjacent alphabet characters decode to the same underlying bits,
        # making that specific tamper a no-op some fraction of the time.
        mid = len(token) // 2
        tampered = token[:mid] + ("a" if token[mid] != "a" else "b") + token[mid + 1:]
        assert read_session_token(tampered) is None

    def test_empty_token_rejected(self):
        assert read_session_token("") is None

    def test_garbage_token_rejected(self):
        assert read_session_token("not-a-real-token") is None


class TestClaimInvite:

    async def test_claiming_tenant_invite_creates_user_tenant_grant(
        self, db_session: AsyncSession, tenant: dict
    ):
        invite = Invite(
            token="test-token-1", tenant_id=tenant["id"], role="coordinator",
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        db_session.add(invite)
        await db_session.commit()

        user = User(discord_id="claimant-1", discord_username="Claimant")
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)

        await _claim_invite(db_session, "test-token-1", user)

        grant = (await db_session.execute(
            select(UserTenant).where(UserTenant.user_id == user.id, UserTenant.tenant_id == tenant["id"])
        )).scalar_one_or_none()
        assert grant is not None
        assert grant.role == "coordinator"

        await db_session.refresh(invite)
        assert invite.used_at is not None
        assert invite.used_by == user.id

    async def test_claiming_kingdom_invite_creates_user_kingdom_grant(
        self, db_session: AsyncSession, tenant: dict
    ):
        invite = Invite(
            token="test-token-2", kingdom_id=tenant["kingdom_id"], role="kingdom_coordinator",
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        db_session.add(invite)
        await db_session.commit()

        user = User(discord_id="claimant-2", discord_username="Claimant2")
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)

        await _claim_invite(db_session, "test-token-2", user)

        grant = (await db_session.execute(
            select(UserKingdom).where(UserKingdom.user_id == user.id, UserKingdom.kingdom_id == tenant["kingdom_id"])
        )).scalar_one_or_none()
        assert grant is not None

    async def test_expired_invite_not_claimed(self, db_session: AsyncSession, tenant: dict):
        invite = Invite(
            token="expired-token", tenant_id=tenant["id"], role="coordinator",
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        db_session.add(invite)
        await db_session.commit()

        user = User(discord_id="claimant-3", discord_username="Claimant3")
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)

        await _claim_invite(db_session, "expired-token", user)

        grant = (await db_session.execute(
            select(UserTenant).where(UserTenant.user_id == user.id)
        )).scalar_one_or_none()
        assert grant is None

    async def test_revoked_invite_not_claimed(self, db_session: AsyncSession, tenant: dict):
        invite = Invite(
            token="revoked-token", tenant_id=tenant["id"], role="coordinator",
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            revoked_at=datetime.now(timezone.utc),
        )
        db_session.add(invite)
        await db_session.commit()

        user = User(discord_id="claimant-4", discord_username="Claimant4")
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)

        await _claim_invite(db_session, "revoked-token", user)

        grant = (await db_session.execute(
            select(UserTenant).where(UserTenant.user_id == user.id)
        )).scalar_one_or_none()
        assert grant is None

    async def test_second_claim_of_same_invite_does_not_double_grant(
        self, db_session: AsyncSession, tenant: dict
    ):
        """The used_at IS NULL race guard: a second claim attempt on an
        already-used invite (simulating two near-simultaneous clicks)
        must not create a second grant or re-mark it used by someone else."""
        invite = Invite(
            token="race-token", tenant_id=tenant["id"], role="coordinator",
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        db_session.add(invite)
        await db_session.commit()

        first_user = User(discord_id="racer-1", discord_username="Racer1")
        second_user = User(discord_id="racer-2", discord_username="Racer2")
        db_session.add_all([first_user, second_user])
        await db_session.commit()
        await db_session.refresh(first_user)
        await db_session.refresh(second_user)

        await _claim_invite(db_session, "race-token", first_user)
        await _claim_invite(db_session, "race-token", second_user)  # should be a no-op

        grants = (await db_session.execute(select(UserTenant))).scalars().all()
        assert len(grants) == 1
        assert grants[0].user_id == first_user.id

        await db_session.refresh(invite)
        assert invite.used_by == first_user.id


class TestDiscordCallback:

    async def test_state_mismatch_rejected(self, client_no_session: AsyncClient):
        client_no_session.cookies.set("samaya_oauth_state", "expected")
        r = await client_no_session.get(
            "/auth/discord/callback?code=fake&state=wrong",
            follow_redirects=False,
        )
        assert r.status_code in (302, 303, 307)
        assert "login-failed" in r.headers["location"]

    async def test_successful_login_creates_session_cookie(
        self, client_no_session: AsyncClient, monkeypatch
    ):
        async def fake_exchange(code):
            return "fake-access-token", ""
        async def fake_identity(access_token):
            return {"id": "new-discord-user-1", "username": "NewUser"}, ""
        monkeypatch.setattr("routers.auth.exchange_code_for_token", fake_exchange)
        monkeypatch.setattr("routers.auth.get_discord_identity", fake_identity)

        client_no_session.cookies.set("samaya_oauth_state", "matching")
        r = await client_no_session.get("/auth/discord/callback?code=fake&state=matching")
        assert "samaya_session" in r.cookies

    async def test_full_invite_claim_round_trip_grants_access(
        self, client_no_session: AsyncClient, db_session, tenant: dict, monkeypatch
    ):
        """The real HTTP flow, not just _claim_invite in isolation: hit
        /invite/{token} (sets the pending-invite cookie, redirects to
        Discord), then the callback (as Discord would call it back),
        and confirm the grant actually exists afterward — and that the
        resulting session actually has access to the tenant."""
        invite = Invite(
            token="full-flow-token", tenant_id=tenant["id"], role="owner",
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        db_session.add(invite)
        await db_session.commit()

        r = await client_no_session.get("/invite/full-flow-token", follow_redirects=False)
        assert r.status_code in (302, 303, 307)
        assert "discord.com" in r.headers["location"]
        oauth_state = r.cookies.get("samaya_oauth_state")
        pending_invite = r.cookies.get("samaya_pending_invite")
        assert pending_invite == "full-flow-token"

        async def fake_exchange(code):
            return "fake-access-token", ""
        async def fake_identity(access_token):
            return {"id": "new-owner-discord-id", "username": "NewOwner"}, ""
        monkeypatch.setattr("routers.auth.exchange_code_for_token", fake_exchange)
        monkeypatch.setattr("routers.auth.get_discord_identity", fake_identity)

        client_no_session.cookies.set("samaya_oauth_state", oauth_state)
        client_no_session.cookies.set("samaya_pending_invite", pending_invite)
        callback = await client_no_session.get(
            f"/auth/discord/callback?code=real&state={oauth_state}", follow_redirects=False
        )
        assert "samaya_session" in callback.cookies

        # This same client, now with a real session, should have access
        # to the tenant it just claimed owner access to.
        client_no_session.cookies.set("samaya_session", callback.cookies["samaya_session"])
        r = await client_no_session.get("/admin/api/events", headers={"X-Tenant-Slug": tenant["slug"]})
        assert r.status_code == 200, r.text

        me = await client_no_session.get("/admin/api/me", headers={"X-Tenant-Slug": tenant["slug"]})
        assert me.json()["tenant_roles"].get(str(tenant["id"])) == "owner" or \
               me.json()["tenant_roles"].get(tenant["id"]) == "owner"
