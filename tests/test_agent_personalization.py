"""
Tests for the agent personalization domain.

Covers the catalog endpoints (``GET /agent-groups``, ``GET /sub-agents``),
the profile endpoints (``GET/PUT /me/agent``), the memory CRUD
(``GET/POST/PATCH/DELETE /me/memories``), the learning toggle
(``PUT /me/learning``), the name filter, the auto-learning pipeline and the
folding of personalization (profile + groups + sub-agents + memories) into the
general agent and ``/ai/chat`` prompts, including the ``why``/``sources``
marker parsing.
"""

import asyncio
import uuid

import pytest
from app.api.deps import get_ai_router, get_settings
from app.commands.agent_memory_commands import CreateUserMemoryCommand
from app.commands.chat_completion_command import ChatCompletionCommand
from app.commands.general_agent_command import GeneralAgentCommand
from app.commands.update_agent_profile_command import UpdateAgentProfileCommand
from app.db.seed_agent_catalog import seed_agent_catalog
from app.db.session import Base, SessionLocal
from app.main import app
from app.models.system_prompt import SystemPrompt
from app.models.user import User
from app.models.user_agent import UserAgent
from app.models.user_memory import UserMemory
from app.services.agent_personalization_service import AgentPersonalizationService
from app.services.agent_prompt_service import (
    build_personalized_system_prompt,
    load_personalization_context,
    parse_personalization_block,
)
from app.services.chat_completion_service import ChatCompletionService
from app.services.general_agent_service import GeneralAgentService
from app.services.memory_learning_service import MemoryLearningService, extract_candidates
from app.utils.name_filter import blocked_name_reason
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.helpers import force_memory_redis, make_conversation_cache

_PASSWORD = "Password123"

_MARKER_REPLY = (
    'Resposta útil aqui.\n'
    '[personalization]{"why": ["Porque o usuário pediu isso.", "Usei o grupo ativo."], '
    '"sources": ["estudantes", "auto_moro_lisboa"]}[/personalization]'
)


class RecordingAI:
    """Duck-typed AI double implementing the history-based chat interface."""

    def __init__(self, reply: str = "Resposta do agente") -> None:
        self.reply = reply
        self.system_prompts: list[str] = []

    async def create_chat_completion_with_history(
        self,
        system_prompt: str,
        messages: list[dict[str, str]],
    ) -> str:
        self.system_prompts.append(system_prompt)
        return self.reply


# ---------------------------------------------------------------------------
# Name filter
# ---------------------------------------------------------------------------


def test_name_filter_blocks_offensive_and_impersonation() -> None:
    assert blocked_name_reason("Idiota") is not None
    assert blocked_name_reason("meu palhaço") is not None
    assert blocked_name_reason("NovaIAx") is not None
    assert blocked_name_reason("ChatGPT") is not None
    assert blocked_name_reason("assistente") is not None


def test_name_filter_accepts_ordinary_names() -> None:
    assert blocked_name_reason("MeuAmigo") is None
    assert blocked_name_reason("Companheiro de Jornada") is None
    # "assinatura" shares no whole token with the blocked "ass"; no false hit.
    assert blocked_name_reason("Assinatura") is None


# ---------------------------------------------------------------------------
# Marker parsing
# ---------------------------------------------------------------------------


def test_parse_marker_extracts_why_and_sources() -> None:
    text, why, sources = parse_personalization_block(_MARKER_REPLY)
    assert text == "Resposta útil aqui."
    assert why == ["Porque o usuário pediu isso.", "Usei o grupo ativo."]
    assert sources == ["estudantes", "auto_moro_lisboa"]


def test_parse_marker_is_a_noop_without_a_marker() -> None:
    text, why, sources = parse_personalization_block("Resposta sem marcador.")
    assert text == "Resposta sem marcador."
    assert why == []
    assert sources == []


def test_parse_marker_with_invalid_json_keeps_the_text_and_empty_lists() -> None:
    reply = "Texto.\n[personalization]{nao-e-json}[/personalization]"
    text, why, sources = parse_personalization_block(reply)
    assert "Texto." in text
    assert why == []
    assert sources == []


def test_parse_marker_with_non_list_payloads_yields_empty_lists() -> None:
    reply = 'Ok.\n[personalization]{"why": "apenas", "sources": null}[/personalization]'
    text, why, sources = parse_personalization_block(reply)
    assert text == "Ok."
    assert why == []
    assert sources == []


