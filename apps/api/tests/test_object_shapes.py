"""阶段 9：抽取期宾语形状检查。

基线（`docs/数据品质基线.md`）里唯一有明确对错的一类，是「宾语根本不是这个谓词
该有的东西」：`released --> 30 个库`、`benchmark --> leaderboard`。

这里钉三件事：

1. 该拦的拦，样本全部取自基线里的真实行，不是编的例子；
2. **不该拦的不拦**——`1.30.7`（版本号）、`2026`（创立年份）、`1000 人`（裁员规模）
   都是数字开头，是最容易被误杀的一批；
3. 判据只有一份：`to_fact` 调的是同一个函数。
"""

from __future__ import annotations

import pytest

from app.services.fact_extraction import to_fact
from app.services.predicate_specs import object_shape_problem


@pytest.mark.parametrize(
    ("predicate", "object_text"),
    [
        ("released", "30 个库"),
        ("released", "7 款新模型"),
        ("benchmark", "18 public datasets"),
    ],
)
def test_rejects_quantity_phrase(predicate: str, object_text: str) -> None:
    """数量不是名字：它无法与后续报道里的同一个东西对齐。"""
    assert object_shape_problem(predicate, object_text) == "object_is_quantity_phrase"


@pytest.mark.parametrize(
    ("predicate", "object_text"),
    [
        ("benchmark", "leaderboard"),
        ("benchmark", "LLM judges panel"),
        ("benchmark", "blinded judges panel"),
        ("benchmark", "榜单"),
    ],
)
def test_rejects_generic_ranking(predicate: str, object_text: str) -> None:
    """泛称榜单与评审团说的是「怎么评的」，不是「哪个基准」。"""
    assert object_shape_problem(predicate, object_text) == "object_is_generic_ranking"


@pytest.mark.parametrize(
    ("predicate", "object_text"),
    [
        # 版本号：纯数字加点的写法，最容易被数字规则误杀
        ("released_version", "1.30.7"),
        ("released_version", "GPT-6.2 Astra"),
        # 创立年份：`founded` 的宾语本来就该长这样
        ("founded", "2026"),
        # 裁员规模：数量和单位都是内容本身
        ("layoff", "1000 人"),
        # 金额：`2.7亿元…` 也是数字开头，但带专名，不是空数量
        ("funded", "2.7亿元回购专项贷款"),
        # 正常产物名：数字开头但不带量词
        ("released", "20 周年纪念版"),
        # 基准名：名字里带榜单二字，但结尾不是泛称
        ("benchmark", "KradleBench"),
        ("benchmark", "Autoform-Hard"),
    ],
)
def test_keeps_legitimate_objects(predicate: str, object_text: str) -> None:
    """误杀比漏拦更贵：被拦掉的事实不会有第二双眼睛看见。"""
    assert object_shape_problem(predicate, object_text) is None


def test_unknown_predicate_is_left_alone() -> None:
    """规格表里没有的谓词没有「声明的类型」，也就无从判形状。"""
    assert object_shape_problem("launched", "30 个库") is None


def test_to_fact_uses_the_same_judgement() -> None:
    """判据只有一份：`to_fact` 给出的原因码就是上面那个函数的返回值。"""
    fact, reason = to_fact(
        {"subject": "某项目", "predicate": "benchmark", "object": "leaderboard"}
    )
    assert fact is None
    assert reason == object_shape_problem("benchmark", "leaderboard")

    fact, reason = to_fact(
        {"subject": "某项目", "predicate": "released", "object": "30 个库"}
    )
    assert fact is None
    assert reason == "object_is_quantity_phrase"


def test_to_fact_still_accepts_a_normal_object() -> None:
    """新闸门不能把正常的产物名一起拦掉。"""
    fact, reason = to_fact(
        {"subject": "OpenAI", "predicate": "released", "object": "GPT-5"}
    )
    assert reason == ""
    assert fact is not None
