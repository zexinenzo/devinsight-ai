import { Suspense } from "react";

import { ApiError, formatDateTime, formatRelative, listSources, type Source } from "@/lib/api";

const HEALTH_STYLE: Record<string, { dot: string; label: string }> = {
  healthy: { dot: "bg-emerald-500", label: "正常" },
  degraded: { dot: "bg-amber-500", label: "偶发失败" },
  failing: { dot: "bg-red-500", label: "连续失败" },
  unknown: { dot: "bg-zinc-400", label: "未探测" },
  disabled: { dot: "bg-zinc-300", label: "已停用" },
};

function HealthBadge({ status, enabled }: { status: string; enabled: boolean }) {
  // 停用的来源后端不探测，health_status 会一直是 unknown；
  // 直接显示「未探测」会被误读成异常，这里优先表达"是停用而不是坏了"
  const key = !enabled && status === "unknown" ? "disabled" : status;
  const style = HEALTH_STYLE[key] ?? HEALTH_STYLE.unknown;
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={`size-2 rounded-full ${style.dot}`} />
      <span>{style.label}</span>
    </span>
  );
}

function RunCell({ source }: { source: Source }) {
  const run = source.last_run;
  if (!run) return <span className="text-zinc-400">尚未采集</span>;
  if (run.status === "failed") {
    return <span className="text-red-600 dark:text-red-400">失败</span>;
  }
  return (
    <span>
      新增 <span className="font-medium">{run.created_count}</span> / 抓取 {run.fetched_count}
    </span>
  );
}

const STAT_CARDS = ["已注册来源", "已启用", "健康", "连续失败"] as const;

/** 取数区，必须落在 Suspense 内（cacheComponents 的运行时数据约束），理由同情报流页面 */
async function SourceBoard() {
  let sources: Source[] = [];
  let errorMessage: string | null = null;

  try {
    sources = await listSources();
  } catch (error) {
    errorMessage = error instanceof ApiError ? error.message : "加载来源失败";
  }

  const enabled = sources.filter((source) => source.enabled);
  const healthy = sources.filter((source) => source.health_status === "healthy");
  const failing = sources.filter((source) => source.health_status === "failing");
  const stats = [sources.length, enabled.length, healthy.length, failing.length];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">数据源</h1>
        <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
          来源状态由运行时健康检查决定，不在代码里硬编码为“可用”。
        </p>
      </div>

      {errorMessage ? (
        <div className="rounded-xl border border-amber-300 bg-amber-50 p-5 text-sm text-amber-800 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200">
          {errorMessage}
        </div>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            {STAT_CARDS.map((label, index) => (
              <div
                key={label}
                className="rounded-xl border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900"
              >
                <div className="text-xs text-zinc-500 dark:text-zinc-400">{label}</div>
                <div className="mt-1 text-2xl font-semibold tabular-nums">{stats[index]}</div>
              </div>
            ))}
          </div>

          <div className="overflow-x-auto rounded-xl border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900">
            <table className="w-full text-left text-sm">
              <thead className="border-b border-zinc-200 text-xs uppercase tracking-wide text-zinc-500 dark:border-zinc-800 dark:text-zinc-400">
                <tr>
                  <th className="px-4 py-3 font-medium">来源</th>
                  <th className="px-4 py-3 font-medium">类型 / 地区</th>
                  <th className="px-4 py-3 font-medium">健康</th>
                  <th className="px-4 py-3 font-medium">间隔</th>
                  <th className="px-4 py-3 font-medium">上次成功</th>
                  <th className="px-4 py-3 font-medium">最近一批</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800">
                {sources.map((source) => (
                  <tr key={source.id} className={source.enabled ? "" : "opacity-60"}>
                    <td className="px-4 py-3">
                      <div className="font-medium">{source.name}</div>
                      <a
                        href={source.base_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-xs text-zinc-500 hover:underline dark:text-zinc-400"
                      >
                        {source.canonical_domain}
                      </a>
                    </td>
                    <td className="px-4 py-3 text-zinc-600 dark:text-zinc-400">
                      <div>
                        {source.source_type} · {source.region === "cn" ? "中国" : "全球"}
                      </div>
                      <div className="text-xs">分层 {source.credibility_tier}</div>
                    </td>
                    <td className="px-4 py-3">
                      <HealthBadge status={source.health_status} enabled={source.enabled} />
                      {source.consecutive_failures > 0 && (
                        <div className="mt-1 text-xs text-red-600 dark:text-red-400">
                          连续失败 {source.consecutive_failures} 次
                        </div>
                      )}
                    </td>
                    <td className="px-4 py-3 tabular-nums text-zinc-600 dark:text-zinc-400">
                      {source.poll_interval_minutes} 分钟
                    </td>
                    <td className="px-4 py-3 text-zinc-600 dark:text-zinc-400">
                      <div>{source.last_success_at ? formatRelative(source.last_success_at) : "从未"}</div>
                      <div className="text-xs">{formatDateTime(source.last_success_at)}</div>
                    </td>
                    <td className="px-4 py-3 text-zinc-600 dark:text-zinc-400">
                      <RunCell source={source} />
                      {source.last_run?.error && (
                        <div className="mt-1 max-w-56 truncate text-xs text-red-600 dark:text-red-400">
                          {source.last_run.error}
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

function SourceSkeleton() {
  return (
    <div className="space-y-6" aria-busy="true" aria-label="正在加载数据源">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">数据源</h1>
        <p className="mt-1 text-sm text-zinc-400">正在加载…</p>
      </div>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {STAT_CARDS.map((label) => (
          <div
            key={label}
            className="rounded-xl border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900"
          >
            <div className="text-xs text-zinc-500 dark:text-zinc-400">{label}</div>
            <div className="mt-1 h-7 w-10 animate-pulse rounded bg-zinc-200 dark:bg-zinc-800" />
          </div>
        ))}
      </div>
      <div className="space-y-2 rounded-xl border border-zinc-200 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
        {Array.from({ length: 5 }).map((_, index) => (
          <div key={index} className="h-10 animate-pulse rounded bg-zinc-100 dark:bg-zinc-800/60" />
        ))}
      </div>
    </div>
  );
}

export default function SourcesPage() {
  return (
    <Suspense fallback={<SourceSkeleton />}>
      <SourceBoard />
    </Suspense>
  );
}
