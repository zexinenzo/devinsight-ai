"""重排层的测试：passage 构造、分数错位保护、只对看过的候选投票。

这几条都是「改一行就悄悄失效」的地方，而且失效时不报错：

1. **passage 少一段就掉一类查询。** 只用标题+摘要，fact 类 R@5 从 1.000 掉到
   0.923（答案那个数字在正文里，摘要没有）；只用标题+命中片段，topic 类从
   0.625 掉到 0.507（片段是「某一处提到」，丢了整体主题）。
2. **分数条数和候选数不一致时必须放弃重排。** 按顺序贴上去不会报错，
   但分数会落到错的文档上，排序看起来完全正常、实际上全错。
3. **只对重排过的候选投票。** 如果把后面没重排的 260 条一并带进 RRF，
   它们的顺序就是原顺序，等于给原顺序又投一票，把重排的效果稀释掉。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence

from app.services import document_service, rerank_service
from app.services.document_service import _rerank_ordered, _rerank_passage
from app.services.vector_index_service import build_header_text


class FakeResult:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def all(self) -> list[tuple]:
        return self._rows


class FakeSession:
    """返回预置的 (id, title, summary, content_text) 行，不连库。"""

    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows
        self.statements: list[str] = []

    async def execute(self, statement):
        self.statements.append(str(statement))
        return FakeResult(self._rows)


class FakeReranker:
    name = "fake-reranker"

    def __init__(self, scores: list[float]) -> None:
        self._scores = scores
        self.seen: list[str] = []

    def score(self, query: str, passages: Sequence[str]) -> list[float]:
        self.seen = list(passages)
        return self._scores


def test_header_text_keeps_title_first():
    text = build_header_text("第 407 期：国家为什么需要开源", "这一期聊开源生态")

    assert text.startswith("第 407 期")
    assert "开源生态" in text


def test_header_text_falls_back_to_title_only():
    """摘要为空时只索引标题，这不叫降级——标题本身就是主题最浓缩的表达。"""

    assert build_header_text("只有标题", None) == "只有标题"
    assert build_header_text(None, "  只有摘要  ") == "只有摘要"
    assert build_header_text("", "") == ""


def test_header_text_is_capped():
    """摘要超长要截断，否则一个块的 token 数会超过模型上限。"""

    text = build_header_text("标题", "很" * 2000)

    assert len(text) <= 400


def test_passage_contains_all_three_parts():
    passage = _rerank_passage("标题", "摘要内容", "命中块原文")

    assert "标题" in passage
    assert "摘要内容" in passage
    assert "命中块原文" in passage


def test_passage_truncates_summary_and_snippet():
    passage = _rerank_passage("标题", "摘" * 500, "段" * 500)
    lines = passage.split("\n")

    assert len(lines[0]) <= 60
    assert len(lines[1]) <= document_service.RERANK_SUMMARY_CHARS
    assert len(lines[2]) <= document_service.RERANK_SNIPPET_CHARS


def test_rerank_reorders_by_score():
    ids = [uuid.uuid4() for _ in range(3)]
    rows = [(i, f"标题{i}", "摘要", "正文") for i in ids]
    session = FakeSession(rows)
    # 第三个分数最高，第一个最低
    rerank_service.set_reranker(FakeReranker([0.1, 0.5, 0.9]))

    ranked, scores = asyncio.run(
        _rerank_ordered(session, query="随便问", ordered=ids, candidates=10)
    )

    assert ranked == [ids[2], ids[1], ids[0]]
    assert scores[ids[2]] == 0.9


def test_rerank_abandons_when_score_count_mismatch():
    """分数比候选少一个时，宁可整批不重排，也不要把分数贴到错的文档上。"""

    ids = [uuid.uuid4() for _ in range(3)]
    rows = [(i, "标题", "摘要", "正文") for i in ids]
    session = FakeSession(rows)
    rerank_service.set_reranker(FakeReranker([0.9, 0.1]))

    ranked, scores = asyncio.run(
        _rerank_ordered(session, query="随便问", ordered=ids, candidates=10)
    )

    assert ranked == ids
    assert scores == {}


def test_rerank_only_votes_for_its_own_candidates():
    """返回结果不能包含未重排的尾部，否则等于给原顺序附加一票。"""

    ids = [uuid.uuid4() for _ in range(5)]
    rows = [(i, "标题", "摘要", "正文") for i in ids]
    session = FakeSession(rows)
    rerank_service.set_reranker(FakeReranker([0.1, 0.2]))

    ranked, _ = asyncio.run(
        _rerank_ordered(session, query="随便问", ordered=ids, candidates=2)
    )

    assert len(ranked) == 2
    assert set(ranked) == {ids[0], ids[1]}


def test_rerank_uses_snippet_at_char_offset():
    """按 char_start 切出命中块，而不是把整篇正文塞给模型。"""

    first, second = uuid.uuid4(), uuid.uuid4()
    body = "前置内容" * 100 + "命中块在这里"
    rows = [
        (first, "标题一", "摘要一", body),
        (second, "标题二", "摘要二", "另一篇的正文"),
    ]
    session = FakeSession(rows)
    reranker = FakeReranker([0.5, 0.1])
    rerank_service.set_reranker(reranker)

    asyncio.run(
        _rerank_ordered(
            session,
            query="随便问",
            ordered=[first, second],
            candidates=10,
            starts={first: 400},
        )
    )

    assert "命中块在这里" in reranker.seen[0]
    assert "前置内容" not in reranker.seen[0]


def test_rerank_skipped_when_unavailable():
    """模型缺失时 get_reranker 抛 RerankUnavailable，绝不用假分数顶上。"""

    from app.services.rerank_service import RerankUnavailable

    original = rerank_service.settings.rerank_backend
    rerank_service.set_reranker(None)
    rerank_service.settings.rerank_backend = "disabled"
    try:
        try:
            rerank_service.get_reranker()
        except RerankUnavailable:
            pass
        else:  # pragma: no cover - 这条不该走到
            raise AssertionError("重排不可用时必须抛 RerankUnavailable")
    finally:
        rerank_service.settings.rerank_backend = original
