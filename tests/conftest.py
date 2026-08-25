import os
# Set test database URL before importing any database engine or settings
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_temp.db"

import asyncio
import pytest
from sqlmodel import SQLModel
from core.database import engine, init_db


@pytest.fixture(autouse=True, scope="session")
def setup_test_database():
    asyncio.run(init_db())
    yield
    # Cleanup temp db file after test session
    if os.path.exists("./test_temp.db"):
        try:
            os.remove("./test_temp.db")
        except Exception:
            pass
