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

import base64
import hashlib
import json
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from app.api.deps import get_current_user, get_redis_client
from app.cache.redis_client import RedisClient
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.network import get_client_ip

# Create a sub-router with the ``/proxy`` prefix and ``proxy`` tag.
router = APIRouter(prefix="/proxy", tags=["proxy"])

# Module-level logger and settings (loaded once at import time).
logger = get_logger(__name__)
settings = get_settings()


async def _cache_key(path: str, params: str, method: str, identity: str) -> str:
    """Generate a deterministic cache key for a request.

    The key incorporates the HTTP method, URL path, query-string
    parameters, and the caller's identity so that different requests
    produce different cache keys — and, crucially, so cached responses
    can never leak between users (or between different API keys).  A
    SHA-256 digest is used to keep the key length manageable and
    avoid issues with special characters in the path or params.

    Args:
        path: The request URL path (e.g. ``/api/v1/proxy/ai/chat``).
        params: The query-string parameters as a string.
        method: The HTTP method (e.g. ``GET``, ``POST``).
        identity: A string scoping the entry to a single caller (user id
            plus the raw API key when one was supplied).

    Returns:
        A cache key string in the format ``cache:<path>:<sha256_digest>``.
    """
    digest = hashlib.sha256(f"{method}:{path}:{params}:{identity}".encode()).hexdigest()
    return f"cache:{path}:{digest}"


def _identity_for_cache(request: Request, current_user: dict[str, Any]) -> str:
    """Build a caller-scoping string for the cache key.

    API-key identities share the sentinel id ``"api-key"``, so the raw
    API key value is mixed in as well to keep each caller's cache
    partition separate.
    """
    identity = str(current_user.get("id", "anonymous"))
    api_key = request.headers.get("x-api-key")
    if api_key:
        identity = f"{identity}:{api_key}"
    return identity


# Hop-by-hop and encoding headers that must not be replayed from a cache
# hit (the gateway re-derives them for each response).
_CACHE_EXCLUDED_HEADERS = {
    "content-length",
    "content-encoding",
    "transfer-encoding",
    "connection",
    "keep-alive",
}


def _cache_serialise(status_code: int, headers: Any, body: bytes) -> str:
    """Pack a downstream response into a JSON cache entry.

    The raw bytes are base64-encoded so that binary payloads survive the
    round-trip through the string-based cache stores without the
    ``errors="ignore"`` data loss.
    """
    safe_headers = {key: value for key, value in headers.items() if key.lower() not in _CACHE_EXCLUDED_HEADERS}
    return json.dumps(
        {
            "status_code": status_code,
            "headers": safe_headers,
            "body": base64.b64encode(body).decode("ascii"),
        }
    )


def _cache_replay(cached: str) -> Response:
    """Rebuild a :class:`Response` from a cached JSON entry."""
    entry = json.loads(cached)
    headers = dict(entry.get("headers") or {})
    headers["X-Cache"] = "HIT"
    body = base64.b64decode(entry["body"]) if isinstance(entry.get("body"), str) else b""
    return Response(content=body, status_code=int(entry["status_code"]), headers=headers, media_type=None)


@router.get("/{service_name:path}")
@router.post("/{service_name:path}")
@router.put("/{service_name:path}")
@router.patch("/{service_name:path}")
@router.delete("/{service_name:path}")
async def proxy_request(
    request: Request,
    service_name: str,
    redis_client: RedisClient = Depends(get_redis_client),
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
        identity = _identity_for_cache(request, current_user)
        cache_key = await _cache_key(request.url.path, str(dict(request.query_params)), request.method, identity)
        cached = await redis_client.get(cache_key)
        if cached:
            try:
                # Return the cached response with an ``X-Cache: HIT`` header
                # so clients can distinguish cached from fresh responses.
                return _cache_replay(cached)
            except (ValueError, TypeError):
                # Stale/corrupt entries (e.g. written by an older format)
                # are dropped and the request is forwarded downstream.
                await redis_client.delete(cache_key)

    # --- 4. Build downstream headers ---
    # Strip sensitive headers that should not be forwarded to downstream
    # services.  The gateway handles authentication itself.
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "authorization", "cookie", "x-api-key"}
    }
    # Inject the original client IP and the authenticated user so
    # downstream services can perform their own access control if needed.
    # ``get_client_ip`` resolves the real IP (via X-Forwarded-For only
    # when a trusted proxy is configured).
    headers["X-Forwarded-For"] = get_client_ip(request, settings)
    headers["X-Gateway-User"] = current_user.get("full_name", "anonymous")

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
        await redis_client.set(
            cache_key,
            _cache_serialise(resp.status_code, resp.headers, response_body),
            ex=settings.cache_ttl_default,
        )

    return response
