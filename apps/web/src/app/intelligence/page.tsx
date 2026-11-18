import Link from "next/link";
import { Suspense } from "react";

import {
  ApiError,
  formatDateTime,
  formatRelative,
  listContentTypes,
  listDocuments,
  listSources,
  listTopics,
  type ContentType,
  type DocumentListItem,
  type Source,
  type Topic,
} from "@/lib/api";

type SearchParams = Promise<Record<string, string | string[] | undefined>>;

/** 来源可信度分层（手册 3.3）——列表里必须让人一眼看出这是几手信息 */
const TIER_STYLE: Record<string, { label: string; className: string }> = {
  A: { label: "A 一手/权威", className: "bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300" },
  B: { label: "B 专业媒体", className: "bg-sky-50 text-sky-700 dark:bg-sky-950 dark:text-sky-300" },
  C: { label: "C 社区线索", className: "bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-300" },
  D: { label: "D 模型推断", className: "bg-violet-50 text-violet-700 dark:bg-violet-950 dark:text-violet-300" },
};

/**
 * 内容类型标识。只给非默认类型加徽章 —— 新闻（article）占 500 多条，
 * 每条都挂一个「文章」标签等于没有信息量；论文和发布说明才是需要一眼区分的。
 */
const CONTENT_TYPE_STYLE: Record<string, { label: string; className: string }> = {
  paper: { label: "论文", className: "bg-violet-50 text-violet-700 dark:bg-violet-950 dark:text-violet-300" },
  release: { label: "发布说明", className: "bg-blue-50 text-blue-700 dark:bg-blue-950 dark:text-blue-300" },
};

/** 质量标记的可读化。原始值是代码约定（snake_case），直接摆给用户看不懂。 */
const FLAG_LABELS: Record<string, string> = {
  missing_summary: "缺摘要",
  missing_published_at: "缺发布时间",
  implausible_published_at: "发布时间可疑",
  missing_url: "缺原文链接",
  self_post: "自帖",
  prerelease: "预发布",
};

function flagLabel(flag: string): string {
  return FLAG_LABELS[flag] ?? flag;
}

function buildHref(params: Record<string, string | undefined>, patch: Record<string, string | undefined>) {
  const merged = new URLSearchParams();
  for (const [key, value] of Object.entries({ ...params, ...patch })) {
    if (value) merged.set(key, value);
  }
  const query = merged.toString();
  return query ? `/intelligence?${query}` : "/intelligence";
}

function pick(raw: Record<string, string | string[] | undefined>, key: string): string | undefined {
  const value = raw[key];
  return typeof value === "string" && value ? value : undefined;
}

