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

    # --- 模型与搜索（后续阶段使用，此处先留占位）---
    llm_provider: str = "openai"
    llm_api_key: str = ""
    llm_model: str = ""
    embedding_model: str = ""
    embedding_dimensions: int = 1536
    web_search_provider: str = ""
    web_search_api_key: str = ""
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
