"""全局配置：环境变量的唯一事实来源。

约定（见 CLAUDE.md）：
- 分段字段（HOST/PORT/USER/PASSWORD/DB）是事实来源，连接串一律由本模块**计算**得出，
  禁止在 .env 里重复手写 ``*_URL``，避免两处不一致。
- 所有敏感值用 ``SecretStr`` 承载，防止被日志/异常/``repr`` 意外打印。
- ``APP_ENV=prod`` 时强制自检：拒绝默认密码、拒绝 DEBUG、拒绝空密钥。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from models.enums import Env

# .env 位置：优先 backend/.env，其次仓库根 .env（compose 用根目录那份）
_ENV_FILES = (
    Path(__file__).resolve().parent.parent / ".env",
    Path(__file__).resolve().parent.parent.parent / ".env",
)

# 生产环境禁止出现的占位值前缀
_PLACEHOLDER_PREFIXES = ("change-me", "changeme", "your-", "sk-your")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILES,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------------- 运行环境 ----------------
    # 取值复用领域枚举 Env（dev | staging | prod），**不另立一套词汇**：
    # 否则 settings.APP_ENV == "prod" 与 Env.PROD == "prod" 两处各写各的，迟早对不上。
    APP_NAME: str = "elt_agent"
    APP_ENV: Env = Env.DEV
    APP_DEBUG: bool = True
    API_V1_PREFIX: str = "/api/v1"
    SECRET_KEY: SecretStr = SecretStr("dev-only-secret-key-not-for-production")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    CORS_ORIGINS: list[str] = Field(default_factory=list)

    # ---------------- 登录防护 ----------------
    # 计数维度是 (租户, 用户名)，**不是** IP：IP 维度由 Nginx limit_req 承担。
    # 键必须含租户（见 core/redis.py 的 KEY_LOGIN_FAIL），否则一个租户内的失败会锁死其他租户的同名账号。
    LOGIN_FAIL_MAX_ATTEMPTS: int = 5
    LOGIN_FAIL_WINDOW_SEC: int = 900

    # ---------------- 幂等 ----------------
    # 幂等键保留多久。取值是权衡：太短则客户端重试窗口过了就失效（重复执行），
    # 太长则 idempotency_key 表无限膨胀。24h 覆盖「用户看到失败后重试」的全部现实场景。
    IDEMPOTENCY_TTL_HOURS: int = 24

    # ---------------- 对外访问 ----------------
    HTTP_PORT: int = 80
    SERVER_HOST: str = "127.0.0.1"

    # ---------------- PostgreSQL（控制面库 + LangGraph Checkpoint）----------------
    POSTGRES_HOST: str = "postgres"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "elt"
    POSTGRES_PASSWORD: SecretStr = SecretStr("elt")
    POSTGRES_DB: str = "elt_agent"
    POSTGRES_POOL_SIZE: int = 10
    POSTGRES_MAX_OVERFLOW: int = 20
    POSTGRES_ECHO: bool = False

    # ---------------- Redis ----------------
    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: SecretStr = SecretStr("")
    REDIS_DB_CACHE: int = 0
    REDIS_DB_BROKER: int = 1
    REDIS_DB_RESULT: int = 2

    # ---------------- Celery / Outbox ----------------
    CELERY_WORKER_CONCURRENCY: int = 4
    CELERY_TASK_TIME_LIMIT: int = 7200
    CELERY_TASK_SOFT_TIME_LIMIT: int = 6900
    OUTBOX_SCAN_INTERVAL_SEC: int = 2
    OUTBOX_MAX_ATTEMPTS: int = 5

    # ---------------- MinIO ----------------
    MINIO_ENDPOINT: str = ""  # 留空则按 MINIO_HOST/PORT 计算
    MINIO_HOST: str = "minio"
    MINIO_PORT: int = 9000
    MINIO_SECURE: bool = False
    MINIO_ROOT_USER: str = "elt-minio"
    MINIO_ROOT_PASSWORD: SecretStr = SecretStr("")
    MINIO_BUCKET: str = "elt-artifacts"

    # ---------------- Vault ----------------
    VAULT_ADDR: str = "http://vault:8200"
    VAULT_TOKEN: SecretStr = SecretStr("")
    VAULT_KV_MOUNT: str = "kv"
    VAULT_ROLE_ID: str = ""
    VAULT_SECRET_ID: SecretStr = SecretStr("")
    VAULT_TIMEOUT_SEC: int = 10

    # ---------------- LLM ----------------
    LLM_PROVIDER: str = "deepseek"
    LLM_BASE_URL: str = "https://api.deepseek.com"
    LLM_API_KEY: SecretStr = SecretStr("")
    LLM_MODEL_GENERATE: str = "deepseek-chat"
    LLM_MODEL_DIAGNOSE: str = "deepseek-reasoner"
    LLM_TIMEOUT_SEC: int = 90
    LLM_MAX_RETRY: int = 2
    LLM_PROMPT_TOKEN_BUDGET: int = 8000
    LLM_AUTOFIX_MAX_ROUNDS: int = 3

    # ---------------- Capability / Ledger 密钥 ----------------
    CAPABILITY_SIGNING_KEY_PATH: str = "/run/secrets/capability_signing_key.pem"
    CAPABILITY_KEY_ID: str = "cap-dev-1"
    CAPABILITY_TTL_SEC: int = 300
    CAPABILITY_ISSUER: str = "elt-agent"
    LEDGER_SIGNING_KEY_PATH: str = "/run/secrets/ledger_signing_key.pem"
    LEDGER_CHECKPOINT_INTERVAL: int = 100

    # ---------------- 演示数据面 ----------------
    DEMO_MYSQL_HOST: str = "mysql"
    DEMO_MYSQL_PORT: int = 3306
    DEMO_MYSQL_USER: str = "etl_demo"
    DEMO_MYSQL_PASSWORD: SecretStr = SecretStr("")
    DEMO_MYSQL_DB: str = "biz_demo"
    DEMO_DORIS_HOST: str = "doris"
    DEMO_DORIS_PORT: int = 9030
    DEMO_DORIS_USER: str = "root"
    DEMO_DORIS_PASSWORD: SecretStr = SecretStr("")
    DEMO_DORIS_DB: str = "dw_demo"
    SEATUNNEL_HOST: str = "seatunnel"
    SEATUNNEL_API_PORT: int = 8080
    # 容器内工作目录（Dockerfile 已建），不是宿主临时目录，S108 不适用
    SEATUNNEL_WORK_DIR: str = "/tmp/seatunnel-jobs"  # noqa: S108

    # ---------------- 准备单与审批（be-13）----------------
    # 审批有效期。**进配置而不是写死在 service 里**：它是一个业务口径
    # （「多久没批完就当废」），不是一个技术常数，改它不应该需要发版。
    # 默认 72h：够覆盖一个周末，又短到不会让「上个月批过的单」被拿来执行。
    PREPARATION_TTL_HOURS: int = 72

    # ---------------- 运行监督默认预算 ----------------
    SUP_MAX_READ_ROWS: int = 100_000_000
    SUP_MAX_WRITE_BYTES: int = 53_687_091_200
    SUP_MAX_DURATION_SEC: int = 7200
    SUP_MAX_AMPLIFICATION_RATIO: float = 1.5
    SUP_MAX_REJECT_RATE: float = 0.05
    SUP_WARN_THRESHOLD_PCT: int = 80

    # ---------------- 保留策略（0 = 永久）----------------
    RUN_LOG_RETENTION_DAYS: int = 90
    QUALITY_REPORT_RETENTION_DAYS: int = 0
    EVIDENCE_RETENTION_DAYS: int = 0

    # ---------------- 派生属性 ----------------
    @property
    def is_production(self) -> bool:
        return self.APP_ENV is Env.PROD

    @property
    def postgres_dsn(self) -> str:
        """SQLAlchemy async DSN。密码做 URL 编码，避免特殊字符破坏 DSN。"""
        pwd = quote(self.POSTGRES_PASSWORD.get_secret_value(), safe="")
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{pwd}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def postgres_dsn_sync(self) -> str:
        """Alembic / 脚本用的同步 DSN（psycopg）。"""
        pwd = quote(self.POSTGRES_PASSWORD.get_secret_value(), safe="")
        return (
            f"postgresql+psycopg://{self.POSTGRES_USER}:{pwd}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    def redis_url(self, db: int | None = None) -> str:
        pwd = self.REDIS_PASSWORD.get_secret_value()
        auth = f":{quote(pwd, safe='')}@" if pwd else ""
        index = db if db is not None else self.REDIS_DB_CACHE
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{index}"

    @property
    def celery_broker_url(self) -> str:
        return self.redis_url(self.REDIS_DB_BROKER)

    @property
    def celery_result_backend(self) -> str:
        return self.redis_url(self.REDIS_DB_RESULT)

    @property
    def minio_endpoint(self) -> str:
        return self.MINIO_ENDPOINT or f"{self.MINIO_HOST}:{self.MINIO_PORT}"

    @property
    def seatunnel_api_base(self) -> str:
        return f"http://{self.SEATUNNEL_HOST}:{self.SEATUNNEL_API_PORT}"

    # ---------------- 生产环境自检 ----------------
    def _assert_no_placeholder(self, name: str, value: SecretStr | str) -> None:
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not raw or raw.lower().startswith(_PLACEHOLDER_PREFIXES):
            raise ValueError(f"生产环境禁止使用占位/空值：{name}（当前值被拒绝）")

    @model_validator(mode="after")
    def _validate_by_env(self) -> Settings:
        if self.is_production:
            # 密钥与密码一律不得为占位值
            for name in (
                "SECRET_KEY",
                "POSTGRES_PASSWORD",
                "REDIS_PASSWORD",
                "MINIO_ROOT_PASSWORD",
                "VAULT_TOKEN",
                "LLM_API_KEY",
            ):
                self._assert_no_placeholder(name, getattr(self, name))

            if self.APP_DEBUG:
                raise ValueError("生产环境必须 APP_DEBUG=false")
            if "*" in self.CORS_ORIGINS:
                raise ValueError("生产环境禁止 CORS 通配 '*'，请显式列出来源域名")
            if not self.VAULT_ADDR.startswith("https://") and "127.0.0.1" not in self.VAULT_ADDR:
                # Vault 未启用 TLS 且非本机回环，视为配置错误
                raise ValueError("生产环境 Vault 必须走 HTTPS 或本机回环地址")
        return self

    def safe_dump(self) -> dict[str, object]:
        """可安全写入日志的配置快照（敏感值只显示是否已配置）。"""
        sensitive = {
            "SECRET_KEY",
            "POSTGRES_PASSWORD",
            "REDIS_PASSWORD",
            "MINIO_ROOT_PASSWORD",
            "VAULT_TOKEN",
            "VAULT_SECRET_ID",
            "LLM_API_KEY",
            "DEMO_MYSQL_PASSWORD",
            "DEMO_DORIS_PASSWORD",
        }
        out: dict[str, object] = {}
        for key, value in self.model_dump().items():
            if key in sensitive:
                raw = getattr(self, key)
                raw = raw.get_secret_value() if isinstance(raw, SecretStr) else raw
                out[key] = "***configured***" if raw else "***empty***"
            else:
                out[key] = value
        return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
