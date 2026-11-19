"""应用配置。

配置来源优先级：环境变量 > 仓库根目录 `.env` > 代码默认值。
`.env` 不入库（见 .gitignore），`.env.example` 只放占位符。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# apps/api/app/core/config.py -> apps/api
API_DIR = Path(__file__).resolve().parents[2]


def _find_repo_root(start: Path) -> Path:
    """向上查找仓库根目录（以 docker-compose.yml 或 .env 为标志）。"""

    for candidate in (start, *start.parents):
        if (candidate / "docker-compose.yml").exists() or (candidate / ".env").exists():
            return candidate
    return start


REPO_ROOT = _find_repo_root(API_DIR)


class Settings(BaseSettings):
    """全局配置对象，在进程内单例缓存。"""

    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", API_DIR / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- 运行环境 ---
    app_env: str = "local"
    app_name: str = "DevInsightAI"
    api_base_url: str = "http://localhost:8000"

    # --- 数据存储 ---
    database_url: str = "postgresql+asyncpg://devinsight:change_me_dev@localhost:5432/devinsight"
    redis_url: str = "redis://localhost:6379/0"

    # --- 模型与搜索 ---
    llm_provider: str = "openai"
    llm_api_key: str = ""
    llm_model: str = ""
    # 向量化只走本地模型：把全文发给第三方 API 做嵌入，法律性质会从「我们自己存储」
    # 变成「向第三方提供作品」，等于绕过了 rights_mode 那道闸门。
    #
    # onnx = 本地 ONNX 推理；hashing = 字符哈希假向量（只用于测试/离线演示）；
    # disabled = 彻底关掉，检索退回关键词。
    embedding_backend: str = "onnx"
    # 必须能覆盖中英混排：语料是 981 英 / 450 中，纯中文模型嵌英文会失真
    embedding_model: str = "intfloat/multilingual-e5-small"
    # 这个数字必须和模型输出维度一致，建表时写进了 vector(N)，改模型要重跑迁移。
    # 曾经这里是 1536（OpenAI 的量纲），和本地模型对不上，必须先改掉再建表。
    embedding_dimensions: int = 384
    # 留空则用 ~/.cache/devinsight/models/<模型名>，模型文件不进仓库
    embedding_model_dir: str = ""
    # 重排（cross-encoder）。把 query 和候选文档拼成一条句子送进模型，
    # 直接输出相关性分数——它能看到 query，所以比向量点积准得多，
    # 代价是不能预计算：每篇候选每次查询都要过一遍模型。
    #
    # onnx = 本地 ONNX 推理；disabled = 不重排，直接用 RRF 顺序。
    rerank_backend: str = "onnx"
    rerank_model: str = "Xenova/bge-reranker-base"
    rerank_model_dir: str = ""
    # 只重排前多少条候选。40 条 × 约 25ms ≈ 1 秒，这是「值得为一个查询多等一秒」
    # 的量级上限；再往上加，用户感知到的是延迟而不是准确率。
    rerank_candidates: int = 40
    # --- 实时搜索（阶段 5）---
    # provider 留空 = 能力关闭，/evidence 只返回本地证据，不报错；
    # 调用方显式要求 include_web 时才 503（与 rerank 同一套「缺能力要明说」的约定）。
    #
    # tavily_keyless = 不带 key、加 X-Tavily-Access-Mode: keyless 头，约 30 次/天
    # （上游 429 的错误码是 daily_cap_reached，是每日不是每小时）。
    # 它的响应 schema 与带 key 完全相同，所以「先用 keyless 跑通、后面有 key 直接换」
    # 不需要改任何解析代码——这条是实测确认过的，不是文档抄的。
    web_search_provider: str = ""
    web_search_api_key: str = ""
    web_search_base_url: str = "https://api.tavily.com"
    web_search_keyless: bool = True
    # news 模式才返回 published_date。general 模式按相关性排，实测会把 2023 年的
    # 旧闻排到第一——所以默认走 news，拿日期比拿覆盖面重要。
    web_search_topic: str = "news"
    # 时间窗（天）。news 模式下这是硬过滤；超出 1~30 会被服务端拒绝。
    web_search_days: int = 7
    # 一次给 Agent 的证据条数上限。不是越大越好：N 条 web 结果意味着 N 个
    # 待验证的域名，而模型能可靠引用的通常不到 10 条。
    web_search_max_results: int = 8
    github_token: str = ""
    huggingface_token: str = ""

    # --- 安全 ---
    secret_key: str = "replace_with_long_random_secret"
    allowed_origins: str = "http://localhost:3000"

    # --- 采集行为 ---
    default_timezone: str = "Asia/Shanghai"
    default_locale: str = "zh-CN"
    http_timeout_seconds: float = 20.0
    http_user_agent: str = "DevInsightAI/0.1 (+contact: admin@example.com)"
    max_items_per_sync: int = 200
    summary_max_chars: int = 600
    log_level: str = "INFO"

    # --- 后续阶段预算占位 ---
    max_research_tool_calls: int = 12
    max_web_search_calls: int = 4
    max_agent_runtime_seconds: int = 180

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, value: str) -> str:
        """强制使用 asyncpg 驱动，避免误用同步 URL 导致运行时才报错。"""

        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+asyncpg://", 1)
        if value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql+asyncpg://", 1)
        return value

    @property
    def cors_origins(self) -> list[str]:
        """把逗号分隔的 ALLOWED_ORIGINS 解析为列表。"""

        return [item.strip() for item in self.allowed_origins.split(",") if item.strip()]

    @property
    def sync_database_url(self) -> str:
        """同步驱动 URL，供 Alembic 与一次性脚本使用。"""

        return self.database_url.replace("+asyncpg", "+psycopg")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
