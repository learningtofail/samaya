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
async def client(db_engine):
    """
    HTTP test client wired to the FastAPI app with the test database.
    Overrides the get_db dependency so no real DB is touched.
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
        headers={"X-Admin-Key": TEST_ADMIN_KEY},
    ) as ac:
        yield ac

    app.dependency_overrides.clear()