function DocumentCard({ document }: { document: DocumentListItem }) {
  const tier = TIER_STYLE[document.source.credibility_tier] ?? TIER_STYLE.B;
  const contentType = CONTENT_TYPE_STYLE[document.content_type];
  const isPaper = document.content_type === "paper";
  const flags = document.quality_flags.map(flagLabel);
  const visibleFlags = flags.slice(0, 2);
  const hiddenFlagCount = flags.length - visibleFlags.length;

  return (
    <article className="rounded-xl border border-zinc-200 bg-white p-5 transition hover:border-zinc-300 dark:border-zinc-800 dark:bg-zinc-900 dark:hover:border-zinc-700">
      <div className="mb-2 flex flex-wrap items-center gap-2 text-xs">
        {contentType && (
          <span className={`rounded px-1.5 py-0.5 font-medium ${contentType.className}`}>{contentType.label}</span>
        )}
        <span className="font-medium text-zinc-700 dark:text-zinc-300">{document.source.name}</span>
        <span className={`rounded px-1.5 py-0.5 font-medium ${tier.className}`}>{tier.label}</span>
        <span className="text-zinc-400">{document.region === "cn" ? "中国" : "全球"}</span>
        {document.language && <span className="uppercase text-zinc-400">{document.language}</span>}
        {visibleFlags.map((flag) => (
          <span
            key={flag}
            className="rounded bg-amber-50 px-1.5 py-0.5 text-amber-700 dark:bg-amber-950 dark:text-amber-300"
          >
            {flag}
          </span>
        ))}
        {hiddenFlagCount > 0 && (
          <span
            className="rounded bg-zinc-100 px-1.5 py-0.5 text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400"
            title={flags.join("、")}
          >
            +{hiddenFlagCount}
          </span>
        )}
      </div>

      <h2 className={`text-base font-semibold tracking-tight ${isPaper ? "leading-7" : "leading-6"}`}>
        <a
          href={document.original_url ?? document.canonical_url}
          target="_blank"
          rel="noopener noreferrer"
          className="hover:underline"
        >
          {document.title}
        </a>
      </h2>

      {document.summary ? (
        <p className={`mt-2 text-sm leading-6 text-zinc-600 dark:text-zinc-400 ${isPaper ? "line-clamp-3" : "line-clamp-2"}`}>
          {document.summary}
        </p>
      ) : (
        <p className="mt-2 text-sm italic leading-6 text-zinc-400 dark:text-zinc-500">
          原文未提供摘要 —— 正文需抓取原文后补全
        </p>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-zinc-500 dark:text-zinc-500">
        <span>发布 {document.published_at ? formatDateTime(document.published_at) : "未知"}</span>
        <span>
          采集 {formatDateTime(document.fetched_at)}（{formatRelative(document.fetched_at)}）
        </span>
        {document.topics.map((topic) => (
          <Link
            key={topic}
            href={`/intelligence?topic=${encodeURIComponent(topic)}`}
            className="rounded bg-zinc-100 px-1.5 py-0.5 text-zinc-600 hover:bg-zinc-200 dark:bg-zinc-800 dark:text-zinc-300 dark:hover:bg-zinc-700"
          >
            {topic}
          </Link>
        ))}
      </div>
    </article>
  );
}

/**
 * 数据区。await searchParams 与三次取数都必须落在这个 Suspense 边界内：
 * Next 16 开启 cacheComponents 后，运行时数据（searchParams、no-store fetch）
 * 出现在边界外会让整个路由无法预渲染，构建直接报 blocking-prerender-dynamic。
 */
async function IntelligenceBoard({ searchParams }: { searchParams: SearchParams }) {
  const raw = await searchParams;

  const filters = {
    q: pick(raw, "q"),
    source: pick(raw, "source"),
    region: pick(raw, "region"),
    topic: pick(raw, "topic"),
    content_type: pick(raw, "content_type"),
  };

  let items: DocumentListItem[] = [];
  let total = 0;
  let nextCursor: string | null = null;
  let sources: Source[] = [];
  let topics: Topic[] = [];
  let contentTypes: ContentType[] = [];
  let errorMessage: string | null = null;

  try {
    const [page, sourceList, topicList, typeList] = await Promise.all([
      listDocuments({ ...filters, limit: 20, cursor: pick(raw, "cursor") }),
      listSources(),
      listTopics(),
      listContentTypes(),
    ]);
    items = page.items;
    total = page.total;
    nextCursor = page.nextCursor;
    sources = sourceList;
    topics = topicList;
    contentTypes = typeList;
  } catch (error) {
    errorMessage = error instanceof ApiError ? error.message : "加载情报失败";
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">情报流</h1>
          <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
            {errorMessage ? "后端未就绪" : `共 ${total} 条，按发布时间倒序（无发布时间按采集时间排序）`}
          </p>
        </div>
        <form method="get" className="flex flex-wrap items-center gap-2 text-sm">
          <input
            type="search"
            name="q"
            defaultValue={filters.q ?? ""}
            placeholder="标题或摘要关键词"
            className="h-9 w-52 rounded-md border border-zinc-300 bg-white px-3 outline-none focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-900"
          />
          <select
            name="source"
            defaultValue={filters.source ?? ""}
            className="h-9 rounded-md border border-zinc-300 bg-white px-2 dark:border-zinc-700 dark:bg-zinc-900"
          >
            <option value="">全部来源</option>
            {sources.map((source) => (
              <option key={source.id} value={source.source_key}>
                {source.name}
              </option>
            ))}
          </select>
          <select
            name="region"
            defaultValue={filters.region ?? ""}
            className="h-9 rounded-md border border-zinc-300 bg-white px-2 dark:border-zinc-700 dark:bg-zinc-900"
          >
            <option value="">全部地区</option>
            <option value="cn">中国</option>
            <option value="global">全球</option>
          </select>
          <select
            name="content_type"
            defaultValue={filters.content_type ?? ""}
            className="h-9 rounded-md border border-zinc-300 bg-white px-2 dark:border-zinc-700 dark:bg-zinc-900"
          >
            <option value="">全部类型</option>
            {contentTypes.map((type) => (
              <option key={type.value} value={type.value}>
                {type.label}（{type.count}）
              </option>
            ))}
          </select>
          <select
            name="topic"
            defaultValue={filters.topic ?? ""}
            className="h-9 rounded-md border border-zinc-300 bg-white px-2 dark:border-zinc-700 dark:bg-zinc-900"
          >
            <option value="">全部领域</option>
            {topics.map((topic) => (
              <option key={topic.slug} value={topic.slug}>
                {topic.label}
              </option>
            ))}
          </select>
          <button
            type="submit"
            className="h-9 rounded-md bg-zinc-900 px-3 font-medium text-white transition hover:bg-zinc-700 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-300"
          >
            筛选
          </button>
          <Link
            href="/intelligence"
            className="h-9 rounded-md border border-zinc-300 px-3 leading-9 text-zinc-600 dark:border-zinc-700 dark:text-zinc-400"
          >
            重置
          </Link>
        </form>
      </div>

      {errorMessage && (
        <div className="rounded-xl border border-amber-300 bg-amber-50 p-5 text-sm text-amber-800 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
          <p className="font-medium">{errorMessage}</p>
          <p className="mt-2 text-amber-700 dark:text-amber-300">
            在仓库根目录执行 <code className="rounded bg-amber-100 px-1 dark:bg-amber-900">make api</code> 后刷新本页。
          </p>
        </div>
      )}

      {!errorMessage && items.length === 0 && (
        <div className="rounded-xl border border-dashed border-zinc-300 p-10 text-center text-sm text-zinc-500 dark:border-zinc-700">
          <p className="font-medium text-zinc-700 dark:text-zinc-300">还没有情报</p>
          <p className="mt-2">
            执行 <code className="rounded bg-zinc-100 px-1 dark:bg-zinc-800">make sync</code> 采集一轮，数据会自动出现在这里。
          </p>
        </div>
      )}

      <div className="space-y-3">
        {items.map((document) => (
          <DocumentCard key={document.id} document={document} />
        ))}
      </div>

      {nextCursor && (
        <div className="flex justify-center pt-2">
          <Link
            href={buildHref(filters, { cursor: nextCursor })}
            className="rounded-md border border-zinc-300 px-4 py-2 text-sm hover:bg-zinc-100 dark:border-zinc-700 dark:hover:bg-zinc-800"
          >
            下一页
          </Link>
        </div>
      )}
    </div>
  );
}

/** 骨架：与真实布局同尺寸，避免数据到达时页面跳动 */
function BoardSkeleton() {
  return (
    <div className="space-y-6" aria-busy="true" aria-label="正在加载情报">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">情报流</h1>
          <p className="mt-1 text-sm text-zinc-400">正在加载…</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <div className="h-9 w-52 animate-pulse rounded-md bg-zinc-200 dark:bg-zinc-800" />
          <div className="h-9 w-28 animate-pulse rounded-md bg-zinc-200 dark:bg-zinc-800" />
          <div className="h-9 w-24 animate-pulse rounded-md bg-zinc-200 dark:bg-zinc-800" />
          <div className="h-9 w-24 animate-pulse rounded-md bg-zinc-200 dark:bg-zinc-800" />
        </div>
      </div>
      <div className="space-y-3">
        {Array.from({ length: 5 }).map((_, index) => (
          <div
            key={index}
            className="rounded-xl border border-zinc-200 bg-white p-5 dark:border-zinc-800 dark:bg-zinc-900"
          >
            <div className="mb-3 h-3 w-40 animate-pulse rounded bg-zinc-200 dark:bg-zinc-800" />
            <div className="h-4 w-3/4 animate-pulse rounded bg-zinc-200 dark:bg-zinc-800" />
            <div className="mt-3 h-3 w-1/2 animate-pulse rounded bg-zinc-100 dark:bg-zinc-800/60" />
          </div>
        ))}
      </div>
    </div>
  );
}

export default function IntelligencePage({ searchParams }: { searchParams: SearchParams }) {
  return (
    <Suspense fallback={<BoardSkeleton />}>
      <IntelligenceBoard searchParams={searchParams} />
    </Suspense>
  );
}
