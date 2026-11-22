"""脚本入口的数据库预检：把「连不上」翻译成能照着做的一句话。

这一条来自一次实跑：`docker compose up -d db` 报「找不到 dockerDesktopLinuxEngine」，
随后两个脚本都以 120 行 SQLAlchemy + greenlet 调用栈结束，栈底那行
`ConnectionRefusedError: … ('127.0.0.1', 5432)` 被终端一滚就看不见了。

和闭合日那条判据是同一个道理：**知道该做什么的时候，不要让读的人自己去翻栈**。

这里只测纯函数 `database_hint`。`check_database` 要真的连库，测试里连不上任何东西，
把它一起测等于测试环境本身。
"""

from __future__ import annotations

from app.db.session import database_hint

URL = "postgresql+asyncpg://devinsight:sup3r_secret@localhost:5432/devinsight"


def refusal() -> ConnectionRefusedError:
    return ConnectionRefusedError(111, "Connect call failed ('127.0.0.1', 5432)")


def test_hint_names_the_target() -> None:
    """先说要连哪，才知道该去起哪个容器。"""
    message = database_hint(URL, refusal())
    assert "localhost:5432" in message


def test_hint_says_what_to_do() -> None:
    """两行命令，顺序就是排查顺序：起容器 → 看状态。"""
    message = database_hint(URL, refusal())
    assert "docker compose up -d db" in message
    assert "docker compose ps" in message
    assert "healthy" in message


def test_hint_explains_the_wsl_shutdown_link() -> None:
    """`wsl --shutdown` 会带走 Docker 引擎——这是这台机器上真实发生过的那次。

    不写这一句，人只会想「我昨天还能用」。
    """
    message = database_hint(URL, refusal())
    assert "wsl --shutdown" in message
    assert "Docker Desktop" in message


def test_hint_never_prints_the_password() -> None:
    """URL 里有密码。只取 host/port，不要为了「信息完整」把整条串打出来——
    日志、截图、issue 里的连接串是会流传的。"""
    message = database_hint(URL, refusal())
    assert "sup3r_secret" not in message


def test_hint_keeps_the_original_error() -> None:
    """翻译不等于吞掉原文。排查时「Errno 111」和「密码错」的下一步完全不同，
    所以原始类型与消息必须留在最后一行。"""
    message = database_hint(URL, refusal())
    assert "ConnectionRefusedError" in message
    assert "Errno" in message or "111" in message


def test_hint_survives_a_broken_url() -> None:
    """配置本身就配坏时（缺 `://`、协议写错），这里不能再抛一次——
    那会把「配置错了」变成「预检崩了」。"""
    message = database_hint("这不是一条 URL", refusal())
    assert "连不上" in message
    assert "docker compose up -d db" in message
