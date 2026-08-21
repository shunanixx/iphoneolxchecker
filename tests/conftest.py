import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

# Config is read at import time in some modules; make sure a token exists
# before anything imports bot.config.
os.environ.setdefault("BOT_TOKEN", "test:token")

from bot.config import Settings  # noqa: E402
from bot.db.engine import create_schema, dispose_engine, init_engine, session_scope  # noqa: E402


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        BOT_TOKEN="test:token",
        GEMINI_API_KEY="",
        DB_PATH=str(tmp_path / "test.db"),
    )


@pytest_asyncio.fixture
async def db(settings: Settings) -> AsyncIterator[None]:
    """A fresh SQLite file per test, with the real schema applied."""
    init_engine(settings)
    await create_schema()
    try:
        yield
    finally:
        await dispose_engine()


@pytest_asyncio.fixture
async def session(db) -> AsyncIterator:
    async with session_scope() as sess:
        yield sess
