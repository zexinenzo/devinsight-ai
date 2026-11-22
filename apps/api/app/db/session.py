"""异步数据库会话管理。

API 进程与 Worker 进程各自持有连接池；不要跨进程复用 Session。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    echo=False,
)

SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖注入用的会话。"""

    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """脚本/任务用的显式事务边界。"""

    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    await engine.dispose()


def database_hint(url: str, exc: BaseException) -> str:
    """把「连不上库」翻译成能照着做的一句话。纯函数，可以脱离数据库断言。

    **为什么要专门写这一句**：库没起来时，Python 抛的是
    `ConnectionRefusedError`，但它在 120 行 SQLAlchemy + greenlet 调用栈的最底部——
    终端一滚就没了，留下的是满屏 `_connection_for_bind` / `_checkout`。真正的信息
    「5432 没人监听」被埋在里面，而读到的人只看到「脚本崩了」。

    同一个病这个项目返工过一次：闭合日早于起始日时，判据要在**发语句之前**拒，
    否则报错离原因太远。知道该做什么的时候，不要让读的人自己去翻栈。

    注意只取 host/port，不打印整条 URL——那里有密码。
    """

    target = "数据库"
    try:
        parsed = make_url(url)
    except Exception:  # noqa: BLE001 —— URL 本身就配坏了，这时更要给人话
        parsed = None
    if parsed is not None and parsed.host:
        target = f"数据库 {parsed.host}:{parsed.port or 5432}"

    return (
        f"连不上{target}。先别改代码，按顺序看：\n"
        "  docker compose up -d db     # 容器没起。注意 wsl --shutdown 会带走 Docker 引擎，"
        "要重新打开 Docker Desktop 再等它显示 running\n"
        "  docker compose ps           # 状态要是 Up (healthy)\n"
        f"原始错误：{type(exc).__name__}: {exc}"
    )


async def check_database(timeout: float = 3.0) -> str | None:
    """起手探一下库：连得上返回 None，连不上返回一句能照着做的话。

    脚本的入口先调它，是为了让「Docker 没起」这种事在**第一行**就说完，
    而不是等某次 `session.execute` 抛出来。超时压到 3 秒：连不上时本地端口
    是立刻拒绝的，只有网络分区才会等，而那种情况也不该让脚本挂一分钟。
    """

    try:
        async with asyncio.timeout(timeout):
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 —— 任何连接失败都该翻译成人话，不是中断脚本
        return database_hint(settings.database_url, exc)
    return None