# ---------------------------------------------------------------------------
# Auto-learning extraction
# ---------------------------------------------------------------------------


def test_learns_explicit_first_person_statements() -> None:
    candidates = extract_candidates("Eu moro em Lisboa. Eu trabalho com vendas de roupas.")
    assert len(candidates) == 2
    assert candidates[0][0].startswith("auto_moro")
    assert candidates[0][1] == "Eu moro em Lisboa"
    assert candidates[1][0].startswith("auto_trabalho")
    assert candidates[1][1] == "Eu trabalho com vendas de roupas"


def test_does_not_learn_questions_or_bare_utterances() -> None:
    assert extract_candidates("Como eu estou indo?") == []
    assert extract_candidates("Eu sou.") == []
    assert extract_candidates("Oi") == []


def test_does_not_learn_sensitive_topics_without_save_intent() -> None:
    assert extract_candidates("Eu tenho diabetes e tomo remédio.") == []
    assert extract_candidates("Eu sou evangélico e vou ao culto.") == []
    assert extract_candidates("Eu apoio o candidato X na eleição.") == []
    assert extract_candidates("Meu salário é de três mil reais.") == []


def test_learns_sensitive_topics_when_the_user_asks_to_save() -> None:
    candidates = extract_candidates("Quero que você lembre que eu tenho diabetes.")
    assert len(candidates) == 1
    assert candidates[0][0].startswith("auto_")


def test_learns_possessive_statements() -> None:
    candidates = extract_candidates("Meu filho se chama Arthur.")
    assert len(candidates) == 1
    assert candidates[0][0].startswith("auto_filho")
    assert candidates[0][1] == "Meu filho se chama Arthur"


# ---------------------------------------------------------------------------
# Service + HTTP helpers for real-database scenarios
# ---------------------------------------------------------------------------


async def _run_real_db(scenario) -> None:
    """Run ``scenario(session, user_id)`` against an in-memory SQLite DB."""

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(bind=engine, expire_on_commit=False)
            async with factory() as session:
                await seed_agent_catalog(session)
                user = User(
                    full_name="Pers User",
                    email=f"pers-{uuid.uuid4().hex[:8]}@example.com",
                    password_hash="x",
                )
                session.add(user)
                await session.commit()
                # No refresh: it would reopen an implicit transaction and the
                # services below (which own their writes with ``begin()``)
                # would collide with it.
                await scenario(session, int(user.id))
        finally:
            await engine.dispose()

    await run()


