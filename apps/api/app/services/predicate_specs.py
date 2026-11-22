"""谓词规格：一个谓词「是什么意思、宾语该长什么样、能否同时有多个宾语」。

**为什么单独一个模块。** 这些信息原来散在四处，各自维护：

| 位置 | 它知道的那一份 |
|---|---|
| `timeline_service.PREDICATES` | 谓词名与中文标签 |
| `timeline_service.MULTI_VALUED_PREDICATES` | 能不能同时有多个宾语 |
| `fact_extraction.SYSTEM_PROMPT` 规则 3/10 | 宾语该是什么类型 |
| `scripts/repair_facts.py` 的 ② 档 | 宾语形状不对时怎么判 |

四处各写一遍的结果不是「冗余」，是**会不一致，而且不一致时没有任何信号**。
`released_version` 就是这样漏的：名字表里有它、多值名单里没有，于是
`nginx 1.30.7 / 1.31.9`、`DaisyRose-27B` 的三种量化权重、`GPT-6.2` 的
`Astra / Sol` 三组「两条都成立」被当成前后任报了出来——直到 `supersede --list`
把它们列在同一屏里才看见。

一张表之后，加谓词只改一处：prompt 的宾语类型、冲突判定的单值/多值、审计的
形状检查会同时跟上。这不是为了整洁，是因为「记得同步第二张表」这件事已经被
同一个项目证明做不到两次。

**`multi_valued` 怎么判。** 只有一句话：这个谓词说的是**存在性**还是**唯一性**。

- 「发布过某个版本」是存在性——一个项目可以同时维护 1.30 稳定线与 1.31 主线，
  两种量化权重同时提供，多个变体并列。天然多值。
- 「当前版本是某个版本」才是唯一性。项目里没有这个谓词；将来要建模它得**新开一个
  名字**，而不是把 `released_version` 改回单值——否则 nginx 的两条线又会互相打架。

默认方向也是刻意的：**名单外按单值处理**。漏报的表现是「两条矛盾并存却没人提示」，
比多报危险得多——噪音会被人骂，漏报不会有人知道。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# 封闭集合，不是自由字符串：类型名是 prompt 与代码之间的契约，多一个少一个都该
# 在静态检查时暴露，而不是等到某个谓词渲染时 KeyError。
ObjectType = Literal[
    "product",
    "version",
    "org",
    "person",
    "year_or_org",
    "amount",
    "scale",
    "benchmark",
]

# 宾语类型 → 渲染进抽取 prompt 的那句话。类型名同时在 ② 档里用来选形状检查，
# 所以两边看到的是同一个字符串，不存在「prompt 说要人名、代码查年份」的可能。
#
# 写法上不嵌套括号：这些句子会被套进「（宾语：……）」里，再嵌一层全角括号
# 就成了「（宾语：产品、模型或功能名（`GPT-5`））」，读的人要数括号。
# 举例统一用「，如」引出。
OBJECT_TYPES: dict[ObjectType, str] = {
    "product": "产品、模型或功能名，如 `GPT-5`、`Mode`",
    "version": "版本号，如 `1.31.9`、`v2.0`；同一版本的不同构建产物算同一个宾语",
    "org": "机构名，如公司、实验室、政府部门",
    "person": "人名，如 `Sam Altman`、`黄仁勋`",
    "year_or_org": "年份，如 `1993`，或机构名；**不要填创始人姓名**",
    "amount": "金额或轮次，如 `$1.2B`、`B轮`、`IPO`",
    "scale": "人数或比例，如 `1000 人`、`10%`",
    "benchmark": "评测集或榜单名，如 `Autoform-Hard`；**分数与名次写进 evidence**",
}


@dataclass(frozen=True, slots=True)
class PredicateSpec:
    key: str
    label: str
    object_type: ObjectType
    # 设计稿写的是 `cardinality: Literal["single", "many"]`，实现用 bool：
    # 这里只有两种状态，没有第三种，多一个字符串枚举只多一层要记住的映射。
    multi_valued: bool
    # 类型说得不够清楚时才填。`product` 对 `released` 够用，对 `open_sourced` 不够——
    # 后者要额外说清「许可证不算」。
    hint: str = ""


# 顺序有意义：prompt 里的词表按这个顺序渲染，改动会影响模型输出的稳定性，
# 所以新增谓词往末尾加，不要插在中间。
SPECS: tuple[PredicateSpec, ...] = (
    PredicateSpec("released", "发布产品或模型", "product", True),
    PredicateSpec("released_version", "发布具体版本号", "version", True),
    PredicateSpec("announced", "宣布但未交付", "product", True),
    PredicateSpec("discontinued", "下线或停服", "product", True),
    PredicateSpec("acquired", "收购（主语是收购方）", "org", True),
    PredicateSpec("partnered_with", "建立合作", "org", True),
    # 单值：同一时刻只能有一个人担任 CEO。两个宾语同时成立就是冲突，
    # 这正是 `find_overlaps` 存在的理由，也是 `valid_to` 必须被填的理由。
    PredicateSpec("ceo", "CEO 由谁担任", "person", False),
    PredicateSpec("founded", "创立", "year_or_org", False),
    PredicateSpec("funded", "获得融资", "amount", True),
    PredicateSpec("layoff", "裁员", "scale", True),
    # 宾语是**被开源的项目**，不是许可证。`Fardesk --open_sourced--> Apache-2.0` 这条
    # 三元组读起来是「Fardesk 开源了 Apache-2.0」——许可证是「以什么条件开源」，
    # 是条件不是产物，正确写法是宾语填仓库/项目名，许可证留在 evidence 里。
    #
    # 这也不是随手定的：① 档正是靠「任何谓词的宾语都不该是许可证」这条把上面那类
    # 记录撤回的（实跑 3 条：Fardesk 与两行 Windshift）。如果这里给 `open_sourced`
    # 一个 `license` 类型，① 档的前提就塌了——它要么误撤正确记录，要么被迫按谓词
    # 分支，从此变成判断题。所以**这个类型集合里没有 license，是刻意的**。
    PredicateSpec(
        "open_sourced",
        "开源",
        "product",
        True,
        hint="被开源的项目或仓库名，如 `PyTorch`；许可证如 `Apache-2.0` 写进 evidence，"
        "**不要写进宾语**",
    ),
    PredicateSpec("benchmark", "基准测试成绩", "benchmark", True),
)

PREDICATE_SPECS: dict[str, PredicateSpec] = {spec.key: spec for spec in SPECS}

# 两个派生视图。它们是 dict / frozenset 而不是函数，是因为调用方很多，
# 且这些值在进程生命周期内不变——每次调用都算一遍只会让热路径变慢。
PREDICATES: dict[str, str] = {spec.key: spec.label for spec in SPECS}

MULTI_VALUED_PREDICATES: frozenset[str] = frozenset(
    spec.key for spec in SPECS if spec.multi_valued
)
