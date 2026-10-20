"""API v1 public remote-app-configuration endpoint.

``GET /api/v1/app-config`` is the one call the mobile app makes at startup to
learn whether the backend is in maintenance mode, which app version it must be
on, where to update and which modules are enabled.  It carries no user data and
requires no authentication, so it sits in the auth middleware's public
allow-list.

The response is served from a short-lived Redis cache and revalidated with an
``ETag`` (see :func:`app.utils.http_cache.cached_json_response`), so a polling
client normally transfers no body.
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.api.deps import get_redis_client, get_session
from app.cache.redis_client import RedisClient
from app.core.config import Settings, get_settings
from app.services.app_config_service import AppConfigService
from app.utils.http_cache import cached_json_response

router = APIRouter(prefix="/app-config", tags=["app-config"])


@router.get("")
async def get_app_config(
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis_client: RedisClient = Depends(get_redis_client),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Return the public remote app configuration.

    Layout::

        {
          "maintenance": {"enabled": bool, "message": str},
          "versions": {"min_supported": str, "recommended": str, "store_url": str},
          "features": {"medications": bool, ...},
          "announcements": [{"title": str, "body": str, "level": str}]
        }

    A short ``Cache-Control`` and a strong ``ETag`` let clients revalidate
    cheaply; a bodyless ``304`` is returned when ``If-None-Match`` matches.
    """
    payload = await AppConfigService(session, settings).load_public(redis_client)
    return cached_json_response(request, payload, max_age=settings.app_config_cache_ttl_seconds)