@pytest.fixture(scope="module", autouse=True)
def _seed_catalog_http() -> None:
    """Idempotently seed the agent catalog used by the HTTP tests."""

    async def seed() -> None:
        async with SessionLocal() as session:
            await seed_agent_catalog(session)

    asyncio.run(seed())


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate cached state and lift the AI rate limit for these tests."""
    force_memory_redis(monkeypatch)
    monkeypatch.setattr(get_settings(), "rate_limit_ai", 10_000)


@pytest.fixture(autouse=True)
def _cleanup() -> None:
    yield

    async def purge() -> None:
        async with SessionLocal() as session:
            result = await session.execute(
                select(User.id).where(User.email.like("pers-%@example.com"))
            )
            ids = [row[0] for row in result.all()]
            if ids:
                await session.execute(delete(UserAgent).where(UserAgent.user_id.in_(ids)))
                await session.execute(delete(UserMemory).where(UserMemory.user_id.in_(ids)))
            await session.execute(delete(SystemPrompt).where(SystemPrompt.tipo.like("pers-%")))
            await session.execute(delete(User).where(User.email.like("pers-%@example.com")))
            await session.commit()

    asyncio.run(purge())


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _register(client: TestClient) -> dict:
    email = f"pers-{uuid.uuid4().hex[:8]}@example.com"
    resp = client.post(
        "/api/v1/auth/register",
        json={"full_name": "Pers User", "email": email, "password": _PASSWORD, "birth_date": "1990-01-01"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _me_id(client: TestClient, tokens: dict) -> int:
    resp = client.get("/api/v1/auth/me", headers=_auth(tokens["access_token"]))
    assert resp.status_code == 200, resp.text
    return int(resp.json()["id"])


# ---------------------------------------------------------------------------
# HTTP: catalog
# ---------------------------------------------------------------------------


def test_catalog_endpoints_require_authentication(client: TestClient) -> None:
    assert client.get("/api/v1/agent-groups").status_code == 401
    assert client.get("/api/v1/sub-agents").status_code == 401


def test_list_agent_groups_returns_the_seeded_catalog(client: TestClient) -> None:
    tokens = _register(client)
    resp = client.get("/api/v1/agent-groups", headers=_auth(tokens["access_token"]))
    assert resp.status_code == 200, resp.text
    groups = resp.json()
    assert len(groups) == 15
    slugs = [group["slug"] for group in groups]
    assert "estudantes" in slugs and "maes_solteiras" in slugs and "idosos" in slugs
    assert [group["sort_order"] for group in groups] == sorted(
        group["sort_order"] for group in groups
    ), "catalog must be ordered by sort_order"


def test_list_sub_agents_returns_seeded_entries_with_group_link(client: TestClient) -> None:
    tokens = _register(client)
    resp = client.get("/api/v1/sub-agents", headers=_auth(tokens["access_token"]))
    assert resp.status_code == 200, resp.text
    sub_agents = {item["slug"]: item for item in resp.json()}
    assert set(sub_agents) == {
        "financas",
        "saude",
        "estudo",
        "produtividade",
        "negocios",
        "social",
    }
    assert sub_agents["estudo"]["group_slug"] == "estudantes"
    assert sub_agents["negocios"]["group_slug"] == "pequenos_negocios"
    assert sub_agents["financas"]["group_slug"] is None
    assert all(item["min_plan"] == "free" for item in sub_agents.values())


# ---------------------------------------------------------------------------
# HTTP: profile
# ---------------------------------------------------------------------------


def test_get_my_agent_returns_defaults_without_a_profile(client: TestClient) -> None:
    tokens = _register(client)
    resp = client.get("/api/v1/me/agent", headers=_auth(tokens["access_token"]))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent_name"] == "MeuAgente"
    assert body["language"] == "pt-BR"
    assert body["timezone"] == "America/Sao_Paulo"
    assert body["active_group_slugs"] == []
    assert body["active_sub_agent_slugs"] == []
    assert body["simplified_mode"] is False
    assert body["learning_enabled"] is True


def test_update_my_agent_persists_and_resolves_active_catalog_entries(client: TestClient) -> None:
    tokens = _register(client)
    headers = _auth(tokens["access_token"])
    resp = client.put(
        "/api/v1/me/agent",
        headers=headers,
        json={
            "agent_name": "MeuCompanheiro",
            "active_group_slugs": ["estudantes"],
            "active_sub_agent_slugs": ["financas", "estudo"],
            "simplified_mode": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["agent_name"] == "MeuCompanheiro"
    assert body["active_group_slugs"] == ["estudantes"]
    assert body["active_sub_agent_slugs"] == ["estudo", "financas"]
    assert [g["name"] for g in body["active_groups"]] == ["Estudantes"]
    assert {s["name"] for s in body["active_sub_agents"]} == {"Finanças", "Estudo"}
    assert body["simplified_mode"] is True

    again = client.get("/api/v1/me/agent", headers=headers)
    assert again.json()["agent_name"] == "MeuCompanheiro"


def test_update_my_agent_rejects_offensive_and_impersonating_names(client: TestClient) -> None:
    tokens = _register(client)
    headers = _auth(tokens["access_token"])
    for name in ["Idiota", "NovaIAx", "ChatGPT"]:
        resp = client.put("/api/v1/me/agent", headers=headers, json={"agent_name": name})
        assert resp.status_code == 422, (name, resp.text)


def test_update_my_agent_rejects_too_short_names(client: TestClient) -> None:
    tokens = _register(client)
    resp = client.put(
        "/api/v1/me/agent",
        headers=_auth(tokens["access_token"]),
        json={"agent_name": "A"},
    )
    assert resp.status_code == 422, resp.text


def test_update_my_agent_rejects_unknown_or_inactive_slugs(client: TestClient) -> None:
    tokens = _register(client)
    headers = _auth(tokens["access_token"])
    resp = client.put(
        "/api/v1/me/agent", headers=headers, json={"active_group_slugs": ["nao-existe"]}
    )
    assert resp.status_code == 422, resp.text
    resp = client.put(
        "/api/v1/me/agent", headers=headers, json={"active_sub_agent_slugs": ["nao-existe"]}
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# HTTP: memories
# ---------------------------------------------------------------------------


def test_memory_crud_flow(client: TestClient) -> None:
    tokens = _register(client)
    headers = _auth(tokens["access_token"])

    created = client.post(
        "/api/v1/me/memories",
        headers=headers,
        json={"key": "moradia", "value": "Moro em Lisboa desde 2020."},
    )
    assert created.status_code == 201, created.text
    assert created.json()["source"] == "user"
    assert created.json()["enabled"] is True

    listed = client.get("/api/v1/me/memories", headers=headers)
    assert listed.status_code == 200, listed.text
    assert [m["key"] for m in listed.json()] == ["moradia"]

    updated = client.patch(
        "/api/v1/me/memories/moradia",
        headers=headers,
        json={"value": "Moro em Lisboa desde 2021."},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["value"] == "Moro em Lisboa desde 2021."

    disabled = client.patch(
        "/api/v1/me/memories/moradia",
        headers=headers,
        json={"enabled": False},
    )
    assert disabled.json()["enabled"] is False

    deleted = client.delete("/api/v1/me/memories/moradia", headers=headers)
    assert deleted.status_code == 204, deleted.text

    empty = client.get("/api/v1/me/memories", headers=headers)
    assert empty.json() == []


def test_memory_duplicate_key_returns_409(client: TestClient) -> None:
    tokens = _register(client)
    headers = _auth(tokens["access_token"])
    first = client.post(
        "/api/v1/me/memories", headers=headers, json={"key": "k1", "value": "v1"}
    )
    assert first.status_code == 201
    second = client.post(
        "/api/v1/me/memories", headers=headers, json={"key": "k1", "value": "v2"}
    )
    assert second.status_code == 409, second.text


def test_memory_reserved_auto_prefix_returns_422(client: TestClient) -> None:
    tokens = _register(client)
    resp = client.post(
        "/api/v1/me/memories",
        headers=_auth(tokens["access_token"]),
        json={"key": "auto_moro_lisboa", "value": "x"},
    )
    assert resp.status_code == 422, resp.text


def test_memory_patch_and_delete_on_unknown_keys_return_404(client: TestClient) -> None:
    tokens = _register(client)
    headers = _auth(tokens["access_token"])
    assert (
        client.patch("/api/v1/me/memories/nao-existe", headers=headers, json={"value": "x"}).status_code == 404
    )
    assert client.delete("/api/v1/me/memories/nao-existe", headers=headers).status_code == 404


# ---------------------------------------------------------------------------
# HTTP: learning toggle
# ---------------------------------------------------------------------------


def test_learning_toggle_persists(client: TestClient) -> None:
    tokens = _register(client)
    headers = _auth(tokens["access_token"])
    resp = client.put("/api/v1/me/learning", headers=headers, json={"enabled": False})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"enabled": False}
    profile = client.get("/api/v1/me/agent", headers=headers)
    assert profile.json()["learning_enabled"] is False

    resp = client.put("/api/v1/me/learning", headers=headers, json={"enabled": True})
    assert resp.json() == {"enabled": True}


# ---------------------------------------------------------------------------
# Learning service: disabled flag and sensitive guard through the service
# ---------------------------------------------------------------------------


def test_learning_service_respects_the_toggle() -> None:
    async def scenario(session, user_id: int) -> None:
        service = AgentPersonalizationService(session)
        await service.set_learning(user_id, enabled=False)

        learner = MemoryLearningService(session)
        saved = await learner.learn_from_statement(user_id, "Eu moro em Lisboa.")
        assert saved == []

    asyncio.run(_run_real_db(scenario))


def test_learning_service_saves_explicit_statements() -> None:
    async def scenario(session, user_id: int) -> None:
        from app.services.privacy_service import ConsentService

        await ConsentService(session).record(user_id, "learning")
        learner = MemoryLearningService(session)
        saved = await learner.learn_from_statement(user_id, "Eu moro em Lisboa.")
        assert len(saved) == 1
        assert saved[0].key.startswith("auto_")
        assert saved[0].source == "learned"

        again = await learner.learn_from_statement(user_id, "Eu moro em Lisboa.")
        assert len(again) == 1, "re-learning must upsert, not duplicate"
        assert again[0].id == saved[0].id

    asyncio.run(_run_real_db(scenario))


# ---------------------------------------------------------------------------
# Personalization folds into the chat and general-agent prompts
# ---------------------------------------------------------------------------


def test_chat_prompt_is_personalized_and_why_sources_are_parsed() -> None:
    async def scenario(session, user_id: int) -> None:
        ai = RecordingAI(reply=_MARKER_REPLY)

        service = AgentPersonalizationService(session)
        await service.update_profile(
            user_id,
            UpdateAgentProfileCommand(
                agent_name="MeuRu",
                active_group_slugs=["estudantes"],
                active_sub_agent_slugs=["financas"],
            ),
        )
        await service.create_memory(
            user_id, CreateUserMemoryCommand(key="moradia", value="Moro em Lisboa desde 2020.")
        )

        prompt = SystemPrompt(tipo=f"pers-{uuid.uuid4().hex[:8]}", system_prompt="Prompt do admin.")
        session.add(prompt)
        await session.commit()
        await session.refresh(prompt)

        chat = ChatCompletionService(session=session, ai_client=ai, cache=None)
        result = await chat.create_completion(
            ChatCompletionCommand(agent_id=int(prompt.id), user_message="Como posso estudar melhor?"),
            user_id=user_id,
        )

        assert result["assistant_message"] == "Resposta útil aqui."
        assert result["why"] == ["Porque o usuário pediu isso.", "Usei o grupo ativo."]
        assert result["sources"] == ["estudantes", "auto_moro_lisboa"]

        system_prompt = ai.system_prompts[0]
        assert "Prompt do admin." in system_prompt, "the admin base prompt must be preserved"
        assert "MeuRu" in system_prompt
        assert "Estudantes" in system_prompt and "Finanças" in system_prompt
        assert "moradia" in system_prompt and "Moro em Lisboa desde 2020." in system_prompt
        assert "não confiáveis" in system_prompt or "nao confiaveis" in system_prompt

    asyncio.run(_run_real_db(scenario))


def test_general_agent_prompt_is_personalized_when_a_profile_exists() -> None:
    async def scenario(session, user_id: int) -> None:
        ai = RecordingAI(reply=_MARKER_REPLY)
        profile_service = AgentPersonalizationService(session)
        await profile_service.update_profile(
            user_id,
            UpdateAgentProfileCommand(agent_name="Guia", active_group_slugs=["idosos"]),
        )

        agent = GeneralAgentService(
            session, ai_client=ai, conversation_cache=make_conversation_cache()
        )
        result = await agent.create_completion(
            GeneralAgentCommand(user_message="Como estou indo?"), user_id=user_id
        )

        assert result["assistant_message"] == "Resposta útil aqui."
        assert result["why"] != []
        assert result["sources"] == ["estudantes", "auto_moro_lisboa"]
        assert "Guia" in ai.system_prompts[0]
        assert "Agente Geral da NovaIAx" in ai.system_prompts[0]

    asyncio.run(_run_real_db(scenario))


def test_chat_endpoint_returns_personalized_why_and_sources(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ai = RecordingAI(reply=_MARKER_REPLY)
    monkeypatch.setattr(app, "dependency_overrides", {**app.dependency_overrides})
    app.dependency_overrides[get_ai_router] = lambda: ai

    async def create_prompt() -> int:
        async with SessionLocal() as session:
            prompt = SystemPrompt(tipo=f"pers-{uuid.uuid4().hex[:8]}", system_prompt="Prompt base do chat.")
            session.add(prompt)
            await session.commit()
            await session.refresh(prompt)
            return int(prompt.id)

    prompt_id = asyncio.run(create_prompt())
    tokens = _register(client)
    headers = _auth(tokens["access_token"])
    client.put(
        "/api/v1/me/agent",
        headers=headers,
        json={"agent_name": "WebAgente", "active_group_slugs": ["bem_estar_fitness"]},
    )
    client.post(
        "/api/v1/me/memories",
        headers=headers,
        json={"key": "rotina", "value": "Acordo às seis da manhã."},
    )

    resp = client.post(
        "/api/v1/ai/chat",
        headers=headers,
        json={"agent_id": prompt_id, "user_message": "Me ajuda ?"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["assistant_message"] == "Resposta útil aqui."
    assert body["why"] == ["Porque o usuário pediu isso.", "Usei o grupo ativo."]
    assert body["sources"] == ["estudantes", "auto_moro_lisboa"]
    system_prompt = ai.system_prompts[-1]
    assert "WebAgente" in system_prompt
    assert "rotina" in system_prompt and "Acordo às seis da manhã." in system_prompt


def test_chat_without_profile_stays_unpersonalized(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ai = RecordingAI(reply="Sem personalização.")
    monkeypatch.setattr(app, "dependency_overrides", {**app.dependency_overrides})
    app.dependency_overrides[get_ai_router] = lambda: ai

    async def create_prompt() -> int:
        async with SessionLocal() as session:
            prompt = SystemPrompt(tipo=f"pers-{uuid.uuid4().hex[:8]}", system_prompt="Prompt base do chat.")
            session.add(prompt)
            await session.commit()
            await session.refresh(prompt)
            return int(prompt.id)

    prompt_id = asyncio.run(create_prompt())
    tokens = _register(client)

    resp = client.post(
        "/api/v1/ai/chat",
        headers=_auth(tokens["access_token"]),
        json={"agent_id": prompt_id, "user_message": "oi"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.pop("message_id")
    assert body == {"assistant_message": "Sem personalização.", "why": [], "sources": []}
    assert ai.system_prompts == ["Prompt base do chat."]


def test_prompt_builder_merges_groups_sub_agents_and_memories_untrusted() -> None:
    async def scenario(session, user_id: int) -> None:
        profile_service = AgentPersonalizationService(session)
        await profile_service.update_profile(
            user_id,
            UpdateAgentProfileCommand(
                agent_name="NomeTeste",
                tone="seco e direto",
                active_group_slugs=["estudantes"],
                active_sub_agent_slugs=["financas"],
            ),
        )
        await profile_service.create_memory(
            user_id, CreateUserMemoryCommand(key="cidade", value="Lisboa")
        )

        ctx = await load_personalization_context(session, user_id)
        assert ctx is not None
        prompt = build_personalized_system_prompt("BASE", ctx)
        assert prompt.startswith("BASE")
        assert "NomeTeste" in prompt
        assert "seco e direto" in prompt
        assert "Estudantes" in prompt
        assert "Finanças" in prompt
        assert "cidade: Lisboa" in prompt

        clean, why, sources = parse_personalization_block(_MARKER_REPLY)
        assert clean == "Resposta útil aqui."

    asyncio.run(_run_real_db(scenario))


def test_auto_learning_flows_into_the_prompt() -> None:
    async def scenario(session, user_id: int) -> None:
        from app.services.privacy_service import ConsentService

        await ConsentService(session).record(user_id, "learning")
        learner = MemoryLearningService(session)
        await learner.learn_from_statement(user_id, "Eu moro em Lisboa desde 2020.")

        ctx = await load_personalization_context(session, user_id)
        assert ctx is not None
        assert any("auto_moro" in memory.key for memory in ctx.memories)

    asyncio.run(_run_real_db(scenario))


def test_learning_requires_and_stops_withdrawn_consent() -> None:
    async def scenario(session, user_id: int) -> None:
        from app.models.consent import ConsentRecord
        from app.models.user_agent import UserAgent
        from app.services.privacy_service import ConsentService
        from sqlalchemy import select

        learner = MemoryLearningService(session)
        assert await learner.learn_from_statement(user_id, "Eu moro em Lisboa.") == []

        await ConsentService(session).record(user_id, "learning")
        assert len(await learner.learn_from_statement(user_id, "Eu moro em Lisboa.")) == 1

        assert await ConsentService(session).withdraw(user_id, "learning")
        assert await learner.learn_from_statement(user_id, "Eu trabalho em Recife.") == []
        profile = (
            await session.execute(select(UserAgent).where(UserAgent.user_id == user_id))
        ).scalar_one_or_none()
        assert profile is not None and profile.learning_enabled is False
        consent = await session.execute(
            select(ConsentRecord).where(ConsentRecord.user_id == user_id, ConsentRecord.type == "learning")
        )
        assert consent.scalar_one_or_none() is None

    asyncio.run(_run_real_db(scenario))
