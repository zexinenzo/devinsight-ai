"""被丢弃的条目要留名：计数只说发生了，名字说发生了什么。"""

from __future__ import annotations

import importlib.util
import sys
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

RAW = {"subject": "美新科技", "predicate": "founded", "object": "星曜宇航"}


def test_every_reason_has_a_chinese_label() -> None:
    """四种原因各有中文名：打出 reason code 会让人以为是程序出错。"""

    for reason, label in [
        ("predicate", "谓词不在词表"),
        ("reflexive", "主语与宾语相同"),
        ("too_long", "宾语超长"),
        ("malformed", "字段缺失"),
    ]:
        line = extract_facts.format_drop(7, RAW, reason)
        assert label in line
        assert "美新科技" in line and "founded" in line and "星曜宇航" in line


def test_unknown_reason_falls_back_to_the_code() -> None:
    """新原因忘了登记时显示英文码，不是空标签。"""

    line = extract_facts.format_drop(1, RAW, "brand_new_reason")

    assert "brand_new_reason" in line
    assert "（）" not in line


def test_missing_fields_do_not_crash() -> None:
    """模型给的原始字典可能缺字段，这一行不能因为缺字段就炸掉整轮。"""

    line = extract_facts.format_drop(2, {}, "malformed")

    assert "字段缺失" in line
    assert "?" in line


def test_long_object_is_truncated() -> None:
    """超长宾语本来就是被丢的原因之一，别让它把日志撑成一屏。"""

    raw = {"subject": "A", "predicate": "released", "object": "很长" * 60}
    line = extract_facts.format_drop(1, raw, "too_long")

    assert line.count("很长") == 20
