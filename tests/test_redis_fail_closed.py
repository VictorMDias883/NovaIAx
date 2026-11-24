"""Security state must not silently fall back to an empty memory store."""

import pytest
from app.cache import redis_client as redis_module
from app.cache.redis_client import RedisClient, RedisUnavailableError
from app.cache.token_denylist import TokenDenylist
from app.exceptions.api_errors import APIError


class _BrokenRedis:
    async def ping(self) -> None:
        raise ConnectionError("down")


@pytest.fixture
def _redis_down(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Factory:
        @staticmethod
        def from_url(*_args, **_kwargs):  # noqa: ANN205
            return _BrokenRedis()

    monkeypatch.setattr(redis_module, "redis_async", _Factory)
    monkeypatch.setattr(redis_module, "_last_redis_attempt", None)


@pytest.mark.usefixtures("_redis_down")
async def test_a_fail_closed_client_raises_instead_of_using_memory() -> None:
    with pytest.raises(RedisUnavailableError):
        await RedisClient(fail_closed=True).get("anything")


@pytest.mark.usefixtures("_redis_down")
async def test_the_default_client_still_falls_back_to_memory() -> None:
    client = RedisClient()
    await client.set("k", "v")
    assert await client.get("k") == "v"


@pytest.mark.usefixtures("_redis_down")
async def test_the_denylist_answers_503_when_it_cannot_check() -> None:
    denylist = TokenDenylist(RedisClient(fail_closed=True))
    with pytest.raises(APIError) as exc_info:
        await denylist.is_revoked("some-jti")
    assert exc_info.value.status_code == 503
