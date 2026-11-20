"""时间解析。

铁律：published_at（来源声称的发布时间）与 fetched_at（系统采集时间）必须分开。
缺失或解析失败一律返回 None，绝不用抓取时间伪装成发布时间。
"""

from __future__ import annotations

import calendar
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

_ONE_HOUR = timedelta(hours=1)


def parse_struct_time(struct: object) -> datetime | None:
    """把 feedparser 的 *_parsed（UTC struct_time）转成 aware datetime。"""

    if not struct:
        return None
    try:
        timestamp = calendar.timegm(struct)  # struct_time 已按 UTC 归一
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC)


def parse_any(value: str | None) -> datetime | None:
    """尽力解析 RFC 822 / ISO 8601，失败返回 None。"""

    if not value:
        return None
    text = value.strip()
    if not text:
        return None

    # RFC 822（RSS 常见）
    try:
        parsed = parsedate_to_datetime(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (TypeError, ValueError, IndexError):
        pass

    # ISO 8601（Atom 常见），兼容尾部 Z
    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except ValueError:
        return None


def is_implausible(published_at: datetime | None, fetched_at: datetime) -> bool:
    """发布时间晚于采集时间（且超过 1 小时容差）视为可疑，年份离谱也视为可疑。"""

    if published_at is None:
        return False
    if published_at - fetched_at > _ONE_HOUR:
        return True
    return not (1990 <= published_at.year <= fetched_at.year + 1)
