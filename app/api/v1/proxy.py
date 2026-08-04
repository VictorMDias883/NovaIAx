"""
API Gateway reverse-proxy endpoint.

This router implements a **reverse proxy** that forwards incoming HTTP
requests to one or more downstream microservices.  The gateway acts as
a single entry point, providing:

    - **Routing**: Requests to ``/proxy/<service_name>/...`` are
      forwarded to the corresponding downstream service configured in
      :attr:`Settings.downstream_services`.
    - **Caching**: GET and HEAD responses are cached in Redis (or the
      in-memory fallback) to reduce load on downstream services.
    - **Header enrichment**: The gateway injects ``X-Forwarded-For``
      and ``X-Gateway-User`` headers so downstream services know the
      original client and authenticated user.
    - **Payload limiting**: Requests with bodies larger than
      ``max_payload_bytes`` are rejected with a 413 error.
    - **Timeout handling**: Each downstream service has its own timeout;
      failures result in a 502 Bad Gateway.

Architecture:
    Client → [Gateway Middleware] → /proxy/<service>/... → Downstream Service
"""

import hashlib
import json
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from app.api.deps import get_auth_service, get_current_user, get_redis_client
from app.cache.redis_client import RedisClient
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.security import AuthService

# Create a sub-router with the ``/proxy`` prefix and ``proxy`` tag.
router = APIRouter(prefix="/proxy", tags=["proxy"])

# Module-level logger and settings (loaded once at import time).
logger = get_logger(__name__)
settings = get_settings()


async def _cache_key(path: str, params: str, method: str) -> str:
    """Generate a deterministic cache key for a request.

    The key incorporates the HTTP method, URL path, and query-string
    parameters so that different requests produce different cache keys.
    A SHA-256 digest is used to keep the key length manageable and
    avoid issues with special characters in the path or params.

    Args:
        path: The request URL path (e.g. ``/api/v1/proxy/ai/chat``).
        params: The query-string parameters as a string.
        method: The HTTP method (e.g. ``GET``, ``POST``).

    Returns:
        A cache key string in the format ``cache:<path>:<sha256_digest>``.
    """
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
    """Forward an incoming request to a downstream service.

    The ``service_name`` path parameter captures everything after
    ``/proxy/`` (e.g. ``ai/chat`` → ``service_name="ai/chat"``).  The
    first segment (``ai``) is used to look up the downstream service
    configuration; the remaining segments form the path on the
    downstream service.

    Flow:
        1. Look up the downstream service by name.
        2. Read and validate the request body (payload size check).
        3. For GET/HEAD requests, check the Redis cache for a hit.
        4. Build the downstream URL and forward headers (stripping
           sensitive headers like ``Authorization`` and ``Cookie``).
        5. Send the request via ``httpx.AsyncClient``.
        6. Cache the response for GET/HEAD requests.
        7. Return the downstream response to the client.

    Args:
        request: The incoming :class:`Request`.
        service_name: The path parameter capturing the service name
            and sub-path (e.g. ``"ai/chat"``).
        redis_client: Redis client for caching (injected).
        auth_service: Auth service (injected, used for type-checking
            the dependency chain).
        current_user: The authenticated user's identity (injected).

    Returns:
        A :class:`Response` mirroring the downstream service's response.

    Raises:
        HTTPException(404): If the service name is not configured.
        HTTPException(413): If the request body exceeds ``max_payload_bytes``.
        HTTPException(502): If the downstream service is unreachable.
    """
    # --- 1. Resolve the downstream service ---
    # The first path segment identifies the service (e.g. "ai").
    service = settings.downstream_services.get(service_name.split("/")[0])
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")

    # --- 2. Read and validate the request body ---
    body = await request.body()
    if len(body) > settings.max_payload_bytes:
        raise HTTPException(status_code=413, detail="Payload too large")

    # --- 3. Check the cache (GET/HEAD only) ---
    if request.method in {"GET", "HEAD"}:
        cache_key = await _cache_key(request.url.path, str(dict(request.query_params)), request.method)
        cached = await redis_client.get(cache_key)
        if cached:
            # Return the cached response with an ``X-Cache: HIT`` header
            # so clients can distinguish cached from fresh responses.
            return JSONResponse(content=json.loads(cached), headers={"X-Cache": "HIT"})

    # --- 4. Build downstream headers ---
    # Strip sensitive headers that should not be forwarded to downstream
    # services.  The gateway handles authentication itself.
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "authorization", "cookie", "x-api-key"}
    }
    # Inject the original client IP and the authenticated username so
    # downstream services can perform their own access control if needed.
    headers["X-Forwarded-For"] = request.client.host if request.client else "unknown"
    headers["X-Gateway-User"] = current_user.get("username", "anonymous")

    # --- 5. Build the downstream URL ---
    # ``service_name`` may contain sub-paths (e.g. "ai/chat").  The first
    # segment is the service name; the rest is appended to the service's
    # base URL.
    downstream_url = f"{service.base_url.rstrip('/')}/{'/'.join(service_name.split('/')[1:])}".rstrip("/") or service.base_url
    if not downstream_url.startswith("http"):
        downstream_url = f"http://{downstream_url}"

    # --- 6. Forward the request ---
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
            # Log the failure with context for debugging, then return
            # a 502 Bad Gateway to the client.
            logger.exception("Proxy request failed", extra={"service": service_name, "url": downstream_url})
            raise HTTPException(status_code=502, detail="Bad gateway response") from exc

    # --- 7. Build the response ---
    response_body = resp.content
    content_type = resp.headers.get("content-type", "application/json")
    response = Response(content=response_body, status_code=resp.status_code, media_type=content_type)
    # Forward downstream headers, excluding hop-by-hop and encoding headers
    # that should be managed by the gateway/proxy layer.
    for key, value in resp.headers.items():
        if key.lower() not in {"content-length", "content-encoding", "transfer-encoding"}:
            response.headers[key] = value

    # --- 8. Cache the response (GET/HEAD only) ---
    if request.method in {"GET", "HEAD"}:
        await redis_client.set(cache_key, response_body.decode("utf-8", errors="ignore"), ex=settings.cache_ttl_default)

    return response
