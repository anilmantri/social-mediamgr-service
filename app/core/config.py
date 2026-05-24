from functools import lru_cache
from typing import Literal
from pydantic import AnyHttpUrl, Field, PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ────────────────────────────────────────────────────────────────────
    APP_NAME: str = "social-mediamgr-service"
    APP_VERSION: str = "1.0.0"
    ENVIRONMENT: Literal["development", "staging", "production"] = "development"
    DEBUG: bool = False
    SECRET_KEY: str = Field(..., min_length=32)
    ALLOWED_ORIGINS: list[AnyHttpUrl] = []

    # ── Database ───────────────────────────────────────────────────────────────
    DATABASE_URL: PostgresDsn = Field(
        default="postgresql+asyncpg://postgres:password@localhost:5432/social_mediamgr"
    )
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_TIMEOUT: int = 30
    DB_ECHO: bool = False

    # ── Redis ─────────────────────────────────────────────────────────────────
    REDIS_URL: RedisDsn = Field(default="redis://localhost:6379/0")
    REDIS_CACHE_TTL: int = 3600  # seconds

    # ── Celery ────────────────────────────────────────────────────────────────
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"
    CELERY_TASK_SOFT_TIME_LIMIT: int = 120
    CELERY_TASK_TIME_LIMIT: int = 180

    # ── AI Providers ──────────────────────────────────────────────────────────
    GROQ_API_KEY: str = Field(...)
    GROQ_MODEL: str = "llama-3.1-70b-versatile"
    GROQ_MAX_TOKENS: int = 2048
    GROQ_TEMPERATURE: float = 0.75

    OPENAI_API_KEY: str = Field(...)
    OPENAI_IMAGE_MODEL: str = "dall-e-3"
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    OPENAI_IMAGE_SIZE: Literal["1024x1024", "1024x1792", "1792x1024"] = "1024x1024"
    OPENAI_IMAGE_QUALITY: Literal["standard", "hd"] = "hd"

    # ── Storage (S3 / Cloudflare R2) ──────────────────────────────────────────
    S3_ENDPOINT_URL: str | None = None
    S3_ACCESS_KEY_ID: str = Field(default="")
    S3_SECRET_ACCESS_KEY: str = Field(default="")
    S3_BUCKET_NAME: str = "social-mediamgr-assets"
    S3_REGION: str = "us-east-1"
    CDN_BASE_URL: str = ""

    # ── Auth ──────────────────────────────────────────────────────────────────
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # ── Instagram Graph API ────────────────────────────────────────────────────
    INSTAGRAM_APP_ID: str = Field(default="")
    INSTAGRAM_APP_SECRET: str = Field(default="")
    INSTAGRAM_REDIRECT_URI: str = Field(default="http://localhost:8000/api/v1/instagram/callback")
    INSTAGRAM_API_VERSION: str = "v19.0"
    INSTAGRAM_BASE_URL: str = "https://graph.facebook.com"
    INSTAGRAM_RATE_LIMIT_PER_HOUR: int = 200   # Meta's published limit

    # ── Scheduling ─────────────────────────────────────────────────────────────
    SCHEDULER_TICK_SECONDS: int = 60           # how often beat checks for due posts
    PUBLISHER_MAX_RETRIES: int = 3
    PUBLISHER_RETRY_BACKOFF: int = 300         # 5 min between retries
    INSIGHTS_SYNC_INTERVAL_HOURS: int = 6
    TOKEN_REFRESH_INTERVAL_DAYS: int = 50      # refresh before 60-day expiry

    # ── Optimal time ──────────────────────────────────────────────────────────
    OPTIMAL_TIME_MIN_POSTS: int = 10           # min posts before time model is trusted
    OPTIMAL_TIME_TOP_N: int = 5                # top N slots to return

    # ── Content generation limits ─────────────────────────────────────────────
    MAX_CAPTION_RETRIES: int = 3
    MAX_IMAGE_RETRIES: int = 3
    MAX_BATCH_SIZE: int = 31               # max posts for 30-day calendar + buffer
    BRAND_VOICE_MIN_SAMPLES: int = 5       # minimum approved posts to build voice profile

    # ── Sentry ────────────────────────────────────────────────────────────────
    SENTRY_DSN: str | None = None

    # ── Google OAuth ──────────────────────────────────────────────────────────
    GOOGLE_CLIENT_ID: str = Field(default="")
    GOOGLE_CLIENT_SECRET: str = Field(default="")
    GOOGLE_REDIRECT_URI: str = Field(default="http://localhost:8000/api/v1/auth/google/callback")

    # ── Stripe ────────────────────────────────────────────────────────────────
    STRIPE_SECRET_KEY: str = Field(default="")
    STRIPE_WEBHOOK_SECRET: str = Field(default="")
    STRIPE_STARTER_MONTHLY_PRICE_ID: str = Field(default="")
    STRIPE_STARTER_ANNUAL_PRICE_ID: str  = Field(default="")
    STRIPE_PRO_MONTHLY_PRICE_ID: str     = Field(default="")
    STRIPE_PRO_ANNUAL_PRICE_ID: str      = Field(default="")
    STRIPE_AGENCY_MONTHLY_PRICE_ID: str  = Field(default="")
    STRIPE_AGENCY_ANNUAL_PRICE_ID: str   = Field(default="")

    # ── Email (SMTP) ──────────────────────────────────────────────────────────
    SMTP_HOST: str = Field(default="localhost")
    SMTP_PORT: int = Field(default=1025)
    SMTP_USER: str = Field(default="")
    SMTP_PASSWORD: str = Field(default="")
    SMTP_FROM_EMAIL: str = Field(default="noreply@socialmgr.app")
    SMTP_FROM_NAME: str  = Field(default="Social Media Manager")

    # ── Frontend ──────────────────────────────────────────────────────────────
    FRONTEND_URL: str = Field(default="http://localhost:3000")

    @field_validator("ALLOWED_ORIGINS", mode="before")
    @classmethod
    def parse_origins(cls, v: str | list) -> list:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def database_url_sync(self) -> str:
        """Sync URL for Alembic migrations."""
        return str(self.DATABASE_URL).replace("+asyncpg", "")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
