import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

# 앱 설정이 import되기 전에 테스트 DB를 지정한다. 개발 DB를 지우지 않도록 _test DB만 허용한다.
os.environ.setdefault("PAO_DATABASE_URL", "postgresql+asyncpg://pao:pao@127.0.0.1:5433/pao_test")
TEST_DATABASE_URL = os.environ["PAO_DATABASE_URL"]
assert TEST_DATABASE_URL.rsplit("/", 1)[-1].endswith("_test"), "tests must run on a *_test database"

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.main import create_app  # noqa: E402
from app.persistence.db import get_engine  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parents[1]


def alembic_config() -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    return config


async def _reset_schema() -> None:
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    # alembic env.py가 asyncio.run을 쓰므로 이벤트 루프가 돌기 전에 sync fixture에서 실행한다.
    asyncio.run(_reset_schema())
    command.upgrade(alembic_config(), "head")


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """테스트마다 바깥 트랜잭션을 열고 끝나면 롤백해 데이터를 남기지 않는다."""
    async with get_engine().connect() as conn:
        trans = await conn.begin()
        async with AsyncSession(bind=conn, join_transaction_mode="create_savepoint") as s:
            yield s
        await trans.rollback()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
