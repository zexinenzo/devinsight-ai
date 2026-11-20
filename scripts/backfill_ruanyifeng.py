"""回填阮一峰周刊的历史文章（CC BY-NC-ND 3.0 授权）。

为什么要单独写脚本，而不是加个 Connector：

Atom feed 只输出最近 3~6 期，可周刊已经出到 419 期。剩下那 400 多期不会
出现在 feed 里，只能从月度归档页 /blog/YYYY/MM/ 逐个翻。这是「一次性回填」，
不是「持续采集」，用一个 CLI 命令去轮询历史页面没有意义。

为什么值得回填：

1. 授权是全项目最干净的一个——页面明示 CC BY-NC-ND 3.0，权利人自己
   放开了复制与转载，不需要我们做合规判断；
2. 中文语料长期只有 36氪 那 30 条快讯，检索演示根本不够用。周刊每期
   5000 字左右、400 多期，是这个项目唯一能拿到的中文长文语料库；
3. ND 条款要求「保持原样」，所以摘要必须是原文首段的原样摘取，
   不能在这里做任何改写或润色。

限速 0.4 秒/请求：对方是个人博客，跑在别人的服务器上。

用法（在 apps/api 目录下跑，需要能 import app.*）：

    python ../scripts/backfill_ruanyifeng.py
    BF_START=2026-10 BF_END=2026-11 python ../scripts/backfill_ruanyifeng.py   # 试跑两个月
    python ../scripts/backfill_ruanyifeng.py --summaries-only                  # 只按新规则重算摘要

脚本是幂等的：已存在（按 external_id 或去掉 scheme 的 URL）的条目会跳过。

`--summaries-only` 是给「改摘要规则」用的：正文已经躺在 `content_text` 里，
重算不该再抓一遍站——既慢（390 篇约十分钟），也是对别人服务器多一次打扰。
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psycopg2
from bs4 import BeautifulSoup
from psycopg2.extras import Json

# 允许从任意目录运行：脚本在 scripts/ 下，而 app.* 在 apps/api/ 下。
# 不加这三行，文档里写的 `python ../scripts/backfill_ruanyifeng.py` 会直接
# ModuleNotFoundError——sys.path[0] 是脚本自己所在的目录，不是当前工作目录。
_API_DIR = Path(__file__).resolve().parents[1] / "apps" / "api"
if str(_API_DIR) not in sys.path:
    sys.path.insert(0, str(_API_DIR))

from app.services.classifier import classify_topics
from app.utils.simhash import simhash
from app.utils.text import content_hash
from app.utils.urls import canonicalize_url

DB = {"host": "localhost", "port": 5432, "user": "devinsight", "password": "change_me_dev", "dbname": "devinsight"}
UA = {"User-Agent": "DevInsightAI/0.1 (+contact: admin@example.com)"}
DELAY = 0.4  # 秒/请求
SOURCE_KEY = "ruanyifeng_zh"


# 周刊从 2018 年末开始，到脚本运行时为止。多给几个月是防边界遗漏。
# 用环境变量可以在试跑时只覆盖最近两个月（BF_START=2026-10 BF_END=2026-11）。
def _month_env(name: str, default: tuple[int, int]) -> tuple[int, int]:
    raw = os.environ.get(name)
    if not raw:
        return default
    year, month = raw.split("-")
    return int(year), int(month)


START = _month_env("BF_START", (2018, 11))
END = _month_env("BF_END", (2026, 11))

DATE_RE = re.compile(r"发表日期[:：]\s*(\d{4})年(\d{1,2})月(\d{1,2})日")


def months(start: tuple[int, int], end: tuple[int, int]) -> list[tuple[int, int]]:
    """生成 [(2018,11), (2018,12), ...] 形式的月份列表。"""

    result: list[tuple[int, int]] = []
    year, month = start
    while (year, month) <= end:
        result.append((year, month))
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return result


def extract_article(html_text: str, url: str) -> dict | None:
    """从详情页抽出标题、正文、发布时间。

    正文容器用 div#main-content：实测它能完整包住周刊正文，且不含侧边栏。
    换成 article 标签会把评论一起带进来。

    正文**必须保留段落边界**（用 "\\n" 而不是空格连接）。这里踩过一次：
    clean_html 用 separator=" " 把所有块级元素连成一行，段落全没了，
    于是「取第一段当摘要」实际取到的是整篇的开头 300 字——而周刊开头
    每期都是同一段固定套话（「这里记录每周值得分享的科技内容…本杂志开源」）。
    结果是 166 篇的摘要看起来一模一样，分类也全被拉向 open-source。
    """

    soup = BeautifulSoup(html_text, "html.parser")
    container = soup.select_one("div#main-content")
    if container is None:
        return None

    # 块级元素逐个取文本，用换行拼接，保住段落边界
    blocks = [element.get_text(" ", strip=True) for element in container.find_all(["p", "h1", "h2", "h3", "h4", "li"])]
    body = "\n".join(block for block in blocks if block)
    if len(body) < 800:  # 周刊不可能这么短，短了就是抽错了
        return None

    # 标题：<title> 末尾带「 - 阮一峰的网络日志」后缀，切掉。
    # 必须过一遍 NFKC：RSS 路径的标题经过 clean_html 归一化（全角括号变半角），
    # 这里若原样保留全角，同一篇文章会有两个看起来不同的标题。
    title = ""
    if soup.title and soup.title.string:
        title = unicodedata.normalize("NFKC", soup.title.string.split(" - ")[0]).strip()

    # 发布时间不在正文容器里——它挂在页脚，形如「发表日期： 2026年11月13日」。
    # 所以日期要拿整页文本去找，正文仍只取 #main-content（否则评论会被卷进来）。
    page_text = soup.get_text(" ", strip=True)
    published = None
    match = DATE_RE.search(page_text)
    if match:
        year, month, day = (int(x) for x in match.groups())
        published = datetime(year, month, day, tzinfo=UTC)
    if published is None:
        # 兜底：URL 里就带着 /blog/2026/11/，退化成当月 1 号。
        # 同月内几期的先后会乱，但排序和去重只看量级，不影响结论。
        url_match = re.search(r"/blog/(\d{4})/(\d{2})/", url)
        if url_match:
            published = datetime(int(url_match.group(1)), int(url_match.group(2)), 1, tzinfo=UTC)

    return {"title": title, "body": body, "published_at": published, "url": url}


# 周刊开头每期都是同一段固定套话。它不是内容，但会霸占摘要的位置，
# 还会把分类器全带向 open-source（「开源」就出现在这句里）。
_BOILERPLATE = (
    "这里记录每周值得分享",
    "本杂志",
    "欢迎投稿",
    "欢迎 投稿",
    "《谁在招人》",
    "封面图",
)

# 周刊的固定版式：两句开场套话 → 「封面图」小节（一张图 + 一句图注）→ 正文第一段。
# 图注有七八十字，长度门槛拦不住，会直接顶掉真正的正文首段。
_COVER_HEADING = "封面图"

# 图注的形态是「上图是……（ via ）」。用**结尾**的 via 判定，比用开头稳：
# 他也写「上图为」，也有整句不提「上图」只留下 via 的。实测 390 篇里
# 有 126 篇的「摘要」其实是图注，就是这个原因。
_CAPTION_RE = re.compile(r"^(上图|题图)|[（(]\s*via\s*[）)]\s*$", re.IGNORECASE)


def derive_summary(body: str, title: str) -> str | None:
    """原样摘取正文第一段，不改写。

    CC BY-NC-ND 的 ND 是「禁止演绎」。机器提炼、改写、压缩都算演绎，
    所以这里只做一件事：跳过固定套话和封面图注，找第一个有信息量的段落，
    原样切出来。

    注意长度门槛是 60 而不是 20：周刊的当期小标题（如「机房来了，写字楼走了」）
    只有十几个字，拿它当摘要等于什么都没说。
    """

    skip_next = False
    for block in re.split(r"\n+", body):
        block = block.strip()
        if not block:
            continue
        if block == _COVER_HEADING:
            # 「封面图」是个小节标题，后面紧跟一行图注。两行都不是正文。
            skip_next = True
            continue
        if skip_next:
            skip_next = False
            continue
        if len(block) < 60:
            continue
        if block == title:
            continue
        if any(marker in block for marker in _BOILERPLATE):
            continue
        if _CAPTION_RE.search(block):
            continue
        if len(block) <= 300:
            return block
        return block[:299].rstrip() + "…"
    return None


async def collect_links(client: httpx.AsyncClient, year: int, month: int) -> list[str]:
    """取某个月度归档页里的周刊链接。"""

    url = f"https://www.ruanyifeng.com/blog/{year}/{month:02d}/"
    try:
        response = await client.get(url)
        if response.status_code != 200:
            return []
    except httpx.HTTPError:
        return []

    links = re.findall(r'href="(https://www\.ruanyifeng\.com/blog/\d{4}/\d{2}/weekly-issue-\d+\.html)"', response.text)
    # 同一篇文章在列表页里会出现多次（标题、评论数各一个链接）
    return sorted(set(links))


def rederive_summaries(conn) -> int:
    """只用库里已存的正文重算摘要，不重新抓站。

    改摘要规则时用它。摘要变了，`content_hash` 和 `topics` 都要跟着重算——
    前者把摘要算进去了，后者本来就吃 title + summary，不动这两列的话
    摘要换了、指纹和分类还是旧的，库就自相矛盾了。
    """

    cur = conn.cursor()
    cur.execute("select id from sources where source_key = %s", (SOURCE_KEY,))
    row = cur.fetchone()
    if row is None:
        print(f"找不到来源 {SOURCE_KEY}，先跑 make seed", file=sys.stderr)
        return 2
    source_id = row[0]

    cur.execute(
        "select id, title, summary, content_text, canonical_url from documents where source_id = %s",
        (source_id,),
    )
    rows = cur.fetchall()

    changed = 0
    for doc_id, title, old_summary, body, url in rows:
        new_summary = derive_summary(body or "", title or "")
        if new_summary == old_summary:
            continue
        cur.execute(
            """
            update documents
               set summary = %s, content_hash = %s, topics = %s
             where id = %s
            """,
            (
                new_summary,
                content_hash(title or "", url, new_summary),
                Json(classify_topics(title or "", new_summary)),
                doc_id,
            ),
        )
        changed += 1
        if changed <= 5:
            print(f"  [{title[:28]}]")
            print(f"    旧：{(old_summary or '（空）')[:64]}")
            print(f"    新：{(new_summary or '（空）')[:64]}", flush=True)

    conn.commit()
    print(f"\n重算完成：{len(rows)} 篇，摘要变更 {changed} 篇")
    return 0


async def main() -> int:
    if "--summaries-only" in sys.argv:
        conn = psycopg2.connect(**DB)
        try:
            return rederive_summaries(conn)
        finally:
            conn.close()
    return await backfill()


async def backfill() -> int:
    conn = psycopg2.connect(**DB)
    cur = conn.cursor()
    cur.execute("select id from sources where source_key = %s", (SOURCE_KEY,))
    row = cur.fetchone()
    if row is None:
        print(f"找不到来源 {SOURCE_KEY}，先跑 make seed", file=sys.stderr)
        return 2
    source_id = row[0]

    inserted = skipped = failed = 0
    async with httpx.AsyncClient(headers=UA, timeout=25, follow_redirects=True) as client:
        for year, month in months(START, END):
            links = await collect_links(client, year, month)
            await asyncio.sleep(DELAY)
            if not links:
                continue

            for link in links:
                canonical = canonicalize_url(link)
                # 判重不能只比 external_id：RSS 路径拿的是 Atom 的 id
                # （tag:www.ruanyifeng.com,2026:/blog//1.2563），回填只能拿 URL，
                # 两者形态不同但指向同一篇文章。所以还要按「去掉 scheme 的 URL」
                # 再比一次——feed 里给的是 http，归档页给的是 https。
                cur.execute(
                    """
                    select 1 from documents
                     where source_id = %s
                       and (external_id = %s
                            or regexp_replace(canonical_url, '^https?://', '')
                               = regexp_replace(%s, '^https?://', ''))
                    """,
                    (source_id, canonical, canonical),
                )
                if cur.fetchone():
                    skipped += 1
                    continue

                try:
                    response = await client.get(link)
                    await asyncio.sleep(DELAY)
                    if response.status_code != 200:
                        failed += 1
                        continue
                    article = extract_article(response.text, link)
                    if article is None:
                        failed += 1
                        continue
                except httpx.HTTPError:
                    failed += 1
                    continue

                summary = derive_summary(article["body"], article["title"])
                cur.execute(
                    """
                    insert into documents
                        (source_id, external_id, canonical_url, title, summary, content_text,
                         language, region, content_type, published_at, fetched_at,
                         content_hash, simhash, topics, rights_mode, content_status,
                         quality_flags)
                    values
                        (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    on conflict (source_id, external_id) do nothing
                    """,
                    (
                        source_id,
                        canonical,
                        canonical,
                        article["title"],
                        summary,
                        article["body"],
                        "zh",
                        "cn",
                        "article",
                        article["published_at"] or datetime.now(UTC),
                        datetime.now(UTC),
                        content_hash(article["title"], canonical, summary),
                        simhash(article["body"]),
                        # topics / quality_flags 是 jsonb，裸 list 会被当成 text[]
                        Json(classify_topics(article["title"], summary)),
                        "cc_by_nc_nd",
                        "from_feed",
                        Json([]),
                    ),
                )
                inserted += 1
                if inserted % 20 == 0:
                    conn.commit()
                    print(f"  已入库 {inserted} 篇（当前进度 {year}-{month:02d}）", flush=True)

    conn.commit()
    print(f"\n回填完成：新增 {inserted}，已存在 {skipped}，失败 {failed}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
