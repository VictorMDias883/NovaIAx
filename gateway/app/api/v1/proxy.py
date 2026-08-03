import hashlib
import json
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from gateway.app.api.deps import get_auth_service, get_current_user, get_redis_client
from gateway.app.cache.redis_client import RedisClient
from gateway.app.core.config import get_settings
from gateway.app.core.logging import get_logger
from gateway.app.core.security import AuthService

router = APIRouter(prefix="/proxy", tags=["proxy"])
logger = get_logger(__name__)
settings = get_settings()


async def _cache_key(path: str, params: str, method: str) -> str:
    digest = hashlib.sha256(f"{method}:{path}:{params}".encode("utf-8")).hexdigest()
    return f"cache:{path}:{digest}"


@router.get("/{service_name:path}")
@router.post("/{service_name:path}")
@router.put("/{service_name:path}")
@router.patch("/{service_name:path}")
@router.delete("/{service_name:path}")
async def proxy_request(
    request: Request,
    service_name: str,
    redis_client: RedisClient = Depends(get_redis_client),
    auth_service: AuthService = Depends(get_auth_service),
    current_user: dict[str, Any] = Depends(get_current_user),
) -> Response:
    service = settings.downstream_services.get(service_name.split("/")[0])
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")

    body = await request.body()
    if len(body) > settings.max_payload_bytes:
        raise HTTPException(status_code=413, detail="Payload too large")

    if request.method in {"GET", "HEAD"}:
        cache_key = await _cache_key(request.url.path, str(dict(request.query_params)), request.method)
        cached = await redis_client.get(cache_key)
        if cached:
            return JSONResponse(content=json.loads(cached), headers={"X-Cache": "HIT"})

    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "authorization", "cookie", "x-api-key"}
    }
    headers["X-Forwarded-For"] = request.client.host if request.client else "unknown"
    headers["X-Gateway-User"] = current_user.get("username", "anonymous")

    downstream_url = f"{service.base_url.rstrip('/')}/{'/'.join(service_name.split('/')[1:])}".rstrip("/") or service.base_url
    if not downstream_url.startswith("http"):
        downstream_url = f"http://{downstream_url}"

    async with httpx.AsyncClient(timeout=service.timeout_seconds) as client:
        try:
            resp = await client.request(
                method=request.method,
                url=downstream_url,
                headers=headers,
                content=body,
                params=request.query_params,
            )
        except httpx.HTTPError as exc:
            logger.exception("Proxy request failed", extra={"service": service_name, "url": downstream_url})
            raise HTTPException(status_code=502, detail="Bad gateway response") from exc

    response_body = resp.content
    content_type = resp.headers.get("content-type", "application/json")
    response = Response(content=response_body, status_code=resp.status_code, media_type=content_type)
    for key, value in resp.headers.items():
        if key.lower() not in {"content-length", "content-encoding", "transfer-encoding"}:
            response.headers[key] = value

    if request.method in {"GET", "HEAD"}:
        await redis_client.set(cache_key, response_body.decode("utf-8", errors="ignore"), ex=settings.cache_ttl_default)

    return response
