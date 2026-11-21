"""审计脚本的三档判定（阶段 8 第 4 步）。

纯函数测试，不连库。这里钉住的是**判据本身**，不是脚本的 IO——
因为判错的代价不对称：漏一条，错事实留在时间线上且没人知道错了；
多报一条，人只是多看一眼。所以这组测试偏向「宁可多报」，
每一条 `assert ... is None` 都是在限制「不能什么都报」。

`classify` 的返回是 (档位, 原因) 或 None。档位的顺序不能反：
① 是形状检查，必须先于任何语义判断——否则 `founded --Apache-2.0` 会被
当成「宾语不是年份」的 ② 项，而它其实是能机械判定的 ① 项。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "repair_facts.py"


def _load_script():
    """按路径加载脚本；`sys.modules` 要先登记再 exec（`@dataclass(slots=True)` 依赖它）。"""
    name = "repair_facts_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


repair = _load_script()


# --- ① 形状 ---------------------------------------------------------------


def test_license_is_shape_violation_under_any_predicate() -> None:
    """包括 open_sourced。豁免它恰好会放过唯一真实的违规。"""
    for predicate in ("open_sourced", "released", "founded", "benchmark"):
        verdict = repair.classify("Fardesk", predicate, "Apache-2.0", None)
        assert verdict is not None
        assert verdict[0] == "1_shape"
        assert verdict[1] == "object_is_license"


def test_license_variants_are_recognized() -> None:
    for text in ("MIT", "GPL-3.0", "AGPL-3.0-only", "Apache-2.0", "BSD", "CC BY"):
        assert repair.is_shape_violation(text) is not None, text


def test_product_names_that_merely_start_with_a_license_name_are_not_flagged() -> None:
    """`Apache Spark` 是产品，`Apache-2.0` 是许可证。正则必须整串匹配。"""
    for text in ("Apache Spark", "MIT Media Lab", "BSD 套接字"):
        assert repair.is_shape_violation(text) is None, text


# --- ② 宾语形状 -----------------------------------------------------------


def test_founded_accepts_year_and_org() -> None:
    assert repair.classify("OpenAI", "founded", "2015", None) is None
    assert repair.classify("OpenAI", "founded", "Y Combinator 实验室", None) is None


def test_founded_with_person_object_is_queued() -> None:
    """错不在「人不能创立公司」，错在这个位置该填何时/由谁做主语。"""
    verdict = repair.classify("OpenAI", "founded", "Sam Altman", None)
    assert verdict == ("2_object_shape", "founded_object_not_year_or_org")


def test_ceo_accepts_person() -> None:
    assert repair.classify("NVIDIA", "ceo", "Jensen Huang", None) is None
    assert repair.classify("英伟达", "ceo", "黄仁勋", None) is None


def test_ceo_with_company_object_is_queued() -> None:
    verdict = repair.classify("OpenAI", "ceo", "OpenAI Inc", None)
    assert verdict is not None
    assert verdict[1] == "ceo_object_not_person"


def test_funded_accepts_amount_or_round() -> None:
    assert repair.classify("Anthropic", "funded", "$2 billion", None) is None
    assert repair.classify("月之暗面", "funded", "B轮", None) is None


def test_funded_accepts_amount_written_without_digits() -> None:
    """「近亿元融资」是真实库里的一条。第一版正则要求数字紧跟 `亿`，把它误报了。"""
    assert repair.classify("深微芯创", "funded", "近亿元融资", None) is None


def test_funded_accepts_financing_event() -> None:
    """IPO 与「B轮」在语义上是同一类：回答「以什么方式融」，不是「融了多少」。"""
    for text in ("IPO", "首次公开募股", "IPO 募资"):
        assert repair.classify("Anthropic", "funded", text, None) is None, text


def test_funded_with_person_object_is_queued() -> None:
    verdict = repair.classify("Anthropic", "funded", "Dario Amodei", None)
    assert verdict is not None
    assert verdict[1] == "funded_object_is_not_amount"


def test_funded_with_investor_object_is_queued() -> None:
    """`深微芯创 --funded--> 深产投`：宾语填了投资方，不是金额。"""
    verdict = repair.classify("深微芯创", "funded", "深产投", None)
    assert verdict is not None
    assert verdict[1] == "funded_object_is_not_amount"


def test_funded_with_loan_facility_gets_its_own_reason() -> None:
    """贷款承诺函是债务工具。谓词用错和宾语填错人，处置方式不同。"""
    verdict = repair.classify("胜宏科技", "funded", "工商银行惠州分行贷款承诺函", None)
    assert verdict is not None
    assert verdict[1] == "funded_object_is_debt_facility"


def test_benchmark_never_enters_the_queue() -> None:
    """泛称宾语数量大、不改变事实判断，该在抽取期用 object_type 挡掉。"""
    assert repair.classify("GPT-5", "benchmark", "leaderboard", None) is None
    assert repair.classify("GPT-5", "benchmark", "Apache-2.0", None)[0] == "1_shape"


# --- ③ 证据指证 -----------------------------------------------------------


def test_ceo_whose_evidence_mentions_cfo_is_queued() -> None:
    """英伟达那条的原始错误：宾语是个人名，形状完全正常。"""
    verdict = repair.classify(
        "NVIDIA",
        "ceo",
        "Colette Kress",
        "Colette Kress, NVIDIA's chief financial officer, said ...",
    )
    assert verdict is not None
    assert verdict[0] == "3_evidence"
    assert verdict[1] == "evidence_mentions_other_c_level_title"


def test_ceo_with_clean_evidence_stays() -> None:
    assert (
        repair.classify("NVIDIA", "ceo", "Jensen Huang", "Jensen Huang is the CEO of NVIDIA.")
        is None
    )


def test_chinese_prefix_before_cfo_still_matches() -> None:
    """真实库里那条 `英伟达 --ceo--> Colette Kress`，原文是「原CFO Colette Kress 接任临时CEO」。

    第一版用的是 `\\bCFO\\b`，而中文字符在 Python 正则里也算 `\\w`，
    `原` 与 `C` 之间没有词边界，于是这条**唯一真实的 ③ 档错误被漏掉了**。
    这个测试钉的就是那个 `\\b`。
    """
    verdict = repair.classify(
        "英伟达",
        "ceo",
        "Colette Kress",
        "这是原CFO Colette Kress接任临时CEO以来,英伟达的首份成绩单。",
    )
    assert verdict is not None
    assert verdict[0] == "3_evidence"


def test_chairman_is_not_treated_as_contradiction() -> None:
    """`Fed Chairman Kevin Warsh` 不算错：Chairman 就是该机构的一把手。

    第一版把 president / chairman / 董事长 都列成「别的头衔」，结果 ③ 档
    唯一命中的竟是这条误报，而真正的 CFO 那条被漏掉——一次错的规则同时制造了
    一个假阳性和一个假阴性。
    """
    assert (
        repair.classify(
            "Federal Reserve",
            "ceo",
            "Kevin Warsh",
            "though this runs contrary to how Fed Chairman Kevin Warsh has preferred to communicate",
        )
        is None
    )


def test_acquisition_direction_reversal_is_queued() -> None:
    verdict = repair.classify(
        "Figma",
        "acquired",
        "Adobe",
        "Adobe was acquired by Figma in a deal announced Thursday.",
    )
    assert verdict is not None
    assert verdict[1] == "acquisition_direction_maybe_reversed"


def test_missing_evidence_does_not_crash_or_flag() -> None:
    """没有 evidence 的条目进不了 ③，但不该因此被当成有问题。"""
    assert repair.classify("OpenAI", "ceo", "Sam Altman", None) is None
    assert repair.classify("OpenAI", "ceo", "Sam Altman", "") is None


# --- 输出 -----------------------------------------------------------------


def test_csv_keeps_chinese_readable_and_has_header() -> None:
    row = repair.AuditRow(
        tier="1_shape",
        fact_id="00000000-0000-0000-0000-000000000000",
        subject="Fardesk",
        predicate="open_sourced",
        object_text="Apache-2.0",
        valid_from="2026-11-01",
        date_source="文中明说",
        reason="object_is_license",
        evidence="Fardesk 以 Apache-2.0 协议开源",
    )
    text = repair.render_csv([row])
    assert text.splitlines()[0] == ",".join(repair.CSV_HEADER)
    assert "Fardesk" in text
    assert "文中明说" in text  # 不被转义成 \u 序列


def test_summary_counts_each_tier() -> None:
    rows = [
        repair.AuditRow("1_shape", "a", "Fardesk", "open_sourced", "Apache-2.0", "", "", "x", ""),
        repair.AuditRow("2_object_shape", "b", "OpenAI", "founded", "Sam Altman", "", "", "y", ""),
        repair.AuditRow("3_evidence", "c", "NVIDIA", "ceo", "Colette Kress", "", "", "z", ""),
    ]
    text = repair.summarize(rows)
    assert "① 形状不符（可自动撤回）：1" in text
    assert "② 宾语形状可疑（人工队列）：1" in text
    assert "③ 证据与谓词冲突（人工队列）：1" in text
