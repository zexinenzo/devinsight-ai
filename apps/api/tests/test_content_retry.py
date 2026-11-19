"""失败重试策略的单测。

这两条规则的错误都是静默的：判宽松了，55 条 `HTTP 403` 反爬条目会被每 6 小时
重打一遍（正是当初决定「不自动重试」的原因）；判严格了，本机代理恢复之后
那 136 条 502 永远不会回到候选队列。没有异常、没有日志告警，只有覆盖率
停在原地——所以边界值要逐条钉住。
"""

from app.db.models import Document
from app.services.content_service import (
    MAX_FETCH_ATTEMPTS,
    RETRY_BACKOFF_HOURS,
    PageResult,
    _is_retryable_error,
    _next_attempts,
    apply_result,
)


def test_transient_errors_are_retryable() -> None:
    """网络层与 5xx：换个时间大概率能通。"""

    for error in (
        "ProxyError: 502 Bad Gateway",
        "ConnectError: [Errno 111] Connection refused",
        "ReadTimeout: ",
        "HTTP 500",
        "HTTP 503",
        "HTTP 429 限流",
    ):
        assert _is_retryable_error(error) is True, error


def test_deterministic_errors_are_not_retryable() -> None:
    """对方明确拒绝或内容类型不对：再敲一次门得到的还是同一句话。"""

    for error in (
        "HTTP 401",
        "HTTP 403",
        "HTTP 404",
        "HTTP 405",
        "HTTP 410",
        "非 HTML 内容（application/pdf）",
    ):
        assert _is_retryable_error(error) is False, error


def test_missing_reason_is_not_retryable() -> None:
    """失败却没有原因，说明不了是不是瞬时问题，不拿预算去赌。"""

    assert _is_retryable_error("") is False


def test_attempts_accumulate_then_saturate() -> None:
    document = Document(content_attempts=0)

    assert _next_attempts(document, "ProxyError: 502") == 1
    document.content_attempts = 1
    assert _next_attempts(document, "ReadTimeout: ") == 2
    document.content_attempts = 2
    # 到顶之后不再增长：数字本身就是「预算用完」的信号
    assert _next_attempts(document, "ConnectError: refused") == MAX_FETCH_ATTEMPTS


def test_terminal_error_burns_whole_budget() -> None:
    """确定性失败不等退避，直接宣告预算耗尽。"""

    document = Document(content_attempts=0)
    assert _next_attempts(document, "HTTP 403") == MAX_FETCH_ATTEMPTS


def test_apply_result_records_attempt_and_time() -> None:
    """退避判断依赖 content_fetched_at，所以失败也必须刷新这个时间。"""

    document = Document(content_attempts=0, content_status="pending")
    apply_result(document, PageResult(status="failed", error="HTTP 500"))

    assert document.content_status == "failed"
    assert document.content_attempts == 1
    assert document.content_fetched_at is not None
    assert document.content_error == "HTTP 500"


def test_backoff_is_monotonic() -> None:
    """退避必须递增：恒定间隔等于把「域名级封锁」当成网络抖动反复重试。"""

    assert RETRY_BACKOFF_HOURS == tuple(sorted(RETRY_BACKOFF_HOURS))
    assert len(RETRY_BACKOFF_HOURS) == MAX_FETCH_ATTEMPTS


def test_enrich_summary_has_all_counters_the_cli_reads():
    """enrich 的返回值被 CLI 按直接下标读取。

    「没有候选」是一条独立的提前返回路径，它曾经少了向量索引那几个键，
    结果是：候选为空时 CLI 抛 KeyError——恰恰在最不该崩的分支上。
    现在两条路径共用 `_enrich_summary()`，这条测试守着键不被删掉。
    """

    from app.services.content_service import _enrich_summary

    summary = _enrich_summary()
    for key in (
        "selected",
        "retried",
        "fetched",
        "empty",
        "failed",
        "summary_gained",
        "summary_repaired",
        "self_posts_marked",
        "indexed_documents",
        "indexed_chunks",
        "index_error",
        "duration_ms",
    ):
        assert key in summary, f"缺少计数键 {key}，CLI 会 KeyError"
