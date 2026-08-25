from contextlib import asynccontextmanager
import os
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlmodel import SQLModel
from config import settings


def get_db_url() -> str:
    return os.getenv("DATABASE_URL", settings.DATABASE_URL)


engine = create_async_engine(
    get_db_url(),
    echo=False,
    future=True,
    connect_args={"check_same_thread": False} if "sqlite" in get_db_url() else {}
)

async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False
)


async def init_db() -> None:
    """Initialize database tables."""
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency for database sessions."""
    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.close()


@asynccontextmanager
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Async context manager for background workers, CLI, and internal services."""
    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.close()
