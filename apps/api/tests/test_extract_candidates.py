"""抽取候选集的「抽过没有」判断（阶段 8）。

不连数据库：`build_documents_query` 只拼语句，把它编译成 SQL 文本就能断言形状。
这类断言的价值在上一轮统计里已经付过一次学费——`抽出 238 条 / 重复 90 条`，
38% 的模型调用花在把同一批文档重抽一遍，而代码里根本没有一处地方在记「抽过」。
只靠读代码不容易发现「少了一个条件」，编译出来的 WHERE 子句是看得见的。
"""

from __future__ import annotations

import importlib.util
import sys
from argparse import Namespace
from pathlib import Path

from app.db.models import Document

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "extract_facts.py"
MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "versions" / (
    "0011_document_facts_extracted_at.py"
)


def _load_script():
    """脚本不是包（`scripts/` 没有 `__init__.py`），只能按路径加载。

    加载时会执行模块顶部的 `sys.path.insert(API_DIR)`，所以 `app.*` 随后可导入。

    `sys.modules` 必须先登记再 exec：脚本里有 `@dataclass(slots=True)`，
    而 dataclasses 要回头查 `sys.modules[cls.__module__]` 才能判定字段类型。
    不登记就抛 `AttributeError: 'NoneType' object has no attribute '__dict__'`，
    报错点在 dataclasses 内部，离真正的原因很远。
    """
    name = "extract_facts_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


extract_facts = _load_script()


def _sql(**overrides) -> str:
    options = {
        "since": "",
        "keyword": "",
        "limit": 10,
        "reprocess": False,
        "only_retracted": False,
    }
    options.update(overrides)
    return str(extract_facts.build_documents_query(Namespace(**options)))


def test_default_query_excludes_already_judged_documents() -> None:
    """默认必须带 `facts_extracted_at IS NULL`，否则就是上一轮那个 bug。"""
    assert "documents.facts_extracted_at IS NULL" in _sql()


def test_reprocess_includes_everything() -> None:
    """--reprocess 是唯一的重抽入口。

    断言的是 `IS NULL` 而不是裸列名：`select(Document)` 会把所有列都列进 SELECT，
    裸名永远为真，这条测试就失去意义了。
    """
    assert "documents.facts_extracted_at IS NULL" not in _sql(reprocess=True)


def test_newest_first_is_kept() -> None:
    """去重不能顺手把排序改掉：新文档优先，旧冷门文档天然沉底。"""
    sql = _sql()
    assert "ORDER BY documents.published_at DESC NULLS LAST" in sql


def test_only_retracted_reverses_the_extracted_gate() -> None:
    """定向重跑取的正是「已判定过」的文档，所以必须绕开 `facts_extracted_at IS NULL`。

    这一条最容易写错的地方是**不报错**：走默认分支时查询合法、返回 0 行，日志打印
    「待处理文档 0 篇」——看起来像「没有要复核的东西」，实际是条件互斥。定向重跑
    的全部价值就在于它精确地取到那十几篇，取不到等于什么都没做。
    """
    sql = _sql(only_retracted=True)
    assert "documents.facts_extracted_at IS NULL" not in sql
    assert "fact_claims.retracted_at IS NOT NULL" in sql


def test_only_retracted_still_requires_body_or_summary() -> None:
    """精确取文档不等于放弃「有正文」这条：没有正文的文档重抽也抽不出东西。"""
    sql = _sql(only_retracted=True)
    assert "documents.content_text IS NOT NULL" in sql


def test_limit_zero_means_no_limit() -> None:
    """0 表示不限。

    定向复核的候选集只有十几篇，若沿用默认 10 篇，表现是「跑完了，还剩几条没复核」
    而日志上看不出来——和静默丢条目是同一种病。默认值按模式分由 `main` 决定：
    普通增量 10 篇保护预算，`--only-retracted` 时不限。
    """
    assert "LIMIT" not in _sql(limit=0).upper()
    assert "LIMIT" in _sql(limit=10).upper()


def test_still_requires_body_or_summary() -> None:
    """没有正文也没有摘要的文档抽不出任何东西，不该进候选集。"""
    sql = _sql()
    assert "documents.content_text IS NOT NULL" in sql
    assert "documents.summary IS NOT NULL" in sql


def test_limit_and_filters_are_applied() -> None:
    sql = str(
        extract_facts.build_documents_query(
            Namespace(since="2026-11-01", keyword="GPT", limit=7, reprocess=False)
        )
    )
    assert "LIMIT" in sql.upper()
    assert "documents.published_at >=" in sql
    # ilike 在通用方言下编译成 lower(...) LIKE lower(...)，在 PG 下是 ILIKE，
    # 两种都算过——这里断言的是「关键词条件还在」，不是它的具体写法
    assert "LIKE" in sql.upper()


def test_orm_and_migration_both_have_the_column() -> None:
    assert "facts_extracted_at" in Document.__table__.columns
    assert Document.__table__.columns["facts_extracted_at"].nullable is True

    sql = " ".join(MIGRATION.read_text(encoding="utf-8").split())
    assert "ADD COLUMN facts_extracted_at timestamptz" in sql


def test_migration_backfills_from_existing_facts() -> None:
    """已有的 852 篇里，产过事实的那批能从事实行反推，别让它们白重抽一次。

    反推必须用 `max(recorded_at)`：一篇被抽过多轮时，「结论已定」是最后一次。
    """
    sql = " ".join(MIGRATION.read_text(encoding="utf-8").split())
    assert "UPDATE documents d" in sql
    assert "max(recorded_at)" in sql


def test_migration_does_not_backfill_failed_documents() -> None:
    """回填只来自 fact_claims 里已有的行。

    这条是防「顺手把 NULL 全填成 now()」：那样一篇被 max_tokens 截断的文档
    会变成「已判定」，从此再也不会被重试，错误被永久固化。
    """
    sql = " ".join(MIGRATION.read_text(encoding="utf-8").split())
    assert "SET facts_extracted_at = f.last_at" in sql
    assert "now()" not in sql.lower()
