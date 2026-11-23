"""正文门槛与「误标记」回滚：两者是同一个缺陷的两面。

只有摘要的文档被抽取只是浪费，被标记才是真正的损伤——标记之后 enrich 补的正文
再也不会被读到。所以门槛（不再放进来）和回滚（已经进来的退出去）必须一起有。
"""

from __future__ import annotations

import importlib.util
import sys
from argparse import Namespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "extract_facts.py"


def _load_script():
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
        "min_body_chars": 0,
    }
    options.update(overrides)
    return str(extract_facts.build_documents_query(Namespace(**options)))


def test_body_gate_is_off_by_default() -> None:
    """默认 0 必须完全不改现有行为：新增参数不能悄悄改变已跑过的批次。"""

    assert "length(documents.content_text)" not in _sql()


def test_body_gate_keeps_the_summary_branch() -> None:
    """门槛是在「有正文或有摘要」之上再加一层，不是替代它。

    若把 OR 换成 AND，`--only-retracted` 和默认查询都会漏掉「有摘要无正文」的文档——
    而这些文档恰恰是撤回记录最常出现的来源。
    """

    sql = _sql(min_body_chars=800)

    assert "length(documents.content_text) >=" in sql
    assert "documents.summary IS NOT NULL" in sql


def test_body_gate_does_not_touch_ordering() -> None:
    """加门槛不能顺手改排序：最新优先是这批候选集的全部意义。"""

    sql = _sql(min_body_chars=800)

    assert "ORDER BY documents.published_at DESC NULLS LAST" in sql


def test_body_gate_applies_to_reprocess_too() -> None:
    """--reprocess 是「连已判定的重抽」，不是「连没正文的也重抽」。

    两个开关解决的是不同问题：`--reprocess` 管「判过没有」，门槛管「值不值得判」。
    """

    sql = _sql(reprocess=True, min_body_chars=800)

    assert "length(documents.content_text) >=" in sql
    assert "documents.facts_extracted_at IS NULL" not in sql


def test_no_body_condition_requires_marked_and_bodyless() -> None:
    """两个条件缺一不可。

    少 `facts_extracted_at IS NOT NULL` 会把所有无正文文档都退回（包括本来就没抽过的，
    无害但会让人以为回滚了更多）；少 `content_text IS NULL` 会把已经正经抽过的
    有正文文档一起退回，白烧一轮调用。
    """

    sql = str(extract_facts.no_body_condition())

    assert "documents.facts_extracted_at IS NOT NULL" in sql
    assert "documents.content_text IS NULL" in sql
    assert "documents.content_text IS NOT NULL" not in sql
