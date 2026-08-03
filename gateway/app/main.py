from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from gateway.app.api.v1.router import router as v1_router
from gateway.app.core.config import get_settings
from gateway.app.core.logging import configure_logging
from gateway.app.exceptions.handlers import register_exception_handlers
from gateway.app.middlewares.auth_middleware import AuthMiddleware
from gateway.app.middlewares.logging_middleware import LoggingMiddleware
from gateway.app.middlewares.rate_limit_middleware import RateLimitMiddleware
from gateway.app.middlewares.security_headers_middleware import SecurityHeadersMiddleware

configure_logging()
settings = get_settings()

app = FastAPI(title=settings.app_name, version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(LoggingMiddleware)
app.add_middleware(AuthMiddleware)
app.add_middleware(RateLimitMiddleware)
app.include_router(v1_router, prefix="/api/v1")
register_exception_handlers(app)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
