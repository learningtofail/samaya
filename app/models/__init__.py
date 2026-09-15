from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from os import environ

DATABASE_URL = environ.get("DATABASE_URL", "postgresql+asyncpg://taraka:password@db:5432/kingshot_scheduler")

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session

def get_session_factory():
    """For the rare route that calls a scheduler/*.py job function
    directly (e.g. the manual "regenerate" button) rather than just
    reading/writing through its own `db: AsyncSession = Depends(get_db)`.
    Those job functions open their own sessions via an injectable
    session_factory (see scheduler/regeneration.py's module docstring)
    rather than accepting an existing session — calling them from inside
    a request needs this dependency so tests can override it the same
    way they override get_db, instead of the job silently falling back
    to the real AsyncSessionLocal regardless of what get_db is overridden
    to. A plain function, not async — it just hands back the
    sessionmaker itself, not a session."""
    return AsyncSessionLocal
