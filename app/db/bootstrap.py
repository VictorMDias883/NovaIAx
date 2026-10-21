"""
Startup bootstrap of the operator (admin) account.

The API provisions the operator account **by itself**, on every boot, right
after the Alembic migrations have run (see :func:`app.main.lifespan`).  The
container entrypoint (``entrypoint.sh``) runs ``alembic upgrade head`` before
starting uvicorn, so the ``users`` table is guaranteed to exist by the time
:func:`bootstrap_admin_account` is called.

The rules are deliberately conservative — the bootstrap must never take an
existing account away from its owner:

* the account is created **only** when ``ADMIN_BOOTSTRAP_EMAIL`` and
  ``ADMIN_BOOTSTRAP_PASSWORD`` are both configured and no user holds that
  e-mail;
* the insert is a plain ``INSERT ... ON CONFLICT (email) DO NOTHING``: no
  advisory lock and no table lock, because both break behind the Supabase
  pooler and when several workers boot at the same time;
* an existing account is never modified — its password is never re-hashed and
  its role is never lowered.  It is only promoted to ``ADMIN`` when
  ``ADMIN_BOOTSTRAP_PROMOTE_EXISTING`` is on;
* in production a malformed e-mail or a weak password is **refused** (logged,
  never raised) so a misconfigured deployment still serves traffic.

Every state change is written to ``admin_audit_log`` with the actor
``system``.
"""

from __future__ import annotations

import traceback
from datetime import UTC, date, datetime
from typing import Any, cast

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import CursorResult, Table
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.security import pwd_context
from app.db.session import SessionLocal
from app.models.consent import DEFAULT_CONSENT_TYPES, ConsentRecord
from app.models.user import User, UserRole
from app.repositories.admin_audit_log_repository import AdminAuditLogRepository
from app.repositories.user_repository import UserRepository

logger = get_logger(__name__)

#: Birth date stored on the bootstrap account.  The column is nullable, but
#: the registration flow always fills it (minimum-age gate), so the bootstrap
#: does too: an adult by a wide margin.
BOOTSTRAP_BIRTH_DATE = date(1990, 1, 1)

#: Minimum length of the bootstrap password accepted in production.
MIN_BOOTSTRAP_PASSWORD_LENGTH = 12

#: Passwords that are never accepted in production, whatever their length.
#: The list holds the classic defaults plus the placeholders shipped in
#: ``.env.example`` — a deployment that copied the template verbatim must not
#: end up with a guessable operator account.
WEAK_BOOTSTRAP_PASSWORDS: frozenset[str] = frozenset(
    {
        "change-me",
        "changeme",
        "admin",
        "admin123",
        "password",
        "12345678",
        "change-me-in-production",
        "changeme123",
        "replace-with-strong-key",
        "your-secret-key",
        "your-password",
        "your-groq-api-key-here",
        "placeholder",
        "senha",
        "senha123",
    }
)

#: Actor recorded in the audit trail for bootstrap actions.
SYSTEM_ACTOR = "system"

#: Audit action written when the operator account is created.
ACTION_CREATED = "admin_bootstrap_created"

#: Audit action written when an existing account is promoted to ADMIN.
ACTION_PROMOTED = "admin_bootstrap_promoted"

#: Outcomes returned by :func:`bootstrap_admin_account`.
OUTCOME_CREATED = "created"
OUTCOME_PROMOTED = "promoted"
OUTCOME_UNCHANGED = "unchanged"
OUTCOME_SKIPPED = "skipped"

_EMAIL_ADAPTER = TypeAdapter(EmailStr)


