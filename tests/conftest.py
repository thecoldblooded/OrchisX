import os
# Set test database URL and test proxy file before importing any database engine or settings
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test_temp.db"
os.environ["PROXY_FILE_PATH"] = "./test_temp_proxies.txt"

# Create mock test proxies file with 10 test entries
with open("./test_temp_proxies.txt", "w") as f:
    for i in range(1, 11):
        f.write(f"192.168.1.{i}:8080:testuser{i}:testpass{i}\n")

import asyncio
import pytest
from sqlmodel import SQLModel
from core.database import engine, init_db


@pytest.fixture(autouse=True, scope="session")
def setup_test_database():
    asyncio.run(init_db())
    yield
    # Cleanup temp db and proxy files after test session
    for temp_file in ["./test_temp.db", "./test_temp_proxies.txt"]:
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except Exception:
                pass
