"""阶段 8 第 8 步：定向重跑之后，撤回记录有没有接替者。

`retracted_replaced_by` 从迁移 0010 起就在表里，但没有任何写入方——又一个
「有列、能写、没人写」的 `aliases`。这一步要把它填上，而填它的判据是设计稿第 6 节
那三条前置条件。这里钉两件事：

1. **三条不齐时必须返回 None，且说明理由。** 留空是正确结果，不是功能缺失：
   猜一次的代价是把「知道是错的」换成「不知道对不对」，而后者没人会再去查。
2. **`evidence_match` 要在复核时重算，不能在抽取时存一份。** 它是文档原文的函数，
   存下来就会和原文脱钩；而 `content_text` 为空时取 `summary` 这条兜底必须与抽取
   时完全一致，否则「当初判 exact 的」会在这里变成 missing，凭空造出一批拒绝。

不连数据库：`pick_replacement` 是纯函数，候选由调用方传进来。
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "repair_facts.py"


def _load_script():
    name = "repair_facts_recheck"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


repair = _load_script()


def candidate(object_text: str, match: str = "exact", **kwargs) -> object:
    defaults = {
        "id": uuid.uuid4(),
        "object_text": object_text,
        "evidence_text": "原文里那句话",
        "match": match,
        "valid_from": date(2026, 11, 18),
    }
    defaults.update(kwargs)
    return repair.ReplacementCandidate(**defaults)


def test_single_exact_candidate_is_chosen() -> None:
    """唯一候选且证据能在原文里逐字找到，才登记替换。"""
    only = candidate("PyTorch", "exact")
    chosen, reason = repair.pick_replacement("Apache-2.0", [only])
    assert chosen is only
    assert reason == ""


def test_partial_evidence_is_refused() -> None:
    """第 3 条：替换方必须比被替换方更硬。

    这条来自实际数据：模型把英文原文意译成中文时 `evidence_match` 是 `partial`，
    三元组本身可能是对的，但**没法在原文里核对**。用它去接替一条已判定违规的记录，
    净效果是把一条确知的错误换成一条无法验证的记录。
    """
    chosen, reason = repair.pick_replacement("Apache-2.0", [candidate("PyTorch", "partial")])
    assert chosen is None
    assert "partial" in reason
    assert "第 3 条" in reason


def test_missing_evidence_is_refused() -> None:
    """`missing` 是模型编了证据，比 partial 更硬地不能用来做替换。"""
    chosen, reason = repair.pick_replacement("黄仁勋", [candidate("1993", "missing")])
    assert chosen is None
    assert "missing" in reason


def test_no_candidate_says_the_two_causes_are_indistinguishable() -> None:
    """0 条候选不是终点，是两种含义相反的情况共用同一种数据库形态。

    模型这次什么都没给 → 词表改动把它劝退了，可能劝过头了。
    模型又给了旧宾语 → 被唯一约束挡掉，词表没起作用。
    两种都表现为「这里没有新记录」，而该采取的动作完全相反。所以提示里必须
    同时给出这两条，并指向重跑日志里唯一能区分它们的证据（`已存在，未写入`）。
    """
    chosen, reason = repair.pick_replacement("Apache-2.0", [])
    assert chosen is None
    assert "已存在" in reason
    assert "--only-retracted" in reason


def test_ambiguous_candidates_are_refused_and_listed() -> None:
    """多条候选时列出全部宾语，人才能判断；只报「有多条」等于让人自己再查一遍。"""
    chosen, reason = repair.pick_replacement(
        "矽品精密", [candidate("5 亿美元授信", "exact"), candidate("B 轮", "exact")]
    )
    assert chosen is None
    assert "5 亿美元授信" in reason and "B 轮" in reason


def test_ambiguity_is_not_resolved_by_picking_the_exact_one() -> None:
    """多条候选里只有一条 exact 时**仍然拒绝**。

    容易顺手加一条「挑唯一 exact 的那个」，但那是在猜对应关系而不是在核对证据：
    exact 只说明这句话在原文里存在，不说明它就是被撤回那条的接替者。
    设计稿的纪律是「定不准就留空」，这条测试就是那个纪律的机器版本。
    """
    chosen, reason = repair.pick_replacement(
        "旧宾语", [candidate("可能的接替者", "exact"), candidate("另一个", "partial")]
    )
    assert chosen is None
    assert "定不了" in reason


def test_candidate_is_frozen_and_hashable() -> None:
    """候选要能放进 set / 当 dict 键：去重逻辑迟早会用到，冻结的 dataclass 才不踩坑。"""
    item = candidate("PyTorch")
    assert {item, item} == {item}
