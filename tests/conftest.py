import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import Base, SessionLocal, engine
from app.settings import get_settings


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if os.getenv("RUN_DB_INTEGRATION_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="set RUN_DB_INTEGRATION_TESTS=1 with a dedicated *_test PostgreSQL database")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


async def _truncate_test_database() -> None:
    table_names = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))


@pytest_asyncio.fixture
async def db_session(tmp_path) -> AsyncIterator[AsyncSession]:
    if os.getenv("RUN_DB_INTEGRATION_TESTS") != "1":
        pytest.skip("database integration tests are disabled")

    settings = get_settings()
    database_name = make_url(settings.database_url).database or ""
    if not database_name.endswith("_test"):
        raise RuntimeError("refusing to clean a database whose name does not end with _test")

    original_runtime_dir = settings.runtime_dir
    settings.runtime_dir = tmp_path / "runtime"
    settings.ensure_runtime()
    await _truncate_test_database()
    try:
        async with SessionLocal() as session:
            yield session
    finally:
        await _truncate_test_database()
        await engine.dispose()
        settings.runtime_dir = original_runtime_dir
