"""检索层纯函数的测试：关键词片段切分与兜底开关。

这几条规则没有数据库依赖，但都是「改一行就悄悄失效」的地方：

1. 中文没有空格，`ILIKE '%整句%'` 对自然语言问句是结构性失效——评测集上
   keyword 模式的 fact / topic / en 三类得分全是 0.000。片段兜底就是为此存在的，
   如果哪天有人把它删掉，指标会掉回 0.1 而没有任何报错。
2. 片段截断必须均匀覆盖整句。按顺序取前 12 个会把句尾的关键词切掉，
   「27 寸的 4K 显示器……多少度电」的「度电」正是最关键的区分词。
3. 兜底开关必须能关。hybrid 里关键词那一路若带上片段匹配，英文类会从 0.744
   掉到 0.522（RRF 只看名次，压不住噪音）。
"""

from __future__ import annotations

from app.db.models import Document
from app.services.document_service import _keyword_condition, _keyword_terms
from sqlalchemy import select
from sqlalchemy.dialects import postgresql


def _compile(statement) -> str:
    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


def test_cjk_query_is_split_into_bigrams():
    terms = _keyword_terms("显示器")

    assert terms == ["显示", "示器"]


def test_english_and_numbers_are_kept_whole():
    terms = _keyword_terms("PHP 8.6 partial function")

    assert "PHP" in terms
    assert "8.6" in terms
    assert "partial" in terms
    # 英文按词切，不会出现 「hp」 这种半截 token
    assert "hp" not in [t.lower() for t in terms]


def test_stop_bigrams_are_filtered():
    """「多少」「什么」这类片段命中不代表任何相关性，留着只会稀释排序。"""

    terms = _keyword_terms("一共有多少参数")

    assert "多少" not in terms
    assert "参数" in terms


def test_truncation_covers_the_tail_of_the_sentence():
    """按顺序截断会丢掉句尾关键词，这是采样而不是截断的理由。"""

    terms = _keyword_terms("27 寸的 4K 显示器一个月大概要用掉多少度电")

    assert len(terms) <= 12
    assert "度电" in terms, "句尾的关键词必须被采到，否则这句查询丢失了最关键的区分词"


def test_max_terms_is_respected():
    long_query = "人工智能大模型训练数据标注质量评估体系与方法论研究进展综述"

    assert len(_keyword_terms(long_query, max_terms=6)) <= 6


def test_short_query_yields_few_terms():
    """两字以内的查询切不出两个片段，必须只走整句匹配，不能放宽成模糊匹配。"""

    assert len(_keyword_terms("开源")) == 1


def test_fallback_condition_includes_fragments():
    sql = _compile(select(Document.id).where(_keyword_condition("GPT-3 一共有多少参数")))

    # 整句匹配占 2 次（标题 + 摘要），剩下的都是片段条件
    assert sql.upper().count("ILIKE") > 2


def test_exact_only_condition_has_no_fragments():
    """hybrid 走这条路径：只有整句匹配，没有片段噪音。

    断言看的是 `ILIKE` 的次数而不是某个词在不在 SQL 里——整句本身就包含
    「参数」，用 in 判断永远成立，测不出任何东西。
    """

    sql = _compile(
        select(Document.id).where(_keyword_condition("GPT-3 一共有多少参数", fallback=False))
    )

    assert sql.upper().count("ILIKE") == 2, "整句匹配只应有标题 + 摘要两处条件"
