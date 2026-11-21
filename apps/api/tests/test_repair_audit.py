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
import uuid
from argparse import Namespace
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


def test_license_followed_by_a_qualifier_is_still_a_violation() -> None:
    """钉住 `$` 那个坑。

    `LICENSE_RE` 原来锚定结尾，于是 `AGPL-3.0 单二进制` 逃过 ① 档，
    和已经撤回的 `Windshift --> AGPL-3.0` 是同一个错误，只因为多了三个字。
    放宽的是**尾部**，不是**首部**——首部一旦放宽，`MIT Media Lab` 就会被误撤。
    """
    for text in ("AGPL-3.0 单二进制", "Apache-2.0 许可证", "MIT license", "GPLv3 源码"):
        assert repair.is_shape_violation(text) == "object_is_license", text


def test_qualifier_whitelist_is_not_a_length_rule() -> None:
    """白名单不是「尾巴短就算」。

    按长度放宽会让 `MIT License Manager` 这种产品名被自动撤回，而 ① 档是
    唯一会自动执行的档位，误撤没有第二双眼睛拦得住。
    """
    for text in ("MIT License Manager", "Apache Airflow", "GPL 兼容的运行时"):
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


def test_past_title_before_cfo_is_not_a_contradiction() -> None:
    """「原CFO 接任临时CEO」不是在冒充 CFO，是刚升任 CEO。

    这条曾经被我当成 ③ 档唯一命中真错的战绩，还专门写了测试「钉住 `\\b` 的坑」。
    后来合并实体、回去读完整段落才发现：`原CFO` 里的 CFO 恰恰是「曾经是」，
    规则读到的方向正好相反。**这条事实自始至终都是对的。**

    教训不是正则写错了，是「关键词命中 ≠ 命题成立」——③ 档一旦写成关键词规则，
    就退化成 ② 档里最差的那种：看起来有证据支撑，实际只匹配了词形。
    """
    assert (
        repair.classify(
            "英伟达",
            "ceo",
            "Colette Kress",
            "这是原CFO Colette Kress接任临时CEO以来,英伟达的首份成绩单。",
        )
        is None
    )
    assert (
        repair.classify(
            "NVIDIA",
            "ceo",
            "Jensen Huang",
            "After former CEO Jensen Huang fought for US approval to sell H200 chips",
        )
        is None
    )


def test_present_tense_title_is_still_queued() -> None:
    """剥掉过去式修饰语之后，「她是 CFO」这种直陈仍然要报——这才是 ③ 档的目标。"""

    verdict = repair.classify(
        "NVIDIA",
        "ceo",
        "Colette Kress",
        "Colette Kress, NVIDIA's chief financial officer, said on the call.",
    )
    assert verdict is not None
    assert verdict[1] == "evidence_mentions_other_c_level_title"


