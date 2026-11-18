/**
 * 后端 API 客户端（只在服务端运行）。
 *
 * 约定：后端统一返回 { data, meta, error }；这里把错误收敛成异常，
 * 由页面各自决定是展示空态还是错误提示。
 */

const API_BASE = process.env.API_BASE_URL ?? "http://localhost:8000";

export type Envelope<T> = {
  data: T | null;
  meta: {
    request_id: string;
    timestamp: string;
    total: number | null;
    next_cursor: string | null;
  };
  error: { code: string; message: string } | null;
};

export type SourceBrief = {
  id: string;
  source_key: string;
  name: string;
  source_type: string;
  credibility_tier: string;
  region: string;
};

export type DocumentListItem = {
  id: string;
  title: string;
  summary: string | null;
  canonical_url: string;
  original_url: string | null;
  language: string | null;
  region: string;
  topics: string[];
  content_type: string;
  published_at: string | null;
  fetched_at: string;
  rights_mode: string;
  quality_flags: string[];
  source: SourceBrief;
};

export type IngestionRun = {
  id: string;
  started_at: string;
  finished_at: string | null;
  status: string;
  fetched_count: number;
  created_count: number;
  skipped_count: number;
  http_status: number | null;
  duration_ms: number | null;
  error: string | null;
  triggered_by: string;
};

export type Source = {
  id: string;
  source_key: string;
  name: string;
  canonical_domain: string;
  source_type: string;
  region: string;
  language: string | null;
  access_method: string;
  base_url: string;
  credibility_tier: string;
  rights_note: string | null;
  enabled: boolean;
  priority: number;
  poll_interval_minutes: number;
  health_status: string;
  last_success_at: string | null;
  last_failure_at: string | null;
  consecutive_failures: number;
  last_http_status: number | null;
  parser_version: string;
  last_run: IngestionRun | null;
};

export type Topic = { slug: string; label: string };

/** 内容类型分布。只包含库里真实存在的类型，避免筛选器出现点进去是空的选项。 */
export type ContentType = { value: string; label: string; count: number };

export class ApiError extends Error {
  code: string;
  constructor(code: string, message: string) {
    super(message);
    this.code = code;
  }
}

async function request<T>(path: string): Promise<Envelope<T>> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/api/v1${path}`, {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
  } catch {
    throw new ApiError("api_unreachable", `无法连接后端 ${API_BASE}，请确认 uvicorn 已启动`);
  }

  let body: Envelope<T>;
  try {
    body = (await response.json()) as Envelope<T>;
  } catch {
    throw new ApiError("invalid_response", `后端返回了非 JSON 响应（HTTP ${response.status}）`);
  }

  if (body.error) {
    throw new ApiError(body.error.code, body.error.message);
  }
  if (!response.ok) {
    throw new ApiError(`http_${response.status}`, `请求失败（HTTP ${response.status}）`);
  }
  return body;
}

export type DocumentQuery = {
  q?: string;
  source?: string;
  region?: string;
  topic?: string;
  content_type?: string;
  limit?: number;
  cursor?: string;
};

export async function listDocuments(query: DocumentQuery) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== "") params.set(key, String(value));
  }
  const envelope = await request<DocumentListItem[]>(`/documents?${params.toString()}`);
  return {
    items: envelope.data ?? [],
    total: envelope.meta.total ?? 0,
    nextCursor: envelope.meta.next_cursor,
  };
}

export async function listSources(): Promise<Source[]> {
  const envelope = await request<Source[]>("/sources");
  return envelope.data ?? [];
}

export async function listTopics(): Promise<Topic[]> {
  const envelope = await request<Topic[]>("/meta/topics");
  return envelope.data ?? [];
}

export async function listContentTypes(): Promise<ContentType[]> {
  const envelope = await request<ContentType[]>("/meta/content-types");
  return envelope.data ?? [];
}

export function formatDateTime(value: string | null): string {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

export function formatRelative(value: string | null): string {
  if (!value) return "从未";
  const diffMs = Date.now() - new Date(value).getTime();
  const minutes = Math.round(diffMs / 60000);
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.round(hours / 24);
  return `${days} 天前`;
}
