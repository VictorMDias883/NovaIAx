#!/usr/bin/env python
"""Seed the database with a known user, one objective and its roadmap days.

Point the app at this account and every screen has data: the objective list,
the roadmap navigation (seven days with content and status) and the chat
history all have something to show.

The seed also provisions an ADMIN account (panel + ``/api/v1`` admin calls),
sample promo codes and informational promotion cards, so the billing and
operator-console flows can be exercised locally without a Play connection.

The password is read from ``SEED_PASSWORD`` when set, so it is never written
to a file or echoed by default; without it a strong random one is generated
and printed once.  The admin password follows the same rule via
``SEED_ADMIN_PASSWORD``.

Usage::

    python -m scripts.seed_dev
    python -m scripts.seed_dev --email me@example.com --password 'S3nh@Forte!'

``GROQ_API_KEY`` decides how the roadmap is produced.  With a key the real
provider is used; without one the roadmap is generated locally so the script
still works offline and never spends quota.

The script is idempotent: re-running it updates the existing account instead
of failing on the unique email constraint.

The account configured in ``ADMIN_BOOTSTRAP_EMAIL`` is owned by the startup
bootstrap (see ``app/db/bootstrap.py``): the seed never creates it and never
re-hashes its password, so the two flows cannot fight over the same row.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import string
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# Allow ``python scripts/seed_dev.py`` as well as ``python -m scripts.seed_dev``.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings  # noqa: E402
from app.core.security import pwd_context  # noqa: E402
from app.db.seed_agent_catalog import seed_agent_catalog  # noqa: E402
from app.db.session import Base, SessionLocal, engine  # noqa: E402
from app.models.community import CommunityReport, ReportCategory, ReportStatus  # noqa: E402
from app.models.health import Medication  # noqa: E402
from app.models.objective import Objective  # noqa: E402
from app.models.productivity import Note, Recurrence, Task, TaskPriority, TaskStatus  # noqa: E402
from app.models.promo_code import PromoCode, PromoCodeKind  # noqa: E402
from app.models.promotion import Promotion  # noqa: E402
from app.models.roadmap_day import RoadmapDay, RoadmapDayStatus  # noqa: E402
from app.models.user import User, UserRole  # noqa: E402
from sqlalchemy import select  # noqa: E402

logger = logging.getLogger("seed_dev")

DEFAULT_EMAIL = "test@novaiax.dev"
DEFAULT_NAME = "Teste development"
DEFAULT_TITLE = "Aprender Python assíncrono"
DEFAULT_ADMIN_EMAIL = "admin@novaiax.dev"
DEFAULT_ADMIN_NAME = "Admin development"
DEFAULT_DESCRIPTION = (
    "Escrever código async/await idiomático, cobrir os caminhos de erro "
    "com testes e documentar as decisões de arquitetura."
)

#: Sample promo codes, one per :class:`PromoCodeKind`, so every redemption
#: path (plan grant, trial extension, quota bonus) can be tested locally.
#: Codes are stored normalised to lower case, matching the model contract.
@dataclass(frozen=True)
class PromoCodeSample:
    """One sample :class:`PromoCode` row."""

    code: str
    kind: str
    plan: str | None = None
    duration_days: int | None = None
    bonus_messages: int | None = None
    note: str | None = None


@dataclass(frozen=True)
class PromotionSample:
    """One sample :class:`Promotion` card."""

    title: str
    message: str
    emoji: str | None = None
    cta_label: str | None = None
    cta_target: str = "plans"
    audience: str = "all"
    audience_plans: list[str] | None = None
    priority: int = 0


SAMPLE_PROMO_CODES: tuple[PromoCodeSample, ...] = (
    PromoCodeSample(
        code="noviax-starter30",
        kind=PromoCodeKind.GRANT_PLAN.value,
        plan="starter",
        duration_days=30,
        note="Seed local: plano Starter por 30 dias.",
    ),
    PromoCodeSample(
        code="noviax-trial14",
        kind=PromoCodeKind.EXTEND_TRIAL.value,
        plan="trial",
        duration_days=14,
        note="Seed local: prorroga o trial por 14 dias.",
    ),
    PromoCodeSample(
        code="noviax-bonus20",
        kind=PromoCodeKind.BONUS_QUOTA.value,
        bonus_messages=20,
        note="Seed local: +20 mensagens de IA por dia.",
    ),
)

#: Sample informational promotion cards.  Both are visible to a fresh
#: account (audience ``all`` / plans that include ``free``), so
#: ``GET /promotions/active`` returns data on first launch.
SAMPLE_PROMOTIONS: tuple[PromotionSample, ...] = (
    PromotionSample(
        title="Bem-vindo ao NovaIAx",
        message="Resgatando um código promocional você libera recursos premium.",
        emoji="🚀",
        cta_label="Resgatar código",
        cta_target="redeem",
        audience="all",
        priority=10,
    ),
    PromotionSample(
        title="Conheça o plano Pro",
        message="Modelos de IA mais potentes e automações para a sua rotina.",
        emoji="✨",
        cta_label="Ver planos",
        cta_target="plans",
        audience="plan",
        audience_plans=["free", "trial"],
        priority=5,
    ),
)

#: The seven days the seed creates, in Portuguese, so the app is exercised with
#: the same content a real user would see.
DAY_METAS: tuple[tuple[str, RoadmapDayStatus], ...] = (
    ("Revisar event loop e corrotinas", RoadmapDayStatus.COMPLETED),
    ("Escrever o primeiro cliente httpx assíncrono", RoadmapDayStatus.COMPLETED),
    ("Praticar asyncio.gather e TaskGroup", RoadmapDayStatus.COMPLETED),
    ("Configurar pool de conexões e timeouts", RoadmapDayStatus.PENDING),
    ("Escrever testes com pytest-asyncio", RoadmapDayStatus.PENDING),
    ("Medir e otimizar consultas N+1", RoadmapDayStatus.PENDING),
    ("Documentar as decisões no README", RoadmapDayStatus.PENDING),
)


def generate_password(length: int = 20) -> str:
    """Return a random password that satisfies the project's policy.

    The policy requires an uppercase letter, a lowercase letter and a digit,
    so the alphabet is built from all three and a character of each is placed
    first to make the result valid regardless of the shuffle.
    """
    alphabet = string.ascii_letters + string.digits + "!@#$%&*"
    body = "".join(secrets.choice(alphabet) for _ in range(max(length - 3, 1)))
    required = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
    ]
    chars = required + list(body)
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def resolve_password(explicit: str | None, env_var: str = "SEED_PASSWORD") -> str:
    """Prefer the explicit flag, then ``env_var``, then a random one."""
    if explicit:
        return explicit
    from_env = os.getenv(env_var)
    if from_env:
        return from_env
    generated = generate_password()
    logger.warning("%s was not set; using a generated password: %s", env_var, generated)
    return generated


async def upsert_user(email: str, full_name: str, password: str) -> tuple[User, bool]:
    """Return ``(user, created)``, updating the password if it already exists."""
    async with SessionLocal() as session:
        existing = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if existing is None:
            user = User(
                full_name=full_name,
                email=email,
                password_hash=pwd_context.hash(password),
            )
            session.add(user)
            await session.commit()
            return user, True

        existing.password_hash = pwd_context.hash(password)
        await session.commit()
        return existing, False


async def upsert_admin(email: str, full_name: str, password: str) -> tuple[User | None, bool]:
    """Return ``(user, created)`` for a local ADMIN account.

    The panel never registers administrators: an admin is a ``users`` row
    with ``role = ADMIN``.  Locally the seed mints one directly so
    ``/admin`` and the admin-only ``/api/v1`` calls work out of the box
    (production uses ``scripts/make_admin_sql.py`` instead — see
    ``docs/ADMIN_PANEL.md``).

    The account configured in ``ADMIN_BOOTSTRAP_EMAIL`` belongs to the
    startup bootstrap (see :mod:`app.db.bootstrap`): the seed never creates
    it and never re-hashes its password, so the two flows cannot fight over
    the same row.  When that account does not exist yet the seed simply
    skips it — the next API boot creates it with the configured credentials.

    Returns ``(None, False)`` when the bootstrap account is not in the
    database yet.
    """
    bootstrap_email = (get_settings().admin_bootstrap_email or "").strip().lower()
    if bootstrap_email and email.strip().lower() == bootstrap_email:
        async with SessionLocal() as session:
            existing = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if existing is None:
            logger.warning(
                "Admin %s pertence ao bootstrap de inicialização; o seed não o cria — "
                "a API cria a conta no próximo boot.",
                email,
            )
            return None, False
        logger.warning(
            "Admin %s pertence ao bootstrap de inicialização; a senha não foi alterada.",
            email,
        )
        return existing, False

    async with SessionLocal() as session:
        existing = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if existing is None:
            admin = User(
                full_name=full_name,
                email=email,
                password_hash=pwd_context.hash(password),
                role=UserRole.ADMIN,
            )
            session.add(admin)
            await session.commit()
            return admin, True

        existing.password_hash = pwd_context.hash(password)
        existing.role = UserRole.ADMIN
        await session.commit()
        return existing, False


async def seed_promo_codes(admin: User | None) -> int:
    """Create the sample promo codes, one per redemption kind.

    Idempotent: an existing code keeps its settings untouched, so re-running
    the seed never resets a code someone already redeemed locally.

    ``admin`` may be ``None`` when the bootstrap account does not exist yet
    — the codes are then created without an author.
    """
    session = SessionLocal()
    created = 0
    try:
        for sample in SAMPLE_PROMO_CODES:
            existing = (
                await session.execute(select(PromoCode).where(PromoCode.code == sample.code))
            ).scalar_one_or_none()
            if existing is not None:
                continue
            session.add(
                PromoCode(
                    code=sample.code,
                    kind=sample.kind,
                    plan=sample.plan,
                    duration_days=sample.duration_days,
                    bonus_messages=sample.bonus_messages,
                    per_user_limit=1,
                    active=True,
                    note=sample.note,
                    created_by=admin.id if admin is not None else None,
                )
            )
            created += 1
        await session.commit()
        return created
    finally:
        await session.close()


async def seed_promotions() -> int:
    """Create the sample informational promotion cards.

    Idempotent by title: an existing card keeps its edited content.
    """
    session = SessionLocal()
    created = 0
    try:
        for sample in SAMPLE_PROMOTIONS:
            existing = (
                await session.execute(select(Promotion).where(Promotion.title == sample.title))
            ).scalar_one_or_none()
            if existing is not None:
                continue
            session.add(
                Promotion(
                    title=sample.title,
                    message=sample.message,
                    emoji=sample.emoji,
                    cta_label=sample.cta_label,
                    cta_target=sample.cta_target,
                    audience=sample.audience,
                    audience_plans=sample.audience_plans,
                    active=True,
                    priority=sample.priority,
                )
            )
            created += 1
        await session.commit()
        return created
    finally:
        await session.close()


async def seed_objective(user: User, title: str, description: str) -> Objective:
    """Create the objective with its seven roadmap days.

    The roadmap text is produced by the same validation the service uses, so a
    malformed provider response cannot leave the seed half-written.
    """
    session = SessionLocal()
    try:
        roadmap_updated_at = datetime.now(UTC)
        objective = Objective(
            user_id=user.id,
            title=title,
            description=description,
            due_date=datetime.now(UTC) + timedelta(days=30),
            roadmap_updated_at=roadmap_updated_at,
        )
        session.add(objective)
        await session.flush()

        period_start = roadmap_updated_at.date()
        for index, (meta, status) in enumerate(DAY_METAS):
            session.add(
                RoadmapDay(
                    objective_id=objective.id,
                    day_number=index + 1,
                    day_date=period_start + timedelta(days=index),
                    content=meta,
                    status=status,
                )
            )
        objective.roadmap = "## Roadmap\n" + "\n".join(f"- Dia {i + 1}: {m}" for i, (m, _) in enumerate(DAY_METAS))
        await session.commit()
        await session.refresh(objective)
        return objective
    finally:
        await session.close()


async def seed_support_modules(user: User) -> dict[str, int]:
    """Seed one row per support module so every screen has data.

    Idempotent: an existing row for the account is left untouched, so
    re-running the seed does not accumulate duplicates.
    """
    session = SessionLocal()
    counts = {"tasks": 0, "notes": 0, "medications": 0, "reports": 0}
    try:
        has_task = (
            await session.execute(select(Task.id).where(Task.user_id == user.id).limit(1))
        ).scalar_one_or_none()
        if has_task is None:
            session.add(
                Task(
                    user_id=user.id,
                    title="Revisar o roadmap da semana",
                    notes="Criado pelo seed de desenvolvimento.",
                    due_at=datetime.now(UTC) + timedelta(days=1),
                    priority=TaskPriority.MEDIUM,
                    status=TaskStatus.PENDING,
                    recurrence=Recurrence.NONE,
                )
            )
            counts["tasks"] = 1

        has_note = (
            await session.execute(select(Note.id).where(Note.user_id == user.id).limit(1))
        ).scalar_one_or_none()
        if has_note is None:
            session.add(
                Note(
                    user_id=user.id,
                    title="Anotações do seed",
                    content="Esta nota foi criada por scripts/seed_dev.py.",
                    tags=["seed", "dev"],
                    pinned=True,
                )
            )
            counts["notes"] = 1

        has_med = (
            await session.execute(select(Medication.id).where(Medication.user_id == user.id).limit(1))
        ).scalar_one_or_none()
        if has_med is None:
            session.add(
                Medication(
                    user_id=user.id,
                    name="Vitamina D",
                    dosage="1 comprimido",
                    frequency="1x ao dia",
                    instructions="Tomar após o café da manhã.",
                    active=True,
                )
            )
            counts["medications"] = 1

        has_report = (
            await session.execute(
                select(CommunityReport.id).where(CommunityReport.user_id == user.id).limit(1)
            )
        ).scalar_one_or_none()
        if has_report is None:
            session.add(
                CommunityReport(
                    user_id=user.id,
                    category=ReportCategory.INFRASTRUCTURE,
                    title="Buraco na via",
                    description="Buraco aberto na calçada em frente ao número 100.",
                    location="Rua Exemplo, 100",
                    status=ReportStatus.SUBMITTED,
                    is_anonymous=False,
                )
            )
            counts["reports"] = 1

        await session.commit()
        return counts
    finally:
        await session.close()


async def seed(
    email: str,
    full_name: str,
    password: str,
    title: str,
    description: str,
    admin_email: str,
    admin_name: str,
    admin_password: str,
) -> int:
    """Seed everything.  Returns a process exit code."""
    await init_schema()
    async with SessionLocal() as catalog_session:
        catalog = await seed_agent_catalog(catalog_session)
    user, created = await upsert_user(email, full_name, password)
    objective = await seed_objective(user, title, description)
    support = await seed_support_modules(user)
    admin, admin_created = await upsert_admin(admin_email, admin_name, admin_password)
    promo_codes = await seed_promo_codes(admin)
    promotions = await seed_promotions()

    print()
    print("Seed concluído.")
    print(f"  usuário   : {user.email} ({'criado' if created else 'atualizado'})")
    print(f"  senha     : {password}")
    print(f"  objetivo  : #{objective.id} - {objective.title}")
    print(f"  dias      : {len(DAY_METAS)} (roadmap atualizado)")
    print(f"  catálogo  : {catalog['groups']} grupos, {catalog['sub_agents']} sub-agentes")
    print(
        "  suporte   : "
        f"{support['tasks']} tarefas, {support['notes']} notas, "
        f"{support['medications']} medicamentos, {support['reports']} relatos"
    )
    print(
        "  admin     : "
        + (
            f"{admin.email} ({'criado' if admin_created else 'atualizado'}) — acesso ao painel em /admin"
            if admin is not None
            else "não criado (conta do bootstrap de inicialização — criada no próximo boot da API)"
        )
    )
    print(f"  promo     : {promo_codes} códigos, {promotions} promoções criados")
    print()
    print(f"Entre em http://localhost:8000/docs com {email} e a senha acima.")
    if not get_settings().groq_api_key:
        print()
        print("GROQ_API_KEY não definida: o roadmap do seed foi gerado localmente,")
        print("sem consumir cota do provider.")
    return 0


async def init_schema() -> None:
    """Create the tables if they do not exist.

    A deployment that manages its schema with Alembic already has them; this
    only helps a brand-new local database, and it never *alters* an existing
    table.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", default=DEFAULT_EMAIL, help=f"account email (default: {DEFAULT_EMAIL})")
    parser.add_argument("--name", default=DEFAULT_NAME, help="display name")
    parser.add_argument("--password", default=None, help="account password (default: $SEED_PASSWORD or random)")
    parser.add_argument("--title", default=DEFAULT_TITLE, help="objective title")
    parser.add_argument("--description", default=DEFAULT_DESCRIPTION, help="objective description")
    parser.add_argument(
        "--admin-email", default=DEFAULT_ADMIN_EMAIL, help=f"admin email (default: {DEFAULT_ADMIN_EMAIL})"
    )
    parser.add_argument("--admin-name", default=DEFAULT_ADMIN_NAME, help="admin display name")
    parser.add_argument(
        "--admin-password",
        default=None,
        help="admin password (default: $SEED_ADMIN_PASSWORD or random)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    password = resolve_password(args.password)
    admin_password = resolve_password(args.admin_password, "SEED_ADMIN_PASSWORD")

    try:
        return asyncio.run(
            seed(
                args.email,
                args.name,
                password,
                args.title,
                args.description,
                args.admin_email,
                args.admin_name,
                admin_password,
            )
        )
    except Exception as exc:  # noqa: BLE001 - top-level CLI guard
        logger.error("Falha ao popular o banco: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