def test_chairman_is_not_treated_as_contradiction() -> None:
    """`Fed Chairman Kevin Warsh` 不算错：Chairman 就是该机构的一把手。

    第一版把 president / chairman / 董事长 都列成「别的头衔」，命中的是这条误报。
    而它当时「漏掉」的 CFO 那条，事后证明本来就不该报——两个方向都错了一次，
    只是错在不同层级：一个是词表不对，一个是整个判据方式不对。
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


# --- retract 的参数防线 ---------------------------------------------------


def _retract_args(**overrides) -> Namespace:
    options = {
        "ids": ["7beeb987-93d1-4773-8651-3b2c811c7e30"],
        "reason": "宾语是许可证，原文写的是「Open source · Apache-2.0」",
        "replaced_by": "",
        "undo": False,
        "dry_run": True,
    }
    options.update(overrides)
    return Namespace(**options)


def test_retract_requires_a_reason() -> None:
    """数据库的 CHECK 只要求「不是 NULL」，空字符串能过——所以这条必须挡在应用层。

    没有理由的撤回是一次不可审计的判断，和当初模型编造事实是同一种病。
    """
    plan = repair.parse_retract_args(_retract_args(reason=""))
    assert plan.error
    assert "必须给理由" in plan.error


def test_retract_rejects_whitespace_only_reason() -> None:
    plan = repair.parse_retract_args(_retract_args(reason="   \t "))
    assert plan.error


def test_undo_does_not_need_a_reason() -> None:
    """恢复采信不是判断，只是把三列置回 NULL，不需要举证。"""
    plan = repair.parse_retract_args(_retract_args(reason="", undo=True))
    assert plan.error == ""


def test_retract_parses_multiple_ids() -> None:
    first = "7beeb987-93d1-4773-8651-3b2c811c7e30"
    second = "1420dc3c-e9e2-484b-adce-eee36aac8fb6"
    plan = repair.parse_retract_args(_retract_args(ids=[first, second]))
    assert [str(item) for item in plan.ids] == [first, second]


def test_retract_trims_reason() -> None:
    plan = repair.parse_retract_args(_retract_args(reason="  宾语是许可证  "))
    assert plan.reason == "宾语是许可证"


def test_retract_rejects_bad_uuid() -> None:
    plan = repair.parse_retract_args(_retract_args(ids=["not-a-uuid"]))
    assert plan.error
    assert "UUID" in plan.error


def test_retract_rejects_bad_replaced_by() -> None:
    plan = repair.parse_retract_args(_retract_args(replaced_by="nope"))
    assert plan.error
    assert "replaced-by" in plan.error


# --- merge：实体合并的参数与成环检查 ---------------------------------------


def _merge_args(**overrides) -> Namespace:
    options = {
        "id": "7beeb987-93d1-4773-8651-3b2c811c7e30",
        "into": "1420dc3c-e9e2-484b-adce-eee36aac8fb6",
        "undo": False,
        "dry_run": True,
    }
    options.update(overrides)
    return Namespace(**options)


def test_merge_rejects_self_target() -> None:
    """自指在数据库也有 CHECK，但要在**发语句之前**拒掉：
    让人看到一条 psycopg 的约束冲突，远不如直接说「不能并到自己」。"""
    same = "7beeb987-93d1-4773-8651-3b2c811c7e30"
    plan = repair.parse_merge_args(_merge_args(id=same, into=same))
    assert plan.error
    assert "自己" in plan.error


def test_merge_rejects_bad_uuid() -> None:
    plan = repair.parse_merge_args(_merge_args(into="nope"))
    assert plan.error
    assert "UUID" in plan.error


def test_merge_accepts_distinct_entities() -> None:
    plan = repair.parse_merge_args(_merge_args())
    assert plan.error == ""
    assert plan.dup != plan.root


def test_merge_cycle_is_refused_before_writing() -> None:
    """A→B 已经存在时，再写 B→A 会成环。

    CHECK 拦不住这个：它只看本行，A 与 B 各自都不指向自己。而环路写进库之后，
    向上找根的查询只能靠深度上限硬停——那时已经没人知道哪一行才是真的根。
    """
    a, b = uuid.uuid4(), uuid.uuid4()
    parent = {b: a}  # b 已并到 a 名下
    error = repair.cycle_error(dup=a, root=b, parent=parent)
    assert error
    assert "成环" in error


def test_merge_chain_without_cycle_is_allowed() -> None:
    """链式本身不是错：合并是分批做的，先并一层、再并一层很常见。"""
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    parent = {c: b}  # c → b，现在要把 a → c
    assert repair.cycle_error(dup=a, root=c, parent=parent) == ""


def test_merge_refuses_to_extend_a_chain_that_already_loops() -> None:
    """目标所在的那条链本身有环时，先修它，不要往上加。"""
    a, b = uuid.uuid4(), uuid.uuid4()
    parent = {a: b, b: a}
    error = repair.cycle_error(dup=uuid.uuid4(), root=a, parent=parent)
    assert error
    assert "环" in error
