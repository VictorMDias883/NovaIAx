from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.commands.change_password_command import ChangePasswordCommand
from app.commands.delete_user_command import DeleteUserCommand
from app.commands.demote_user_command import DemoteUserCommand
from app.commands.promote_user_command import PromoteUserCommand
from app.commands.update_profile_command import UpdateProfileCommand
from app.core.logging import get_logger
from app.core.security import pwd_context
from app.models.user import UserRole
from app.repositories.promo_redemption_repository import PromoRedemptionRepository
from app.repositories.user_repository import UserRepository
from app.services.subscription_service import SubscriptionService

logger = get_logger(__name__)


def _password_matches(password: str, password_hash: str) -> bool:
    """Return whether ``password`` verifies against ``password_hash``.

    A malformed or unknown hash never crashes the request: it is treated as
    a mismatch, mirroring :func:`app.db.bootstrap._hash_matches`.
    """
    try:
        return pwd_context.verify(password, password_hash)
    except Exception:
        return False


class UserService:
    """Business logic for user administration, role management and self-service."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = UserRepository(session)

    @staticmethod
    def _invalidate_refresh_tokens() -> datetime:
        """Return the timestamp that turns off every refresh token minted so far.

        A JWT ``iat`` is an integer by specification, and
        :func:`app.services.auth_service.refresh_token` compares ``iat``
        against the *floor* of ``refresh_token_valid_after``.  Storing the
        raw ``datetime.now(UTC)`` mid-second (``N.something``) floors back to
        ``N``, which would let a token minted earlier in second ``N`` survive
        a credential change made in that same second.  Rounding up to the
        *next* whole second makes the truncation strict: every token from the
        change's own second is turned off, and a later login (next second
        onwards) still works.  Account creation uses a second-truncated value
        instead (see ``User.refresh_token_valid_after``) so the very first
        token a user receives is never rejected.
        """
        return datetime.fromtimestamp(int(datetime.now(UTC).timestamp()) + 1, tz=UTC)

    async def promote(self, actor: dict[str, Any], command: PromoteUserCommand) -> dict[str, str]:
        """Promote a regular user to administrator."""
        self._ensure_admin(actor)

        async with self.session.begin():
            target = await self.repo.get_by_id(command.target_id, for_update=True)
            if target is None:
                raise HTTPException(status_code=404, detail="Usuário não encontrado.")

            if target.role == UserRole.ADMIN:
                return {"message": "User is already an administrator."}

            target.role = UserRole.ADMIN
            target.refresh_token_valid_after = self._invalidate_refresh_tokens()
            await self.session.flush()
            await self.session.refresh(target)

        logger.info(
            "User promoted",
            extra={"actor_id": actor.get("id"), "target_id": target.id},
        )
        return {"message": "User promoted to administrator."}

    async def demote(self, actor: dict[str, Any], command: DemoteUserCommand) -> dict[str, str]:
        """Demote an administrator to a regular user."""
        self._ensure_admin(actor)

        async with self.session.begin():
            target = await self.repo.get_by_id(command.target_id, for_update=True)
            if target is None:
                raise HTTPException(status_code=404, detail="Usuário não encontrado.")

            if target.role != UserRole.ADMIN:
                return {"message": "User is already a regular user."}

            admin_count = await self.repo.count_admins(for_update=True)
            if admin_count <= 1:
                raise HTTPException(status_code=400, detail="Não é possível rebaixar o último administrador.")

            target.role = UserRole.USER
            target.refresh_token_valid_after = self._invalidate_refresh_tokens()
            await self.session.flush()
            await self.session.refresh(target)

        logger.info(
            "User demoted",
            extra={"actor_id": actor.get("id"), "target_id": target.id},
        )
        return {"message": "User demoted to regular user."}

    async def delete(self, actor: dict[str, Any], command: DeleteUserCommand) -> dict[str, str]:
        """Delete a user account by ID."""
        self._ensure_admin(actor)

        async with self.session.begin():
            target = await self.repo.get_by_id(command.target_id, for_update=True)
            if target is None:
                raise HTTPException(status_code=404, detail="Usuário não encontrado.")

            if target.role == UserRole.ADMIN:
                admin_count = await self.repo.count_admins(for_update=True)
                if admin_count <= 1:
                    raise HTTPException(status_code=400, detail="Não é possível excluir o último administrador.")

            await self.session.delete(target)

        logger.info(
            "User deleted",
            extra={"actor_id": actor.get("id"), "target_id": target.id},
        )
        return {"message": "User deleted."}

    async def update_profile(self, actor: dict[str, Any], command: UpdateProfileCommand) -> dict[str, Any]:
        """Update the authenticated user's own display name.

        This is self-service: the target is always the caller identified by
        ``actor["id"]`` — there is deliberately no way to point it at another
        account, and the role is never touched.

        The row is locked for the duration of the transaction so concurrent
        saves cannot silently overwrite each other.

        Args:
            actor: The authenticated caller's identity.
            command: The new display name.

        Returns:
            A dict with the refreshed ``id``, ``full_name``, ``email`` and
            ``role`` so the caller can update its cached profile in place.

        Raises:
            HTTPException(404): If the caller no longer exists (deleted
                while holding a still-valid access token).
        """
        async with self.session.begin():
            user = await self.repo.get_by_id(int(actor["id"]), for_update=True)
            if user is None:
                raise HTTPException(status_code=404, detail="Usuário não encontrado.")

            user.full_name = command.full_name
            await self.session.flush()
            await self.session.refresh(user)

        logger.info("User profile updated", extra={"user_id": user.id})
        return {
            "id": user.id,
            "full_name": user.full_name,
            "email": user.email,
            "role": user.role.value,
        }

    async def change_password(self, actor: dict[str, Any], command: ChangePasswordCommand) -> dict[str, str]:
        """Rotate the authenticated user's own password.

        The current password must be supplied and must verify against the
        stored hash, so a stolen access token alone cannot lock the real
        owner out of their account.

        On success every refresh token issued *before* the change is
        invalidated by bumping ``refresh_token_valid_after`` to now — the
        same mechanism :meth:`promote` and :meth:`demote` use.  Already
        issued access tokens keep working until they expire (they are
        short-lived), and the caller is expected to start a fresh session
        with the new credentials.

        Args:
            actor: The authenticated caller's identity.
            command: The current and replacement passwords.

        Returns:
            ``{"message": "Password updated."}``.

        Raises:
            HTTPException(404): If the caller no longer exists.
            HTTPException(400): If ``current_password`` does not verify, or
                the new password equals the current one.

        .. note::
           A wrong current password is reported as **400**, not 401.  A 401
           on this route would be indistinguishable from an expired access
           token to the client, which is free to treat it as a session
           problem and sign the user out.  Keeping it a 400 means a typo
           costs the user one error message instead of their session.
        """
        async with self.session.begin():
            user = await self.repo.get_by_id(int(actor["id"]), for_update=True)
            if user is None:
                raise HTTPException(status_code=404, detail="Usuário não encontrado.")

            if not _password_matches(command.current_password, user.password_hash):
                raise HTTPException(status_code=400, detail="A senha atual está incorreta.")

            if command.new_password == command.current_password:
                raise HTTPException(
                    status_code=400,
                    detail="A nova senha deve ser diferente da senha atual.",
                )

            user.password_hash = pwd_context.hash(command.new_password)
            # Invalidate refresh tokens minted before this change.
            user.refresh_token_valid_after = self._invalidate_refresh_tokens()
            await self.session.flush()
            await self.session.refresh(user)

        logger.info("User password changed", extra={"user_id": user.id})
        return {"message": "Password updated."}

    async def list_users(
        self, page: int = 1, limit: int = 20, query: str | None = None
    ) -> dict[str, Any]:
        """Return a paginated, optionally filtered list of users.

        ``query`` matches the e-mail, display name or numeric id.  Sensitive
        fields (password hash, TOTP secret) are never included.
        """
        page = max(1, page)
        limit = min(max(1, limit), 100)
        rows, total = await self.repo.search(query, page=page, limit=limit)
        return {
            "users": [
                {
                    "id": user.id,
                    "full_name": user.full_name,
                    "email": user.email,
                    "role": user.role.value,
                    "created_at": user.created_at,
                    "last_login_at": user.last_login_at,
                    "suspended": user.suspended_at is not None,
                    "community_banned": user.community_banned_at is not None,
                }
                for user in rows
            ],
            "page": page,
            "limit": limit,
            "total": total,
            "query": (query or "").strip(),
        }

    async def get_user_detail(self, user_id: int) -> dict[str, Any]:
        """Return the full, read-only profile shown on the user detail page.

        Only *metadata* is exposed: role, plan/subscription, promo grants,
        consent records and bookkeeping timestamps.  The operator never gets
        access to the user's private content (chats, notes, health, finance…).
        """
        # Imported lazily: ``privacy_service`` imports ``user_service`` at
        # module import time, so a top-level import here would be circular.
        from app.services.privacy_service import ConsentService

        user = await self.repo.get_by_id(user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="Usuário não encontrado.")

        profile = {
            "id": user.id,
            "full_name": user.full_name,
            "email": user.email,
            "role": user.role.value,
            "birth_date": user.birth_date,
            "created_at": user.created_at,
            "last_login_at": user.last_login_at,
            "suspended_at": user.suspended_at,
            "community_banned_at": user.community_banned_at,
        }
        consents = await ConsentService(self.session).list(user_id)
        grant_rows = await PromoRedemptionRepository(self.session).list_for_user(user_id)
        grants = [
            {
                "plan": grant.plan,
                "source": grant.source,
                "granted_until": grant.granted_until,
                "bonus_messages": grant.bonus_messages,
                "reason": grant.reason,
                "redeemed_at": grant.redeemed_at,
            }
            for grant in grant_rows
        ]
        entitlement = await SubscriptionService(self.session).resolve_entitlement(user_id)
        subscription = {
            "plan": entitlement.plan.value,
            "status": entitlement.status.value if entitlement.status is not None else None,
            "source": entitlement.source,
            "product_id": entitlement.product_id,
            "expires_at": entitlement.expires_at,
            "auto_renewing": entitlement.auto_renewing,
            "is_trial": entitlement.is_trial,
            "has_subscription": entitlement.has_subscription,
            "subscription_inactive": entitlement.subscription_inactive,
        }
        # Hand the session back clean so a subsequent action can own its own
        # write transaction (see :func:`app.api.deps._release_read_transaction`).
        await self.session.rollback()
        return {
            "user": profile,
            "consents": consents,
            "grants": grants,
            "subscription": subscription,
        }

    async def set_suspended(self, actor: dict[str, Any], user_id: int, *, suspended: bool) -> dict[str, str]:
        """Suspend or unsuspend an account.

        A suspended account is refused on every API call and cannot refresh
        its tokens (see :func:`app.api.deps._load_user`).  The last remaining
        administrator can never be suspended, mirroring the demote guard.
        """
        self._ensure_admin(actor)
        async with self.session.begin():
            target = await self.repo.get_by_id(user_id, for_update=True)
            if target is None:
                raise HTTPException(status_code=404, detail="Usuário não encontrado.")
            if suspended and target.role == UserRole.ADMIN and await self.repo.count_admins(for_update=True) <= 1:
                raise HTTPException(
                    status_code=400,
                    detail="Não é possível suspender o último administrador.",
                )
            target.suspended_at = datetime.now(UTC) if suspended else None
            target.refresh_token_valid_after = self._invalidate_refresh_tokens()
            await self.session.flush()
        logger.info(
            "User suspension changed",
            extra={"actor_id": actor.get("id"), "target_id": user_id, "suspended": suspended},
        )
        return {"message": "Suspensão atualizada."}

    async def force_logout(self, actor: dict[str, Any], user_id: int) -> dict[str, str]:
        """Invalidate every refresh token issued to the user so far."""
        self._ensure_admin(actor)
        async with self.session.begin():
            target = await self.repo.get_by_id(user_id, for_update=True)
            if target is None:
                raise HTTPException(status_code=404, detail="Usuário não encontrado.")
            target.refresh_token_valid_after = self._invalidate_refresh_tokens()
            await self.session.flush()
        logger.info("User force-logout", extra={"actor_id": actor.get("id"), "target_id": user_id})
        return {"message": "Sessões encerradas."}

    async def set_community_banned(
        self, actor: dict[str, Any], user_id: int, *, banned: bool
    ) -> dict[str, str]:
        """Bar (or allow) a user from posting community reports."""
        self._ensure_admin(actor)
        async with self.session.begin():
            target = await self.repo.get_by_id(user_id, for_update=True)
            if target is None:
                raise HTTPException(status_code=404, detail="Usuário não encontrado.")
            target.community_banned_at = datetime.now(UTC) if banned else None
            await self.session.flush()
        logger.info(
            "Community ban changed",
            extra={"actor_id": actor.get("id"), "target_id": user_id, "banned": banned},
        )
        return {"message": "Permissão de postagem atualizada."}

    def _ensure_admin(self, actor: dict[str, Any]) -> None:
        if str(actor.get("role")) != UserRole.ADMIN.value:
            raise HTTPException(status_code=403, detail="Não autorizado.")
