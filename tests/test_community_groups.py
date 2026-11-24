"""Niche communities: access rules, privacy, flags, blocks and moderation."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Generator

import pytest
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.main import app
from app.models.community_group import CommunityPost, CommunityProfile
from app.models.user_agent import UserAgent
from app.services.community_group_service import CommunityGroupService, contains_contact_data
from fastapi.testclient import TestClient
from sqlalchemy import select

_API = "/api/v1/community"
_PASSWORD = "Password123"


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as c:
        yield c


class _User:
    def __init__(self, client: TestClient) -> None:
        email = f"cg-{uuid.uuid4().hex[:10]}@example.com"
        resp = client.post(
            "/api/v1/auth/register",
            json={"full_name": "Fulano Real Silva", "email": email, "password": _PASSWORD, "birth_date": "1990-01-01"},
        )
        assert resp.status_code == 201, resp.text
        self.email = email
        self.headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
        me = client.get("/api/v1/auth/me", headers=self.headers)
        self.id = int(me.json()["id"])

    def profile(self, client: TestClient, nickname: str, city: str | None = "Campinas", state: str | None = "SP") -> None:
        resp = client.put(
            f"{_API}/profile",
            headers=self.headers,
            json={"nickname": nickname, "city": city, "state": state, "accept_rules": True},
        )
        assert resp.status_code == 200, resp.text


def _run(coro):  # noqa: ANN001, ANN202
    return asyncio.run(coro)


async def _activate_audience(user_id: int, slug: str) -> None:
    async with SessionLocal() as session:
        session.add(UserAgent(user_id=user_id, active_group_slugs=[slug]))
        await session.commit()


async def _make_moderator(slug: str, user_id: int) -> None:
    async with SessionLocal() as session:
        service = CommunityGroupService(session)
        group = await service.repo.get_group_by_slug(slug)
        assert group is not None
        await service.admin_set_moderator(group.id, user_id, moderator=True)


def test_contact_data_detection() -> None:
    assert contains_contact_data("me chama no (11) 98765-4321")
    assert contains_contact_data("meu email é fulana@gmail.com")
    assert contains_contact_data("cpf 123.456.789-00")
    assert not contains_contact_data("Ligue 180 se precisar de ajuda, das 8h às 18h em 2026")


def test_endpoints_require_authentication(client: TestClient) -> None:
    assert client.get(f"{_API}/groups").status_code == 401


def test_profile_is_required_and_rules_must_be_accepted(client: TestClient) -> None:
    user = _User(client)
    groups = client.get(f"{_API}/groups", headers=user.headers).json()
    slugs = [group["slug"] for group in groups]
    assert slugs[:2] == ["geral", "religiosos"]
    assert {"estudantes", "trabalhadores", "maes_solteiras", "motoristas", "autonomos"} <= set(slugs)

    assert client.get(f"{_API}/profile", headers=user.headers).status_code == 404
    assert client.post(f"{_API}/groups/geral/join", headers=user.headers).status_code == 403

    no_rules = client.put(f"{_API}/profile", headers=user.headers, json={"nickname": "Ana", "accept_rules": False})
    assert no_rules.status_code == 422
    phone_nick = client.put(
        f"{_API}/profile", headers=user.headers, json={"nickname": "11987654321", "accept_rules": True}
    )
    assert phone_nick.status_code == 422
    bad_state = client.put(
        f"{_API}/profile", headers=user.headers, json={"nickname": "Ana", "state": "XX", "accept_rules": True}
    )
    assert bad_state.status_code == 422

    user.profile(client, "Ana")
    assert client.post(f"{_API}/groups/geral/join", headers=user.headers).json()["status"] == "active"


def test_only_members_read_and_authors_show_nickname_only(client: TestClient) -> None:
    author, outsider = _User(client), _User(client)
    author.profile(client, "Ana Dicas")
    outsider.profile(client, "Bia", city="Recife", state="PE")
    client.post(f"{_API}/groups/geral/join", headers=author.headers)

    created = client.post(
        f"{_API}/groups/geral/posts",
        headers=author.headers,
        json={"kind": "tip", "body": "Feira de domingo tem preço melhor depois das 11h."},
    )
    assert created.status_code == 201, created.text
    post = created.json()
    assert post["author"]["nickname"] == "Ana Dicas"
    assert (post["city"], post["state"]) == ("Campinas", "SP")

    assert client.get(f"{_API}/groups/geral/posts", headers=outsider.headers).status_code == 403
    assert client.get(f"{_API}/posts/{post['id']}", headers=outsider.headers).status_code == 404

    client.post(f"{_API}/groups/geral/join", headers=outsider.headers)
    feed = client.get(f"{_API}/groups/geral/posts", headers=outsider.headers)
    assert feed.status_code == 200
    body = feed.text
    assert "Ana Dicas" in body and "Fulano Real Silva" not in body and author.email not in body

    same_city = client.get(f"{_API}/groups/geral/posts", headers=outsider.headers, params={"city": "campinas"})
    assert post["id"] in [item["id"] for item in same_city.json()["items"]]
    other_city = client.get(f"{_API}/groups/geral/posts", headers=outsider.headers, params={"city": "Recife"})
    assert post["id"] not in [item["id"] for item in other_city.json()["items"]]

    comment = client.post(
        f"{_API}/posts/{post['id']}/comments", headers=outsider.headers, json={"body": "Valeu pela dica!"}
    )
    assert comment.status_code == 201
    detail = client.get(f"{_API}/posts/{post['id']}", headers=author.headers).json()
    assert [c["author"]["nickname"] for c in detail["comments"]] == ["Bia"]


def test_contact_data_is_refused(client: TestClient) -> None:
    user = _User(client)
    user.profile(client, "Carla")
    client.post(f"{_API}/groups/geral/join", headers=user.headers)
    resp = client.post(
        f"{_API}/groups/geral/posts", headers=user.headers, json={"body": "Me chama no zap 11 98765-4321"}
    )
    assert resp.status_code == 422


def test_audience_group_requires_the_matching_personal_audience(client: TestClient) -> None:
    user = _User(client)
    user.profile(client, "Duda")
    group = client.get(f"{_API}/groups/motoristas", headers=user.headers).json()
    assert group["can_join"] is False and group["join_hint"]
    assert client.post(f"{_API}/groups/motoristas/join", headers=user.headers).status_code == 403

    _run(_activate_audience(user.id, "motoristas"))
    assert client.get(f"{_API}/groups/motoristas", headers=user.headers).json()["can_join"] is True
    assert client.post(f"{_API}/groups/motoristas/join", headers=user.headers).json()["status"] == "active"


def test_approval_group_needs_a_moderator(client: TestClient) -> None:
    mother, moderator = _User(client), _User(client)
    mother.profile(client, "Eva")
    moderator.profile(client, "Moderadora")
    _run(_activate_audience(mother.id, "maes_solteiras"))
    _run(_make_moderator("maes_solteiras", moderator.id))

    joined = client.post(f"{_API}/groups/maes_solteiras/join", headers=mother.headers).json()
    assert joined["status"] == "pending"
    assert client.get(f"{_API}/groups/maes_solteiras/posts", headers=mother.headers).status_code == 403
    # A pending member cannot approve anyone.
    assert client.get(f"{_API}/groups/maes_solteiras/pending", headers=mother.headers).status_code == 403

    pending = client.get(f"{_API}/groups/maes_solteiras/pending", headers=moderator.headers).json()
    assert mother.id in [row["user_id"] for row in pending]
    approve = client.post(f"{_API}/groups/maes_solteiras/pending/{mother.id}/approve", headers=moderator.headers)
    assert approve.status_code == 204
    assert client.get(f"{_API}/groups/maes_solteiras/posts", headers=mother.headers).status_code == 200


def test_blocks_flags_and_moderation(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "community_report_flag_threshold", 1)
    author, reader, moderator = _User(client), _User(client), _User(client)
    for user, nick in ((author, "Autor"), (reader, "Leitor"), (moderator, "Mod")):
        user.profile(client, nick)
        client.post(f"{_API}/groups/geral/join", headers=user.headers)
    _run(_make_moderator("geral", moderator.id))

    post_id = client.post(
        f"{_API}/groups/geral/posts", headers=author.headers, json={"kind": "question", "body": "Alguém indica mecânico?"}
    ).json()["id"]

    # Block → the reader no longer sees the author's post.
    assert client.post(f"{_API}/blocks", headers=reader.headers, json={"blocked_id": author.id}).status_code == 201
    ids = [p["id"] for p in client.get(f"{_API}/groups/geral/posts", headers=reader.headers).json()["items"]]
    assert post_id not in ids
    client.delete(f"{_API}/blocks/{author.id}", headers=reader.headers)

    # Self-flag refused; one flag (threshold 1) hides it from everyone but the author/moderators.
    assert client.post(f"{_API}/posts/{post_id}/flag", headers=author.headers, json={}).status_code == 400
    flagged = client.post(f"{_API}/posts/{post_id}/flag", headers=reader.headers, json={"reason": "spam"}).json()
    assert flagged == {"flagged": True, "hidden": True}
    assert client.get(f"{_API}/posts/{post_id}", headers=reader.headers).status_code == 404
    assert client.get(f"{_API}/posts/{post_id}", headers=author.headers).json()["hidden"] is True
    assert client.get(f"{_API}/posts/{post_id}", headers=moderator.headers).status_code == 200

    # Readers cannot delete others' posts; moderators remove (soft).
    assert client.delete(f"{_API}/posts/{post_id}", headers=reader.headers).status_code == 404
    assert client.delete(f"{_API}/posts/{post_id}", headers=moderator.headers).status_code == 204

    async def _removed() -> bool:
        async with SessionLocal() as session:
            post = await session.get(CommunityPost, post_id)
            return post is not None and post.removed_at is not None

    assert _run(_removed())


def test_account_deletion_removes_community_content(client: TestClient) -> None:
    user = _User(client)
    user.profile(client, "Some")
    client.post(f"{_API}/groups/geral/join", headers=user.headers)
    client.post(f"{_API}/groups/geral/posts", headers=user.headers, json={"body": "Vou apagar minha conta."})

    export = client.get("/api/v1/me/export", headers=user.headers)
    assert export.status_code == 200, export.text
    assert "Vou apagar minha conta." in export.text

    resp = client.request("DELETE", "/api/v1/me", headers=user.headers, json={"password": _PASSWORD})
    assert resp.status_code in (200, 204), resp.text

    async def _left() -> tuple[int, int]:
        async with SessionLocal() as session:
            posts = (await session.execute(select(CommunityPost).where(CommunityPost.user_id == user.id))).all()
            profiles = (
                await session.execute(select(CommunityProfile).where(CommunityProfile.user_id == user.id))
            ).all()
            return len(posts), len(profiles)

    assert _run(_left()) == (0, 0)
