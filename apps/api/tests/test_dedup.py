"""事件聚类的分组逻辑测试。

用构造数据而不是真实数据，因为真实库里目前没有「多家媒体报同一件事」的样本
（中文快讯、英文 HN、论文三个源的主题几乎不重叠）。没有样本就没法验证聚类是对的，
所以把分组逻辑抽成纯函数 `group_similar_titles` 单独测。

四个必须成立的性质：
1. 不同源、措辞不同但讲同一件事的报道要聚到一起
2. 无关报道不能聚
3. 超过时间窗的相似标题不能聚（三天前的旧闻不是「同一事件」）
4. **同一个源的两条不能聚** —— 这条是实测撞出来的误聚
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.db.models import Document
from app.services.dedup_service import _domain, group_similar_titles

BASE = datetime(2026, 11, 18, 10, 0, tzinfo=UTC)


def _item(
    minutes: int, title: str, source: uuid.UUID | None = None
) -> tuple[uuid.UUID, datetime, str, uuid.UUID]:
    # 不传 source 时每次生成新的，默认视为「不同媒体」
    return uuid.uuid4(), BASE + timedelta(minutes=minutes), title, source or uuid.uuid4()


def test_same_event_reports_are_grouped():
    items = [
        _item(0, "OpenAI 发布 GPT-5 模型，推理能力大幅提升"),
        _item(30, "GPT-5 正式发布：OpenAI 称推理能力大幅提升"),
        _item(90, "OpenAI GPT-5 发布，主打推理能力提升"),
    ]
    groups = group_similar_titles(items)
    assert len(groups) == 1
    assert len(groups[0]) == 3


def test_unrelated_titles_stay_apart():
    items = [
        _item(0, "OpenAI 发布 GPT-5 模型，推理能力大幅提升"),
        _item(5, "英伟达发布新架构显卡，游戏性能提升明显"),
        _item(10, "某地暴雨导致地铁停运，多条线路调整"),
    ]
    groups = group_similar_titles(items)
    assert len(groups) == 3
    assert all(len(group) == 1 for group in groups)


def test_window_blocks_old_similar_title():
    items = [
        _item(0, "OpenAI 发布 GPT-5 模型，推理能力大幅提升"),
        # 三天后才有另一家报道，超出 48 小时窗口：不是同一轮事件
        _item(60 * 72, "GPT-5 正式发布：OpenAI 称推理能力大幅提升"),
    ]
    groups = group_similar_titles(items)
    assert len(groups) == 2


def test_union_is_transitive():
    # 精确构造一条「相似度不闭合」的链：A 与 B 相似、B 与 C 相似，但 A 与 C 只有 0.30，
    # 低于阈值。并查集必须把三条归一组，否则「首发→转述→再转述」的报道链会断。
    #
    # 用无意义词而不是新闻标题，是因为要精确控制 token 集合的包含关系
    # （C ⊂ B ⊂ A），真实标题的用词重叠太随机，凑不出这个边界。
    items = [
        _item(0, "alpha beta gamma delta epsilon zeta theta iota kappa lambda"),
        _item(20, "alpha beta gamma delta epsilon zeta"),
        _item(40, "alpha beta gamma"),
    ]
    groups = group_similar_titles(items)
    assert len(groups) == 1
    assert len(groups[0]) == 3


def test_short_titles_do_not_cluster():
    # "New release" 这类标题 token 太少，任何两条都会被判成相似，必须直接跳过
    items = [
        _item(0, "New release"),
        _item(5, "New release"),
    ]
    groups = group_similar_titles(items)
    assert len(groups) == 2


def test_same_source_never_clusters():
    # 实测撞出来的误聚：OpenAI 同一天发两条公告，都带 "Introducing ChatGPT" 前缀，
    # 标题相似度达标，但它们是两件不同的事。同一个源发两次不构成「多家在报」。
    source = uuid.uuid4()
    items = [
        _item(0, "Introducing ChatGPT Images 2.5", source),
        _item(60, "Introducing ChatGPT for Financial Services", source),
    ]
    groups = group_similar_titles(items)
    assert len(groups) == 2


def test_repost_requires_different_domains():
    """转载必须跨站：同域两篇相似只能说明共用站点模板。

    这条约束是真实数据撞出来的。arXiv 的 abs 页里模板文字（分类路径、编号、
    Submitted on、View PDF、Abstract:）占了大半，两篇无关论文的指纹只差 10 位，
    刚好落在阈值边界上，于是被判成「转载」。它们明明在同一个域名下，
    不可能是互相转载的关系。
    """

    first = Document(canonical_url="https://arxiv.org/abs/2611.23022")
    second = Document(canonical_url="https://arxiv.org/abs/2611.20218")
    assert _domain(first) == _domain(second)

    repost = Document(canonical_url="https://www.reuters.com/world/article")
    mirror = Document(canonical_url="https://techcrunch.com/2026/11/18/article")
    assert _domain(repost) != _domain(mirror)


def test_domain_normalizes_www_and_case():
    assert _domain(Document(canonical_url="https://WWW.Example.com/a")) == "example.com"
    assert _domain(Document(canonical_url="")) == ""
