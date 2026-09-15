"""
Test configuration and shared fixtures for Samaya.

Uses an in-memory SQLite database so tests never touch
the production PostgreSQL instance.
"""
import os

# main.py refuses to start without ADMIN_API_KEY set (see main.py's startup
# check), and admin routes now require it as the X-Admin-Key header — set
# this before `main` is imported anywhere in the test session.
os.environ.setdefault("ADMIN_API_KEY", "test-admin-key")
TEST_ADMIN_KEY = os.environ["ADMIN_API_KEY"]

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from models.db import Base
from models import get_db

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
async def client(db_engine, tenant):
    """
    HTTP test client wired to the FastAPI app with the test database.
    Overrides the get_db dependency so no real DB is touched. Carries
    X-Tenant-Slug for `tenant` by default — tests that need a specific
    other tenant's context use client.headers["X-Tenant-Slug"] = ... to
    switch it, rather than a separate fixture per tenant.
    """
    from main import app

    TestSessionLocal = async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False
    )

    async def override_get_db():
        async with TestSessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"X-Admin-Key": TEST_ADMIN_KEY, "X-Tenant-Slug": tenant["slug"]},
    ) as ac:
        yield ac

    app.dependency_overrides.clear()
