"""`amount` 宾语的形状校验。

判据是「有信号就放行」：数字、亿、万元、轮、Series/Seed、IPO、募股、上市、融资。
需要拒绝的是完全没有信号的写法——典型是投资方名字被当成融资轮次。
"""

from __future__ import annotations

import pytest

from app.services.predicate_specs import object_shape_problem


@pytest.mark.parametrize(
    "obj",
    ["慧眼资本", "京东方显智链", "交银资本", "源杰科技独家直投", "英伟达"],
)
def test_lender_name_is_not_amount(obj: str) -> None:
    """投资方名字没有金额也没有轮次，不是 funded 的宾语。"""
    assert object_shape_problem("funded", obj) == "object_is_not_amount"


@pytest.mark.parametrize(
    "obj",
    [
        "近亿元融资",
        "2.7亿元回购专项贷款",
        "470 万欧元",
        "$1.2B",
        "A++轮",
        "天使轮",
        "B+轮融资",
        "IPO",
        "首次公开募股",
        "IPO 募资",
        "Series B",
        "seed round",
    ],
)
def test_amount_forms_pass(obj: str) -> None:
    """常见金额与轮次写法一律放行，不做白名单。"""
    assert object_shape_problem("funded", obj) is None


def test_other_predicates_untouched() -> None:
    """形状规则按 object_type 生效，不能连累 org / person / benchmark。"""
    assert object_shape_problem("acquired", "Deno") is None
    assert object_shape_problem("partnered_with", "富士康") is None
    assert object_shape_problem("ceo", "孙正义") is None


def test_benchmark_rule_still_works() -> None:
    """原有两条规则没有回归。"""
    assert object_shape_problem("benchmark", "leaderboard") == "object_is_generic_ranking"
    assert object_shape_problem("released", "30 个库") == "object_is_quantity_phrase"
