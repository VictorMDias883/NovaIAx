"""
Tests for the ``/health`` endpoint and its underlying dependency checks.

``/health`` is a combined liveness/readiness probe: it performs a real
database query (``SELECT 1``) and a real Redis ping on every call, then:

* returns 200 ``{"status": "ok", "db": "ok", "redis": "ok"}`` when both
  dependencies are reachable, and
* returns **503** with the failing component(s) reported as ``"error"``
  when either check fails (so Fly's health check marks the machine
  unhealthy).

The dependency checks are monkeypatched here so the HTTP behaviour is
deterministic regardless of the local infrastructure.
"""

import app.main as main_module
from fastapi.testclient import TestClient


def test_health_ok_when_all_dependencies_up(monkeypatch) -> None:
    async def ok_database() -> str:
        return "ok"

    async def ok_redis(url: str) -> str:
        return "ok"

    monkeypatch.setattr(main_module, "check_database", ok_database)
    monkeypatch.setattr(main_module, "check_redis", ok_redis)

    with TestClient(main_module.app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok", "redis": "ok"}


def test_health_503_when_database_down(monkeypatch) -> None:
    async def error_database() -> str:
        return "error"

    async def ok_redis(url: str) -> str:
        return "ok"

    monkeypatch.setattr(main_module, "check_database", error_database)
    monkeypatch.setattr(main_module, "check_redis", ok_redis)

    with TestClient(main_module.app) as client:
        response = client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "error", "db": "error", "redis": "ok"}


def test_health_503_when_redis_down(monkeypatch) -> None:
    async def ok_database() -> str:
        return "ok"

    async def error_redis(url: str) -> str:
        return "error"

    monkeypatch.setattr(main_module, "check_database", ok_database)
    monkeypatch.setattr(main_module, "check_redis", error_redis)

    with TestClient(main_module.app) as client:
        response = client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "error", "db": "ok", "redis": "error"}


def test_health_503_when_both_dependencies_down(monkeypatch) -> None:
    async def error_database() -> str:
        return "error"

    async def error_redis(url: str) -> str:
        return "error"

    monkeypatch.setattr(main_module, "check_database", error_database)
    monkeypatch.setattr(main_module, "check_redis", error_redis)

    with TestClient(main_module.app) as client:
        response = client.get("/health")
    assert response.status_code == 503
    assert response.json() == {"status": "error", "db": "error", "redis": "error"}


def test_check_database_returns_ok_with_working_db() -> None:
    """A reachable database passes the ``SELECT 1`` check."""
    import asyncio

    from app.core.health import check_database

    assert asyncio.run(check_database()) == "ok"


def test_check_redis_returns_error_when_unreachable() -> None:
    """An unreachable Redis server fails the ping check."""
    import asyncio

    from app.core.health import check_redis

    assert asyncio.run(check_redis("redis://127.0.0.1:1/0")) == "error"
