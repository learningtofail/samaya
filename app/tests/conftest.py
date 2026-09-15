"""
Test configuration and shared fixtures for Samaya.

Uses an in-memory SQLite database so tests never touch
the production PostgreSQL instance.
"""
import os

# main.py refuses to start without SECRET_KEY and Discord OAuth credentials
# set (see main.py's startup check) — set these before `main` is imported
# anywhere in the test session. The OAuth credentials are never actually
# used in tests (no test exercises the real Discord OAuth round-trip;
# routers/auth.py's flow is covered by unit-testing _claim_invite directly
# instead — see test_auth.py), they just need to exist so main.py boots.
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("DISCORD_OAUTH_CLIENT_ID", "test-client-id")
os.environ.setdefault("DISCORD_OAUTH_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("DISCORD_OAUTH_REDIRECT_URI", "http://test/auth/discord/callback")

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from models.db import Base
from models import get_db, get_session_factory
from services.sessions import SESSION_COOKIE_NAME, create_session_token

# In-memory SQLite — fast, isolated, no cleanup needed
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture(scope="function")
async def db_engine():
    """Create a fresh in-memory database for each test function."""
    engine = create_async_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def db_session(db_engine):
    """Provide an async session connected to the test database."""
    TestSessionLocal = async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with TestSessionLocal() as session:
        yield session


@pytest_asyncio.fixture(scope="function")
async def tenant(db_engine):
    """The default tenant almost every test operates in. Multi-tenant/
    kingdom-wide tests that need a second one use second_tenant below
    instead of re-seeding by hand."""
    from models.db import Kingdom, Tenant
    TestSessionLocal = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with TestSessionLocal() as session:
        kingdom = Kingdom(name="Kingdom 138", slug="k138")
        session.add(kingdom)
        await session.commit()
        t = Tenant(
            kingdom_id=kingdom.id, name="MOD", slug="mod",
            guild_id="test-guild-mod", bot_token="test-bot-token-mod", public_key="test-pubkey-mod",
        )
        session.add(t)
        await session.commit()
        return {"id": t.id, "slug": t.slug, "kingdom_id": kingdom.id}


@pytest_asyncio.fixture(scope="function")
async def second_tenant(db_engine, tenant):
    """A second tenant in the same Kingdom as `tenant` — for kingdom-wide
    fan-out and cross-tenant-isolation tests."""
    from models.db import Tenant
    TestSessionLocal = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with TestSessionLocal() as session:
        t = Tenant(
            kingdom_id=tenant["kingdom_id"], name="NSR", slug="nsr",
            guild_id="test-guild-nsr", bot_token="test-bot-token-nsr", public_key="test-pubkey-nsr",
        )
        session.add(t)
        await session.commit()
        return {"id": t.id, "slug": t.slug, "kingdom_id": tenant["kingdom_id"]}


@pytest_asyncio.fixture(scope="function")
async def test_user(db_engine):
    """The default logged-in user for `client` below — a superadmin, on
    purpose. Business-logic tests (event CRUD, posting, sync, ...) aren't
    also meant to be implicitly testing the permission model; that's
    covered on its own by test_auth_permissions.py using a deliberately
    unprivileged user instead of this one."""
    from models.db import User
    TestSessionLocal = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with TestSessionLocal() as session:
        u = User(discord_id="test-discord-id-superadmin", discord_username="TestSuperadmin", is_superadmin=True)
        session.add(u)
        await session.commit()
        return {"id": u.id, "discord_id": u.discord_id}


@pytest_asyncio.fixture(scope="function")
async def client(db_engine, tenant, test_user):
    """
    HTTP test client wired to the FastAPI app with the test database.
    Overrides the get_db dependency so no real DB is touched. Carries a
    real session cookie for test_user (a superadmin) and X-Tenant-Slug
    for `tenant` by default — tests that need a specific other tenant's
    context use client.headers["X-Tenant-Slug"] = ... to switch it.
    """
    from main import app

    TestSessionLocal = async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False
    )

    async def override_get_db():
        async with TestSessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = lambda: TestSessionLocal

    session_token = create_session_token(test_user["id"])
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"X-Tenant-Slug": tenant["slug"]},
        cookies={SESSION_COOKIE_NAME: session_token},
    ) as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest_asyncio.fixture(scope="function")
async def client_no_session(db_engine):
    """A client with no session cookie and no tenant header at all — for
    testing the login/callback/invite-claim routes themselves, which by
    definition run before any of that exists."""
    from main import app

    TestSessionLocal = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db():
        async with TestSessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_session_factory] = lambda: TestSessionLocal

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest_asyncio.fixture(scope="function")
async def make_user_and_client(db_engine):
    """Factory for a logged-in client as a *specific*, non-superadmin
    user with specific grants — for permission-boundary tests where
    `client`'s superadmin default would hide the thing being tested.

    Usage: client2 = await make_user_and_client(tenant_grants=[(tenant["id"], "coordinator")], kingdom_grants=[])
    """
    from main import app
    from models.db import User, UserKingdom, UserTenant

    TestSessionLocal = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async def _make(tenant_grants=None, kingdom_grants=None, discord_id="test-discord-id-other"):
        async with TestSessionLocal() as session:
            u = User(discord_id=discord_id, discord_username="OtherUser", is_superadmin=False)
            session.add(u)
            await session.commit()
            for tenant_id, role in (tenant_grants or []):
                session.add(UserTenant(user_id=u.id, tenant_id=tenant_id, role=role))
            for kingdom_id in (kingdom_grants or []):
                session.add(UserKingdom(user_id=u.id, kingdom_id=kingdom_id))
            await session.commit()
            user_id = u.id

        async def override_get_db():
            async with TestSessionLocal() as session:
                yield session
        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_session_factory] = lambda: TestSessionLocal

        session_token = create_session_token(user_id)
        client = AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
            cookies={SESSION_COOKIE_NAME: session_token},
        )
        return client, user_id

    yield _make
    app.dependency_overrides.clear()
