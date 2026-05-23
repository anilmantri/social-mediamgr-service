"""
FastAPI application factory — Module 1: AI Content Engine
"""
import time
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.endpoints.content import router as content_router
from app.api.v1.endpoints.scheduler import router as scheduler_router
from app.core.config import settings
from app.db.session import check_db_connection

log = structlog.get_logger(__name__)


# ── Logging setup ──────────────────────────────────────────────────────────────
def configure_logging() -> None:
    import logging
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer() if not settings.is_production
            else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if settings.DEBUG else logging.INFO
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )


# ── Lifespan ───────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()

    # Sentry
    if settings.SENTRY_DSN:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            integrations=[FastApiIntegration()],
            environment=settings.ENVIRONMENT,
            release=settings.APP_VERSION,
            traces_sample_rate=0.1 if settings.is_production else 1.0,
        )

    db_ok = await check_db_connection()
    log.info(
        "startup",
        app=settings.APP_NAME,
        version=settings.APP_VERSION,
        env=settings.ENVIRONMENT,
        db_ok=db_ok,
    )

    yield  # application runs

    log.info("shutdown", app=settings.APP_NAME)


# ── App factory ────────────────────────────────────────────────────────────────
def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        description="social-mediamgr-service — AI Content Engine API",
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        lifespan=lifespan,
    )

    # ── Middleware ─────────────────────────────────────────────────────────────
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[str(o) for o in settings.ALLOWED_ORIGINS],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Request ID + timing middleware ─────────────────────────────────────────
    @app.middleware("http")
    async def request_context(request: Request, call_next) -> Response:
        import uuid as _uuid
        request_id = request.headers.get("X-Request-ID", str(_uuid.uuid4()))
        structlog.contextvars.bind_contextvars(request_id=request_id)
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = (time.perf_counter() - start) * 1000
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time"] = f"{elapsed:.1f}ms"
        log.debug(
            "http_request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            ms=round(elapsed, 1),
        )
        structlog.contextvars.clear_contextvars()
        return response

    # ── Exception handlers ─────────────────────────────────────────────────────
    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"detail": str(exc), "type": "validation_error"},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled_exception", path=request.url.path)
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "type": "server_error"},
        )

    # ── Routes ────────────────────────────────────────────────────────────────
    app.include_router(content_router)
    app.include_router(scheduler_router)

    @app.get("/health", tags=["Health"])
    async def health() -> dict:
        db_ok = await check_db_connection()
        return {
            "status": "ok" if db_ok else "degraded",
            "version": settings.APP_VERSION,
            "environment": settings.ENVIRONMENT,
            "db": "up" if db_ok else "down",
        }

    return app


app = create_app()