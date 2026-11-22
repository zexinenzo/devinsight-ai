"""阶段 8 第 7 步：谓词规格一张表。

改这一处是为了消灭一个已经发生两次的故障模式：**同一个谓词的定义被抄成多份，
其中一份忘了更新。** `partnered_with` 漏在多值名单外 → 15 条假冲突；
`released_version` 同样漏 → nginx / DaisyRose / GPT-6.2 三组「本来就并存」被当成
前后任。两次都不是判断错，是**同一个判断有两个副本**。

所以这里钉的不是「某条规则对不对」，而是**派生关系成不成立**：
prompt 里的类型、冲突判定的单值/多值、审计的形状检查，必须全部来自 `SPECS`。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from app.services.fact_extraction import build_prompt
from app.services.predicate_specs import (
    MULTI_VALUED_PREDICATES,
    OBJECT_TYPES,
    PREDICATE_SPECS,
    PREDICATES,
    SPECS,
)
from app.services.timeline_service import (
    MULTI_VALUED_PREDICATES as MULTI_VALUED_FROM_TIMELINE,
)
from app.services.timeline_service import PREDICATES as PREDICATES_FROM_TIMELINE

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "repair_facts.py"


def _load_script():
    """按路径加载脚本；`sys.modules` 要先登记再 exec（`@dataclass(slots=True)` 依赖它）。"""
    name = "repair_facts_specs"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


repair = _load_script()


# --- 派生关系 ---------------------------------------------------------------


def test_predicate_table_is_derived_from_specs() -> None:
    """名字表不能自己写一遍。写了就会和规格表漂移，而漂移没有信号。"""
    assert {spec.key: spec.label for spec in SPECS} == PREDICATES


def test_multi_valued_set_is_derived_from_specs() -> None:
    """多值名单也不能自己写一遍——它已经漏过两次。"""
    assert {spec.key for spec in SPECS if spec.multi_valued} == MULTI_VALUED_PREDICATES


def test_timeline_service_reexports_the_same_objects() -> None:
    """`timeline_service` 里的两个名字必须是同一份，而不是又抄了一遍。

    它们被 `fact_extraction`、测试和 repair 脚本从旧路径导入，所以保留旧路径；
    但保留不等于可以各存一份——本测试用 `is` 而不是 `==`，抄一份再相等也不行。
    """
    assert PREDICATES_FROM_TIMELINE is PREDICATES
    assert MULTI_VALUED_FROM_TIMELINE is MULTI_VALUED_PREDICATES


def test_spec_keys_are_unique() -> None:
    """重复的 key 会让 dict 派生静默吞掉一条，而 `len(SPECS)` 照旧。"""
    assert len(PREDICATE_SPECS) == len(SPECS)


# --- prompt 与代码同源 ------------------------------------------------------


def test_every_object_type_has_a_description() -> None:
    """类型名打错一个字，prompt 会在渲染时 KeyError——那是好结果；
    更糟的是先 `setdefault` 成一个空串，模型收到「宾语：」后面什么都没有。"""
    assert {spec.object_type for spec in SPECS} <= set(OBJECT_TYPES)


def test_prompt_states_object_type_for_every_predicate() -> None:
    """prompt 里每个谓词都要带宾语类型。

    原始 prompt 只在规则 10 里对 benchmark 明说了类型（「只填评测集或榜单名」），
    其余 11 个谓词靠模型自己猜——`founded --> 黄仁勋` 就是这么来的：模型猜成了
    「谁创立」，而这一列记的是「何时/由谁」。类型属于谓词的定义，就该长在词表里。
    """
    prompt = build_prompt()
    for spec in SPECS:
        assert f"- {spec.key}：" in prompt
        assert (spec.hint or OBJECT_TYPES[spec.object_type]) in prompt


def test_no_predicate_takes_a_license_as_its_object() -> None:
    """① 档的前提：**任何**谓词的宾语都不该是许可证。

    `open_sourced` 是最容易被写成 `license` 类型的那一个——「开源」听起来就像在说
    许可证。真写成那样，`is_shape_violation` 会同时是「对的」和「错的」：它撤回的是
    宾语槽里出现许可证的记录（Fardesk / 两行 Windshift，实跑 3 条，都撤回得对），
    而如果 `open_sourced` 的宾语就该是许可证，这 3 条撤得全错。

    所以这个类型集合里没有 license 是刻意的，不是忘了加。
    """
    assert "license" not in OBJECT_TYPES
    assert PREDICATE_SPECS["open_sourced"].object_type == "product"
    assert repair.is_shape_violation("Apache-2.0") == "object_is_license"


def test_prompt_has_one_line_per_spec_and_no_extra() -> None:
    """多一行或少一行都要报出来：这是「词表与代码是同一份」的直接证据。"""
    typed = [
        line
        for line in build_prompt().splitlines()
        if line.startswith("- ") and "（宾语：" in line
    ]
    assert len(typed) == len(SPECS)


def test_prompt_override_still_works() -> None:
    """测试和临时实验会给一份替身词表；它不该被规格表覆盖掉。"""
    prompt = build_prompt({"only_this": "只有这一个"})
    assert "- only_this：只有这一个" in prompt
    assert "- released：" not in prompt


# --- 审计的形状检查按 object_type 选 ---------------------------------------


def test_audit_picks_check_by_object_type_not_by_name() -> None:
    """理由串没变，但分支依据变了：从「谓词叫什么」变成「宾语该是什么类型」。

    这一条钉的是行为不变——重构不能顺手改动 ② 档给出的理由，
    那些字符串已经被 38 条审计测试和文档引用着。
    """
    assert repair.object_shape_reason("founded", "黄仁勋") == "founded_object_not_year_or_org"
    assert repair.object_shape_reason("founded", "1993") is None
    assert repair.object_shape_reason("ceo", "Sam Altman") is None
    # 用 `OpenAI` 而不是 `英伟达`：`looks_like_person` 认不出 2～4 字的中文专名，
    # `英伟达` 会被判成人名——这是那条启发式已知的假阴性，不该被这条测试当成基准
    assert repair.object_shape_reason("ceo", "OpenAI") == "ceo_object_not_person"
    assert repair.object_shape_reason("funded", "B轮") is None
    assert repair.object_shape_reason("funded", "矽品精密") == "funded_object_is_not_amount"
    # 债务工具用「分行贷款承诺函」而不是「5 亿美元授信额度」：后者先命中金额规则，
    # 根本走不到债务分支——这条顺序（先认金额、再认债务）本身是有意的，
    # 带明确金额的融资记录不该因为出现「授信」二字就被降级
    assert repair.object_shape_reason("funded", "工商银行惠州分行贷款承诺函") == (
        "funded_object_is_debt_facility"
    )


def test_predicates_without_a_shape_check_stay_silent() -> None:
    """产品/版本/机构这些类型还没有形状检查，② 档必须闭嘴而不是乱报。

    `released_version --> BF16 checkpoint` 被判「可疑」就毫无意义——它正是那个类型的
    正常写法。宁可少报，也不能让 ② 档输出一堆需要人工回绝的行。
    """
    assert repair.object_shape_reason("released_version", "BF16 checkpoint") is None
    assert repair.object_shape_reason("released", "GPT-5") is None
    assert repair.object_shape_reason("benchmark", "Autoform-Hard") is None


def test_unknown_predicate_never_reaches_shape_check() -> None:
    """规格表里没有的谓词直接返回 None：审计不该替一个不存在的定义判形状。"""
    assert repair.object_shape_reason("launched", "GPT-5") is None
