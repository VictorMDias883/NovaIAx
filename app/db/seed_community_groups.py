"""
Default niche communities.

The service calls :func:`ensure_default_groups` before listing groups, so a
fresh database (or a new default added here) gets its rows without a manual
seed step.  It only **inserts missing slugs**: an operator who edited or
deactivated a group keeps their change.  To retire a default, deactivate it
(``active = false``) instead of deleting the row.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.community_group import CommunityGroup, GroupAccess

#: Rules every member accepts (shown before the first post).
DEFAULT_RULES = (
    "Seja respeitoso. Não publique dados pessoais (seus ou de outras pessoas), "
    "endereço, telefone ou documentos. Proibido discurso de ódio, assédio, "
    "golpes, venda de produtos ilegais e conteúdo sexual. Conteúdo denunciado "
    "pode ser ocultado e contas podem ser impedidas de publicar."
)


@dataclass(frozen=True)
class GroupSeed:
    slug: str
    name: str
    description: str
    icon: str
    access: GroupAccess
    audience_slug: str | None
    sort_order: int


DEFAULT_GROUPS: tuple[GroupSeed, ...] = (
    GroupSeed(
        slug="geral",
        name="Bate-papo geral",
        description="Dicas, experiências e dúvidas do dia a dia, aberto a todos.",
        icon="💬",
        access=GroupAccess.OPEN,
        audience_slug=None,
        sort_order=0,
    ),
    GroupSeed(
        slug="religiosos",
        name="Religiosos",
        description="Rotina, comunidade e fé: troque experiências com respeito a todas as crenças.",
        icon="⛪",
        access=GroupAccess.AUDIENCE,
        audience_slug="religiosos",
        sort_order=10,
    ),
    GroupSeed(
        slug="estudantes",
        name="Estudantes",
        description="Organização dos estudos, dúvidas acadêmicas, vestibular e concursos.",
        icon="🎓",
        access=GroupAccess.AUDIENCE,
        audience_slug="estudantes",
        sort_order=20,
    ),
    GroupSeed(
        slug="trabalhadores",
        name="Trabalhadores",
        description="Rotina de trabalho, turnos, direitos e dicas entre colegas de profissão.",
        icon="🛠️",
        access=GroupAccess.AUDIENCE,
        audience_slug="trabalhadores_turnos",
        sort_order=30,
    ),
    GroupSeed(
        slug="maes_solteiras",
        name="Mães solteiras",
        description="Apoio, organização da rotina e troca entre mães. Entrada com aprovação.",
        icon="👩‍👧",
        access=GroupAccess.APPROVAL,
        audience_slug="maes_solteiras",
        sort_order=40,
    ),
    GroupSeed(
        slug="motoristas",
        name="Motoristas",
        description="Aplicativo, táxi, caminhão ou moto: trânsito, rotas, manutenção e segurança.",
        icon="🚗",
        access=GroupAccess.AUDIENCE,
        audience_slug="motoristas",
        sort_order=50,
    ),
    GroupSeed(
        slug="autonomos",
        name="Autônomos",
        description="Clientes, preços, MEI e organização para quem trabalha por conta própria.",
        icon="💼",
        access=GroupAccess.AUDIENCE,
        audience_slug="autonomos_freelancers",
        sort_order=60,
    ),
)


async def ensure_default_groups(session: AsyncSession) -> int:
    """Insert every default group whose slug is missing; return how many."""
    existing = set((await session.execute(select(CommunityGroup.slug))).scalars().all())
    missing = [seed for seed in DEFAULT_GROUPS if seed.slug not in existing]
    for seed in missing:
        session.add(
            CommunityGroup(
                slug=seed.slug,
                name=seed.name,
                description=seed.description,
                icon=seed.icon,
                access=seed.access.value,
                audience_slug=seed.audience_slug,
                rules=DEFAULT_RULES,
                active=True,
                sort_order=seed.sort_order,
            )
        )
    if missing:
        try:
            await session.commit()
        except IntegrityError:
            # A concurrent request seeded the same slugs first.
            await session.rollback()
            return 0
    return len(missing)


__all__ = ["DEFAULT_GROUPS", "DEFAULT_RULES", "ensure_default_groups"]
