"""实时搜索的价值评测：它到底补上了什么。

**为什么不能沿用 R@5。** 阶段 4 的评测集是「给一个问题找到对的文档」，答案固定，
所以能算召回率。实时搜索的答案每天都在变：今天问「OpenAI 最新发布」得到的十条，
明天就换了一批。硬套固定 ground truth 只会测出「昨天 0.8、今天 0.6」这种噪声，
然后让人去调一个根本没坏的参数。

**这里能量化的是「覆盖缺口补了多少」，不是「答得对不对」：**

- `local_newest`：本地命中里最新那条距今几天
- `web_newest`：实时命中里最新那条距今几天
- `overlap`：实时结果里有多少条本地已经有了——重叠越高，「新信息」越少
- `undated`：上游没给日期的条数。这些不能参与「最新」的判断
- `blind`：本地最新 > 窗口 而 实时有 ≤ 窗口 的结果 → 这一格是实时搜索的净贡献

指标都不需要人工标注，也不会因为新闻更新而失效。代价要写清楚：它只回答
「有没有新的」，不回答「新的对不对」。后者只能靠独立声源数间接反映，
本脚本不声称做了事实核查。

**`--local-profile` 不联网。** 它只查库，回答「我们的源各自覆盖到什么时候」。
这个数字本身就是阶段 5 的存在理由：如果某个源已经两周没更新，实时搜索就从
「加分项」变成了「必需品」。

用法：

    python scripts/eval_evidence.py --local-profile        # 本地时效画像（不联网）
    python scripts/eval_evidence.py --limit 5              # 跑 5 个真实查询（消耗额度）
    python scripts/eval_evidence.py --replay               # 用录制响应回放，不消耗额度
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

API_DIR = Path(__file__).resolve().parents[1] / "apps" / "api"
sys.path.insert(0, str(API_DIR))

from app.core.config import settings
from app.db.session import dispose_engine, session_scope
from app.services import evidence_service
from app.services.web_search_service import (
    WebHit,
    get_web_search,
    parse_results,
    set_web_search,
)
from sqlalchemy import text

FIXTURE = API_DIR / "tests" / "fixtures" / "tavily_news_sample.json"

# 固定的查询集：中英各半。刻意选「时效性话题」而不是「概念性问题」——
# 问「什么是 transformer」不需要实时搜索，问「本周有什么新模型」才需要。
QUERIES: tuple[tuple[str, str], ...] = (
    ("en", "OpenAI latest model release"),
    ("en", "NVIDIA AI chip announcement"),
    ("en", "Anthropic Claude update"),
    ("en", "AI regulation policy"),
    ("zh", "大模型 发布"),
    ("zh", "开源模型 更新"),
    ("zh", "AI 监管 政策"),
    ("zh", "芯片 出口 限制"),
)


class ReplayProvider:
    """回放录制响应。名字里带 replay，避免和实时调用混淆。"""

    name = "tavily-replay"

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def search(self, query: str, *, limit: int, days: int, topic: str) -> list[WebHit]:
        return parse_results(self.payload, provider=self.name)


async def local_profile() -> None:
    """每个启用源的覆盖画像：最新一条多久以前、最近 7 天多少条。"""

    sql = text(
        """
        select s.source_key,
               s.credibility_tier,
               count(d.id) as docs,
               max(coalesce(d.published_at, d.fetched_at)) as newest,
               sum(case when coalesce(d.published_at, d.fetched_at)
                             > now() - interval '7 days' then 1 else 0 end) as last7
        from sources s
        left join documents d on d.source_id = s.id
        where s.enabled
        group by 1, 2
        order by newest desc nulls last
        """
    )
    now = datetime.now(UTC)
    async with session_scope() as session:
        rows = (await session.execute(sql)).all()

    print(f"{'source':22s} {'tier':4s} {'docs':>5s} {'last7':>6s} {'newest':>10s}")
    print("-" * 52)
    stale = 0
    for key, tier, docs, newest, last7 in rows:
        age = (now - newest).days if newest is not None else None
        age_text = f"{age}d" if age is not None else "never"
        if age is None or age > 7:
            stale += 1
        print(f"{key:22s} {tier:4s} {docs:5d} {last7 or 0:6d} {age_text:>10s}")
    print("-" * 52)
    print(f"启用源 {len(rows)} 个，其中 {stale} 个最新一条超过 7 天")


async def run_queries(queries: list[tuple[str, str]], *, days: int) -> None:
    rows: list[dict[str, Any]] = []
    for lang, query in queries:
        async with session_scope() as session:
            bundle = await evidence_service.collect_evidence(
                session, query, days=days, local_limit=8, web_limit=8
            )
        local_ages = [item.age_days for item in bundle.local if item.age_days is not None]
        web_ages = [item.age_days for item in bundle.web if item.age_days is not None]
        local_newest = min(local_ages) if local_ages else None
        web_newest = min(web_ages) if web_ages else None
        rows.append(
            {
                "lang": lang,
                "query": query,
                "local": len(bundle.local),
                "web": len(bundle.web),
                "local_newest": local_newest,
                "web_newest": web_newest,
                "overlap": bundle.verification.overlap_with_local,
                "undated": bundle.verification.date_unverified,
                "domains": bundle.verification.unique_domains,
                # 本地没有窗口内的东西、实时有 → 这一格是净贡献
                "blind": (local_newest is None or local_newest > days)
                and web_newest is not None
                and web_newest <= days,
            }
        )

    print(f"\n{'q':30s} {'lang':4s} {'L':>3s} {'W':>3s} {'L_new':>6s} {'W_new':>6s} {'dup':>4s} {'und':>4s} {'blind':>6s}")
    print("-" * 82)
    for row in rows:
        l_new = f"{row['local_newest']}d" if row["local_newest"] is not None else "-"
        w_new = f"{row['web_newest']}d" if row["web_newest"] is not None else "-"
        print(
            f"{row['query'][:30]:30s} {row['lang']:4s} {row['local']:3d} {row['web']:3d} "
            f"{l_new:>6s} {w_new:>6s} {row['overlap']:4d} {row['undated']:4d} "
            f"{'yes' if row['blind'] else '-':>6s}"
        )

    blind = sum(1 for row in rows if row["blind"])
    print("-" * 82)
    print(f"覆盖盲区被补上的查询：{blind}/{len(rows)}")
    print(
        "注：blind=yes 只说明本地在窗口内没有结果而实时有，"
        "不代表实时那条是对的——判断正确性需要独立声源交叉验证。"
    )


async def main_async(args: argparse.Namespace) -> None:
    if args.local_profile:
        await local_profile()
        return

    provider = None
    if args.replay:
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        provider = ReplayProvider(payload)
        set_web_search(provider)
        print(f"[replay] 使用录制响应 {FIXTURE.name}（不消耗额度；query 不参与检索）")
    else:
        provider = get_web_search()
        if provider is None:
            raise SystemExit(
                "实时搜索未配置：设 WEB_SEARCH_PROVIDER=tavily，或用 --replay / --local-profile"
            )
        print(f"[live] provider={provider.name} topic={settings.web_search_topic} days={args.days}")

    queries = [pair for pair in QUERIES if args.lang == "all" or pair[0] == args.lang]
    queries = queries[: args.limit]
    await run_queries(queries, days=args.days)
    await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description="实时搜索价值评测")
    parser.add_argument("--local-profile", action="store_true", help="只看本地库时效画像，不联网")
    parser.add_argument("--replay", action="store_true", help="回放录制响应，不消耗额度")
    parser.add_argument("--limit", type=int, default=len(QUERIES), help="跑几个查询")
    parser.add_argument(
        "--lang",
        choices=("all", "en", "zh"),
        default="all",
        help="只跑某一语言的问题。中英必须分开看：本地中文源只有 4 个，"
        "和英文源（HN 每天几十条）不是一个量级的覆盖",
    )
    parser.add_argument("--days", type=int, default=7, help="时间窗（天）")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
