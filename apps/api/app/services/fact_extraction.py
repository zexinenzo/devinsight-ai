"""阶段 7：事实抽取的解析与校验（纯函数，不碰数据库、不调网络）。

`scripts/extract_facts.py` 只做三件事：挑文档、调模型、落库。中间那段
「模型的 JSON → 一条能落库的事实」放在这里，因为它全是纯函数，能被测试直接钉住。

这段代码值得单独存在的理由：**模型输出是不可信输入**。它会给中文类别、给
`launched` 这种自造谓词、给「2025-11」这种只到月的日期、把数组包在
`{"facts": [...]}` 里、在 JSON 前后加一段解释。这些都得在这里被明确处理，
处理方式还要能解释得清——放在脚本里就只能靠跑一遍看结果。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date

from app.services.predicate_specs import OBJECT_TYPES, SPECS, object_shape_problem
from app.services.timeline_service import KINDS, PREDICATES, normalize_key

SYSTEM_PROMPT = """你是信息抽取器。从给定文章中抽取结构化事实。

**输出格式是硬约束：整条回复只能是一个 JSON 数组。** 不要写「以下是抽取结果」这类
前言，不要在数组外写任何解释，不要用 ``` 代码围栏。没有可抽的事实就输出 `[]`
这个字面量本身——「本文没有可抽取的事实」不是 JSON，会被整篇丢弃。

每条事实：
{
  "subject": "公司/产品/机构名",
  "subject_kind": "company|product|person|model|org",
  "predicate": "从下方词表里选一个",
  "object": "事实的宾语：一个短名词短语，不超过 20 字",
  "valid_from": "YYYY-MM-DD 或 null",
  "evidence": "支撑这句话的原文片段，不超过 80 字，必须是原文里真实存在的句子",
  "confidence": 0.0 到 1.0 的数字
}

谓词词表（只能选这些，选不到就不要抽这条）：
{predicates}

规则：
1. 只抽文章**明确陈述**的事实。不推断、不补全、不做常识联想。
2. 主语是公司或产品，不是人。例如「OpenAI 的 CEO 是 Sam Altman」抽成
   subject=OpenAI, predicate=ceo, object=Sam Altman。
3. **object 必须是名词短语，不超过 20 字。分数、名次、金额、解释性从句一律
   放进 evidence，不要写进 object。**
   反例：object 写成 "Autoform-Hard（56 项中解出 49 项）" → 应只写 "Autoform-Hard"
   反例：object 写成 "在覆盖 24 个法律领域的评测中排名第一" → 这是句子不是宾语，
   榜单名写成 "法律领域评测"，名次写进 evidence（见规则 10）。
   反例：object 写成 "计划扩展，向全球 20 万研究人员免费开放" → 这是句子，不要抽。
   反例：object 写成 "30 个库" → 数量不是产物名，这类不抽。
   object 好不好，判据是「别处报道这件事时会不会用同一个名字」："Autoform-Hard"
   会被反复提到，"面向 20 万研究人员的免费访问" 不会——那是一次公告本身，
   不是实体，只该留在 evidence 里。
4. **object 不能与 subject 相同。**「Mode 开源了 Mode」这类零信息量的事实不要抽。
5. **宾语里有多个并列实体时拆成多条。**
   反例：object="GPT-6.2 Astra and Sol" → 拆成两条，Astra 一条、Sol 一条。
6. valid_from 只在两种情况下填：① 文章明确写出日期；② 正文说「今天」「昨日」
   「本周三」这类**能唯一换算**的说法，此时用文章顶部的「发布时间」换算成具体日期。
   「最近」「日前」这类无法定位到某一天的一律填 null。
   **不要只因为"文章有发布日期"就拿它当 valid_from**——一篇 2026 年的回顾文章写
   「2024 年发布 GPT-4o」，照抄发布日期会把这条事实挪到 2026 年。
7. 不要抽「某篇文章提到了某公司」这类事。要的是这家公司做了什么。
8. 同一件事只抽一次，不要因为文章多处提到就重复抽取。
9. 抽不出符合词表的事实就返回空数组 []，这是正常结果。
10. **同一个谓词的宾语类型必须固定。** benchmark 的 object 只填评测集或基准名
    （"Autoform-Hard"），分数与名次一律写进 evidence。**泛称的榜单**（"leaderboard"）
    与**评审团**（"LLM judges panel"）不是基准名，这类不抽。
    反例：object="法律领域评测第一名" → 榜单名是"法律领域评测"，"第一名"是成绩，
    写进 evidence，不要写进 object。
11. 宾语只有**泛称**时（"iPhone 版"、"partner plugins"），前面补上主语名或专名，
    让它脱离本文也能指得清："Mode iPhone 版"、"ChatGPT partner plugins"。
    **这不算编造**——主语就在同一个三元组里。补不出更完整的名字时保留原样，
    不要因为「补不全」就把这条事实丢掉。
12. **evidence 必须是原文的逐字复制，语言与原文一致。** 原文是英文就引英文，
    不要翻译成中文，不要改写，不要用省略号把两处拼起来。
    这一条不是格式洁癖：evidence 会跟原文做子串比对，翻译过的证据与「编造」
    在机器眼里无法区分，整条事实会被判成不可信而丢掉。
"""

# prompt 要求 20 字，这里放宽到 40 再拦：中文 20 字是硬要求，但英文产品名
# （`ChatGPT for Academic Researchers`）天然就长，按 20 拦会误杀。40 只用来
# 挡住「整句当宾语」那一类——实测里它们普遍在 35 字以上。
MAX_OBJECT_CHARS = 40


@dataclass(slots=True)
class ExtractedFact:
    subject: str
    subject_kind: str
    predicate: str
    object_text: str
    valid_from: date | None
    evidence: str | None
    confidence: float


def build_prompt(predicates: dict[str, str] | None = None) -> str:
    lines = _predicate_lines(predicates) if predicates else _spec_lines()
    # 用 replace 而不是 format：SYSTEM_PROMPT 里有 JSON 示例，`{` `}` 会被 format
    # 当成占位符，第一次调用就 KeyError。这个坑直到给 build_prompt 写测试才暴露——
    # 在此之前它一次都没被调用过。
    return SYSTEM_PROMPT.replace("{predicates}", lines)


def _spec_lines() -> str:
    """谓词表由 `predicate_specs.SPECS` 渲染，宾语类型和代码判定同源。

    这里多加的那半句（「宾语：产品、模型或功能名」）以前只写在规则 3 和规则 10 里，
    而且是散文形式：「benchmark 的 object 只填评测集或榜单名」。结果是模型知道
    benchmark 该怎么填、不知道 `founded` 该填年份还是人名——那一条正是靠审计 ② 档
    事后抓出来的（`英伟达 --founded--> 黄仁勋`）。类型属于谓词的定义，就该跟谓词
    写在同一行。
    """

    return "\n".join(
        f"- {spec.key}：{spec.label}（宾语：{spec.hint or OBJECT_TYPES[spec.object_type]}）"
        for spec in SPECS
    )


def _predicate_lines(predicates: dict[str, str]) -> str:
    """测试与临时实验用的覆盖入口：只给名字和标签，不带宾语类型。"""

    return "\n".join(f"- {key}：{label}" for key, label in predicates.items())


def parse_facts_json(content: str) -> list[dict]:
    """`try_parse_facts_json` 的宽松版：解析失败就当空数组。

    这个签名是给测试和调用方用的便利封装。批量抽取**不该**用它——
    「模型说本文没有事实」和「模型说了些解析不出来的话」在这两种情况下
    都是 `[]`，而后者是要换模型的信号，混在一起就永远发现不了。
    """

    return try_parse_facts_json(content) or []


def try_parse_facts_json(content: str) -> list[dict] | None:
    """从模型输出里挖出 JSON 数组；挖不出来返回 `None`（不是空列表）。

    模型会在 JSON 前后加解释、加 ```json 围栏，甚至把数组包成 `{"facts": [...]}`。
    这些都要容忍，但不能容忍「解析失败就猜」——猜出来的事实比没有更糟，
    所以三种形状都对不上就返回 `None`，由调用方单独计数。

    `None` 与 `[]` 的区别就是这段代码存在的全部理由：`[]` 是模型看过文章后
    如实回答「没有可抽的事实」，`None` 是模型没按格式回答。前者可以继续，
    后者说明这批输入需要换模型或缩短。
    """

    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    candidates = [text, _slice_between(text, "[", "]"), _slice_between(text, "{", "}")]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
        if isinstance(parsed, dict):
            for value in parsed.values():
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
    return None


def _slice_between(text: str, open_char: str, close_char: str) -> str | None:
    start = text.find(open_char)
    end = text.rfind(close_char)
    if start == -1 or end <= start:
        return None
    return text[start : end + 1]


def _compact(text: str) -> str:
    """去掉所有空白。中文原文常被换行切断，逐字比对前必须先抹平。"""

    return re.sub(r"\s+", "", text)


def evidence_match(evidence: str | None, source: str) -> str:
    """模型给的证据到底在不在原文里：`exact` / `partial` / `missing`。

    这是抽取任务里最难发现的一类错误：三元组看着对、证据读着通顺，但原文里
    根本没有这句话。人眼逐条核对不现实，而这件事可以机械判定。

    分三档是因为两种「不在」含义不同：`partial` 通常是模型抄了原句又加了
    省略号或改了结尾标点，内容仍然可信；`missing` 才是编造。合成一档会让
    真正需要警惕的信号淹没在标点差异里。
    """

    if not evidence:
        return "missing"
    needle = _compact(evidence)
    haystack = _compact(source)
    if not needle or not haystack:
        return "missing"
    if needle in haystack:
        return "exact"

    # 找最长可匹配前缀：模型抄原句时常在结尾补省略号或改标点，开头通常原样。
    # 不能固定取前 N 字——N 正好切在补充内容上时，整段前缀都匹配不上，
    # 原样抄的证据会被误判成编造。
    # 二分可用是因为单调：needle[:k+1] 是子串则 needle[:k] 必然也是。
    low, high, best = 8, len(needle), 0
    while low <= high:
        mid = (low + high) // 2
        if needle[:mid] in haystack:
            best = mid
            low = mid + 1
        else:
            high = mid - 1
    return "partial" if best >= 8 else "missing"


def classify_unparsed(finish_reason: str, content: str) -> str:
    """解析失败到底属于哪一类：`truncated` / `empty` / `format`。

    这三种原因的处置办法完全不同，混成一个「解析失败」计数等于没诊断：

    - `truncated`：`finish_reason == "length"`，输出被 max_tokens 砍断。
      JSON 从中间断开，怎么解析都不可能成功。修法是提高单次上限或缩短输入。
    - `empty`：上游返回了空字符串。不是格式问题，是上游故障或内容被安全策略拦。
      重发同样内容大概率还是空，该换 provider 或跳过。
    - `format`：模型写了前言、包了额外结构、留了尾逗号。这是 prompt 的事。

    477 篇的实测里三种都出现过：6 篇空字符串、6 篇 JSON 断在半截。如果只统计
    「12 篇解析失败」，会得出「模型不稳定」这种没法动手的结论。
    """

    if finish_reason == "length":
        return "truncated"
    if not content.strip():
        return "empty"
    return "format"


def parse_date_value(value: object) -> date | None:
    """只接受完整日期。月份精度（`2025-11`）故意不收。

    把「2025 年 11 月」补成 `2025-11-01` 会凭空造出一天精度，而那一天的排序位置
    就是错的——时间线上它会排在 11 月 15 日发布的产品前面。宁可留空进 `undated`，
    让「时间未知」这件事保持可见。
    """

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.lower() in {"null", "none", "unknown", "n/a"}:
        return None
    match = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def to_fact(raw: dict) -> tuple[ExtractedFact | None, str]:
    """把一条原始 JSON 转成 ExtractedFact，返回 (fact, 拒绝原因)。

    拒绝原因分四种，因为四种的处理方式不同：
    - `predicate`：模型在造词。该回去补词表，而不是放宽校验。
    - `reflexive`：主语和宾语是同一个东西（`Mode 开源了 Mode`）。零信息量，
      而且会把「这个实体发生过什么」的计数抬高，进时间线只会制造噪声。
    - `too_long`：宾语是整句而不是名词短语。这类条目落库后永远无法与后续
      报道对齐（`在覆盖 24 个法律领域的评测中排名第一` vs 下一次评测），
      时间线比对就失效了。
    - `malformed`：字段缺失，无法挽救。
    - `object_is_*`：宾语形状与谓词声明的类型不符（`30 个库` 当产物名、
      `leaderboard` 当基准名）。判据在 `predicate_specs.object_shape_problem`，
      这里只调一次，不另写一份。
    """

    subject = str(raw.get("subject") or "").strip()
    predicate = str(raw.get("predicate") or "").strip().lower()
    object_text = str(raw.get("object") or "").strip()
    if not subject or not predicate or not object_text:
        return None, "malformed"

    if predicate not in PREDICATES:
        return None, "predicate"

    if normalize_key(subject) == normalize_key(object_text):
        return None, "reflexive"
    if len(object_text) > MAX_OBJECT_CHARS:
        return None, "too_long"

    shape = object_shape_problem(predicate, object_text)
    if shape:
        return None, shape

    kind = str(raw.get("subject_kind") or "").strip().lower()
    if kind not in KINDS:
        # 模型偶尔给中文类别（「公司」）。不为一个标签丢掉整条事实。
        kind = "product"

    valid_from = parse_date_value(raw.get("valid_from"))
    try:
        confidence = float(raw.get("confidence") or 0.5)
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = min(max(confidence, 0.0), 1.0)

    evidence = str(raw.get("evidence") or "").strip()[:400] or None
    return (
        ExtractedFact(
            subject=subject,
            subject_kind=kind,
            predicate=predicate,
            object_text=object_text[:200],
            valid_from=valid_from,
            evidence=evidence,
            confidence=confidence,
        ),
        "",
    )
