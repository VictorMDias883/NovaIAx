"""Photo analysis: vision routing, the /agents/general/image route and privacy."""

import asyncio
import uuid
from typing import Any, cast

import pytest
from app.api.deps import get_ai_router
from app.cache.redis_client import RedisClient
from app.clients.ai_quota import AIQuotaTracker
from app.clients.ai_router import AIRouter
from app.clients.ai_types import AIRequest
from app.core.config import AIProviderConfig, get_settings
from app.main import app
from app.services.general_agent_service import PHOTO_HISTORY_MARKER, GeneralAgentService
from fastapi.testclient import TestClient

from tests.helpers import make_conversation_cache
from tests.test_general_agent import _create_user, _run

_PASSWORD = "Password123"
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"
_DATA_URL = "data:image/jpeg;base64,AAAA"


class _VisionAI:
    """Duck-typed client recording what reached the provider."""

    def __init__(self, reply: str = "Vejo um gato no sofá.") -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    async def create_chat_completion(self, system_prompt: str, user_message: str, model: str = "") -> str:
        self.calls.append({"model": model, "messages": [{"role": "user", "content": user_message}]})
        return self.reply

    async def create_chat_completion_with_history(
        self, system_prompt: str, messages: list[dict[str, Any]], model: str = ""
    ) -> str:
        self.calls.append({"model": model, "messages": messages, "system": system_prompt})
        return self.reply


def test_service_sends_the_photo_and_stores_only_a_marker() -> None:
    async def scenario(session) -> None:  # noqa: ANN001
        user = await _create_user(session)
        cache = make_conversation_cache()
        ai = _VisionAI()
        service = GeneralAgentService(session, ai_client=ai, conversation_cache=cache)

        result = await service.analyze_image(user_id=user.id, image_data_url=_DATA_URL, question="O que é isso?")

        assert result["assistant_message"] == "Vejo um gato no sofá."
        last = ai.calls[-1]["messages"][-1]
        assert last["role"] == "user"
        assert last["content"][0] == {"type": "text", "text": "O que é isso?"}
        assert last["content"][1]["image_url"]["url"] == _DATA_URL
        assert "Você não edita imagens" in ai.calls[-1]["system"]

        stored = await cache.get_messages(GeneralAgentService.AGENT_KEY, user.id)
        assert stored == [
            {"role": "user", "content": f"{PHOTO_HISTORY_MARKER} O que é isso?"},
            {"role": "assistant", "content": "Vejo um gato no sofá."},
        ]
        assert "base64" not in str(stored)

    asyncio.run(_run(scenario))


@pytest.mark.asyncio
async def test_router_uses_the_vision_model_and_skips_blind_providers() -> None:
    settings = get_settings()
    blind, seeing = _VisionAI("blind"), _VisionAI("seeing")
    clients = {"blind": blind, "seeing": seeing}
    router = AIRouter(
        providers=[
            AIProviderConfig(name="blind", base_url="http://b", api_key="k", priority=1),
            AIProviderConfig(
                name="seeing", base_url="http://s", api_key="k", priority=2, vision_model="vision-1"
            ),
        ],
        quota_tracker=AIQuotaTracker(RedisClient(), settings),
        settings=settings,
        client_factory=lambda config: cast(Any, clients[config.name]),
    )

    response = await router.complete(
        AIRequest(system_prompt="s", user_message="oi", image_data_url=_DATA_URL)
    )

    assert response.text == "seeing"
    assert response.model == "vision-1"
    assert blind.calls == []
    # A plain text turn still goes to the first provider.
    assert (await router.complete(AIRequest(system_prompt="s", user_message="oi"))).text == "blind"


@pytest.fixture
def client():  # noqa: ANN201
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "rate_limit_ai", 10_000)
    RedisClient.reset_all_memory_stores()


def _register(client: TestClient) -> dict[str, Any]:
    email = f"image-{uuid.uuid4().hex[:8]}@example.com"
    return client.post(
        "/api/v1/auth/register",
        json={"full_name": "Image User", "email": email, "password": _PASSWORD, "birth_date": "1990-01-01"},
    ).json()


def test_image_route_validates_and_answers(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    fake = _VisionAI()
    monkeypatch.setattr(app, "dependency_overrides", {**app.dependency_overrides})
    app.dependency_overrides[get_ai_router] = lambda: fake
    headers = {"Authorization": f"Bearer {_register(client)['access_token']}"}
    url = "/api/v1/agents/general/image"

    not_image = client.post(url, headers=headers, files={"image": ("a.txt", b"hello", "text/plain")})
    assert not_image.status_code == 415

    too_big = b"\xff\xd8\xff" + b"\x00" * (3 * 1024 * 1024)
    assert client.post(url, headers=headers, files={"image": ("a.jpg", too_big, "image/jpeg")}).status_code == 413

    ok = client.post(
        url,
        headers=headers,
        files={"image": ("foto.jpg", _JPEG, "image/jpeg")},
        data={"user_message": "O que tem aqui?"},
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["assistant_message"] == "Vejo um gato no sofá."
    assert body["message_id"]
    sent_url = fake.calls[-1]["messages"][-1]["content"][1]["image_url"]["url"]
    assert sent_url.startswith("data:image/jpeg;base64,")


def test_image_route_requires_authentication(client: TestClient) -> None:
    resp = client.post("/api/v1/agents/general/image", files={"image": ("a.jpg", _JPEG, "image/jpeg")})
    assert resp.status_code == 401