async def bootstrap_admin_account(settings: Settings | None = None) -> str:
    """Create or promote the operator account described by the environment.

    Reads ``ADMIN_BOOTSTRAP_EMAIL`` and ``ADMIN_BOOTSTRAP_PASSWORD`` (the
    deprecated ``ADMIN_DEFAULT_EMAIL`` / ``ADMIN_DEFAULT_PASSWORD`` still
    work as aliases) from :class:`app.core.config.Settings`.

    The routine never raises: a misconfigured environment or an unreachable
    database is logged and the application keeps serving traffic.

    Args:
        settings: Settings to read from; the application singleton by default.

    Returns:
        One of ``"created"``, ``"promoted"``, ``"unchanged"`` or ``"skipped"``.
    """
    settings = settings or get_settings()
    email = (settings.admin_bootstrap_email or "").strip()
    password = settings.admin_bootstrap_password

    if not email and not password:
        await _warn_without_operator()
        return OUTCOME_SKIPPED

    if not email or not password:
        logger.error(
            "Admin bootstrap ignored: ADMIN_BOOTSTRAP_EMAIL and ADMIN_BOOTSTRAP_PASSWORD "
            "must be set together.",
            extra={"has_email": bool(email), "has_password": bool(password)},
        )
        return OUTCOME_SKIPPED

    email_error = validate_bootstrap_email(email)
    if email_error:
        logger.error("Admin bootstrap refused: %s", email_error, extra={"email": email})
        return OUTCOME_SKIPPED

    password_error = validate_bootstrap_password(password)
    if password_error:
        if settings.is_production:
            logger.error("Admin bootstrap refused: %s", password_error)
            return OUTCOME_SKIPPED
        logger.warning("Admin bootstrap: %s (accepted outside production)", password_error)

    try:
        # Every value that must never reach the logs: the password itself and
        # — once it is computed — its hash.  They are scrubbed from the error
        # message and from the traceback below.
        secrets: list[str] = [password]
        async with SessionLocal() as session:
            async with session.begin():
                return await _apply_bootstrap(session, settings, email, password, secrets)
    except Exception as exc:
        # The API must boot even when the operator account cannot be created.
        logger.error(
            "Admin bootstrap failed: %s\n%s",
            _scrub(str(exc), secrets),
            _scrub("".join(traceback.format_exception(exc)), secrets),
            extra={"email": email},
        )
        return OUTCOME_SKIPPED


