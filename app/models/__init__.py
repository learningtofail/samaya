from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from os import environ

DATABASE_URL = environ.get("DATABASE_URL", "postgresql+asyncpg://taraka:password@db:5432/kingshot_scheduler")

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