def _scrub(text: str, secrets: list[str]) -> str:
    """Replace every secret in ``text`` with ``***``.

    The bootstrap must never log the password nor its hash — not even
    indirectly, through the message of a database error.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


async def _apply_bootstrap(
    session: AsyncSession,
    settings: Settings,
    email: str,
    password: str,
    secrets: list[str],
) -> str:
    """Create or promote the account inside the caller's transaction.

    ``secrets`` collects the values that must be scrubbed from the logs
    (the password and, once it is computed, its hash).
    """
    repo = UserRepository(session)
    user = await repo.get_by_email(email)

    if user is None:
        statement = _insert_statement(session, settings.admin_bootstrap_name, email, password, secrets)
        result = cast(CursorResult[Any], await session.execute(statement))
        if result.rowcount == 1:
            created = await repo.get_by_email(email)
            if created is not None:
                await _record_consents(session, created.id, settings)
                await _write_audit(session, ACTION_CREATED, created.id)
                logger.info(
                    "Operator account created",
                    extra={"email": email, "user_id": created.id, "action": ACTION_CREATED},
                )
                return OUTCOME_CREATED
        # The row was already there (another worker won the race): the account
        # exists, nothing is changed.
        user = await repo.get_by_email(email)

    if user is None:
        logger.warning("Operator account could not be created", extra={"email": email})
        return OUTCOME_UNCHANGED

    if user.role == UserRole.ADMIN:
        return OUTCOME_UNCHANGED

    if not settings.admin_bootstrap_promote_existing:
        logger.info(
            "Operator account exists with a different role and was left untouched",
            extra={"email": email, "role": user.role.value},
        )
        return OUTCOME_UNCHANGED

    user.role = UserRole.ADMIN
    # A role change invalidates the refresh tokens issued before now.
    user.refresh_token_valid_after = _invalidate_refresh_tokens()
    await session.flush()
    await _write_audit(session, ACTION_PROMOTED, user.id)
    logger.info(
        "Operator account promoted to ADMIN",
        extra={"email": email, "user_id": user.id, "action": ACTION_PROMOTED},
    )
    return OUTCOME_PROMOTED


def _insert_statement(
    session: AsyncSession,
    full_name: str,
    email: str,
    password: str,
    secrets: list[str],
) -> Any:
    """Build the ``INSERT ... ON CONFLICT (email) DO NOTHING`` statement.

    The statement is dialect-aware (PostgreSQL and SQLite are both
    supported) and idempotent: when the e-mail is already taken the insert
    is silently skipped, which is what keeps concurrent boots from creating
    two operators.

    The password is hashed here (and only here — an existing account is
    never re-hashed).  The hash is appended to ``secrets`` so it can be
    scrubbed from the logs if the insert fails.
    """
    bind = session.get_bind()
    dialect = bind.dialect.name if bind is not None else "sqlite"
    insert = postgresql_insert if dialect == "postgresql" else sqlite_insert
    now = datetime.now(UTC)
    password_hash = pwd_context.hash(password)
    secrets.append(password_hash)
    statement = insert(cast(Table, User.__table__)).values(
        full_name=full_name,
        email=email,
        password_hash=password_hash,
        role=UserRole.ADMIN,
        # ``refresh_token_valid_after`` is NOT NULL: truncate it to the second
        # so the very first refresh token issued to the account (issued
        # moments later, in the same second) is not rejected.
        refresh_token_valid_after=datetime.fromtimestamp(int(now.timestamp()), tz=UTC),
        birth_date=BOOTSTRAP_BIRTH_DATE,
    )
    return statement.on_conflict_do_nothing(index_elements=["email"])


async def _record_consents(session: AsyncSession, user_id: int, settings: Settings) -> None:
    """Record the ``terms`` and ``privacy`` consents the register flow creates."""
    accepted_at = datetime.now(UTC)
    versions = settings.consent_versions
    for consent_type in DEFAULT_CONSENT_TYPES:
        session.add(
            ConsentRecord(
                user_id=user_id,
                type=consent_type,
                version=versions.get(consent_type, ""),
                accepted_at=accepted_at,
            )
        )
    await session.flush()


async def _write_audit(session: AsyncSession, action: str, user_id: int) -> None:
    """Append one audit row for a bootstrap action (actor ``system``).

    The payload holds no secret and no personal data: the account is
    identified by its id, the e-mail would be masked by the audit redaction
    anyway.
    """
    await AdminAuditLogRepository(session).add(
        admin_id=None,
        admin_email=SYSTEM_ACTOR,
        action=action,
        entity="user",
        entity_id=str(user_id),
        before=None,
        after={"role": UserRole.ADMIN.value, "source": "env_bootstrap"},
        ip_address=None,
    )


async def _warn_without_operator() -> None:
    """Warn once when the operator console would be left without an operator.

    The check needs the database: an admin may already exist even when no
    bootstrap variable is configured (the normal state after the first
    deploy).
    """
    try:
        async with SessionLocal() as session:
            if await UserRepository(session).exists_admin():
                return
    except Exception:
        logger.warning("Could not verify whether an admin account exists", exc_info=True)
        return

    logger.warning(
        "No admin account exists and ADMIN_BOOTSTRAP_EMAIL/ADMIN_BOOTSTRAP_PASSWORD are not "
        "set — the /admin panel will have no operator.",
    )


def validate_bootstrap_email(email: str | None) -> str | None:
    """Return an error message when ``email`` is not a usable address.

    ``None`` means the address is acceptable.  The message never contains
    anything but the address itself.
    """
    candidate = (email or "").strip()
    if not candidate:
        return "ADMIN_BOOTSTRAP_EMAIL is empty."
    try:
        _EMAIL_ADAPTER.validate_python(candidate)
    except ValidationError:
        return f"ADMIN_BOOTSTRAP_EMAIL is not a valid e-mail address: {candidate!r}."
    return None


def validate_bootstrap_password(password: str | None) -> str | None:
    """Return an error message when ``password`` is too weak to be accepted.

    ``None`` means the password is acceptable.  The message never contains
    the password itself — only the rule that was broken.
    """
    if not password:
        return "ADMIN_BOOTSTRAP_PASSWORD is empty."
    if len(password) < MIN_BOOTSTRAP_PASSWORD_LENGTH:
        return f"ADMIN_BOOTSTRAP_PASSWORD must have at least {MIN_BOOTSTRAP_PASSWORD_LENGTH} characters."
    if password.strip().lower() in WEAK_BOOTSTRAP_PASSWORDS:
        return "ADMIN_BOOTSTRAP_PASSWORD is one of the well-known passwords that is never accepted."
    return None


def _invalidate_refresh_tokens() -> datetime:
    """Return the timestamp that invalidates the refresh tokens issued before now.

    The value is rounded **up** to the next whole second (a JWT ``iat`` is
    a whole second) so a token issued in the same second is still refused.
    """
    return datetime.fromtimestamp(int(datetime.now(UTC).timestamp()) + 1, tz=UTC)
