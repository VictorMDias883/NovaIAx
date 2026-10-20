"""
Service layer for the operator console's promotions section.

Wraps every write behind validation so the router stays a thin presentation
layer:

* promo-code CRUD, bulk generation and CSV export;
* promotions CRUD;
* manual "conceder plano a usuário" grants (a :class:`PromoRedemption` row with
  ``source="manual"`` and no code).

Nothing here changes a price or takes a payment; a manual grant is a purely
complimentary entitlement, which is exactly what the Google Play payments
policy permits outside Play Billing.
"""

from __future__ import annotations

import csv
import io
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.commands.grant_plan_command import GrantPlanCommand
from app.core.logging import get_logger
from app.models.promo_code import GRANTABLE_PLANS, PromoCode, PromoCodeKind
from app.models.promo_redemption import PromoRedemption
from app.models.promotion import Promotion, PromotionAudience, PromotionCtaTarget
from app.repositories.promo_code_repository import PromoCodeRepository
from app.repositories.promo_redemption_repository import PromoRedemptionRepository
from app.repositories.promotion_repository import PromotionRepository
from app.repositories.user_repository import UserRepository

logger = get_logger(__name__)

_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,39}$")
_GENERATED_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_GENERATED_LENGTH = 8


def normalize_code(raw: str) -> str:
    """Normalise a code to its stored, lower-case form."""
    return raw.strip().lower()


def _random_code(prefix: str, length: int = _GENERATED_LENGTH) -> str:
    """Return a random code body with ``prefix`` (uppercase, unambiguous)."""
    body = "".join(secrets.choice(_GENERATED_ALPHABET) for _ in range(length))
    return f"{prefix.upper()}-{body}"


class PromoAdminService:
    """CRUD + generation + stats for promo codes, promotions and grants."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.codes = PromoCodeRepository(session)
        self.redemptions = PromoRedemptionRepository(session)
        self.promotions = PromotionRepository(session)

    # -- Promo codes --------------------------------------------------------

    async def list_codes(self, *, include_inactive: bool = True) -> list[PromoCode]:
        """Return codes, newest first."""
        return await self.codes.list(include_inactive=include_inactive, limit=500)

    async def list_codes_with_usage(self, *, include_inactive: bool = True) -> list[dict[str, Any]]:
        """Return each code paired with its redemption and remaining counts."""
        codes = await self.list_codes(include_inactive=include_inactive)
        rows: list[dict[str, Any]] = []
        for code in codes:
            total = await self.redemptions.count_for_code(code.id)
            remaining = None if code.max_redemptions is None else max(code.max_redemptions - total, 0)
            rows.append({"code": code, "redemptions": total, "remaining": remaining})
        return rows

    async def list_recent_manual_grants(self, *, limit: int = 25) -> list[PromoRedemption]:
        """Return the most recent manual admin grants."""
        return await self.redemptions.list_by_source("manual", limit=limit)

    async def get_code(self, code_id: int) -> PromoCode:
        """Return a code or raise ``404``."""
        code = await self.codes.get_by_id(code_id)
        if code is None:
            raise HTTPException(status_code=404, detail="Código não encontrado.")
        return code

    async def create_code(self, values: dict[str, Any], *, admin_id: int | None) -> PromoCode:
        """Validate and create one promo code."""
        data = self._validate_code(values)
        normalised = data["code"]
        if await self.codes.get_by_code(normalised) is not None:
            raise HTTPException(status_code=400, detail="Já existe um código com esse valor.")
        code = PromoCode(**data, created_by=admin_id)
        self.codes.add(code)
        await self.session.commit()
        await self.session.refresh(code)
        logger.info("promo_code_created", extra={"code_id": code.id, "kind": code.kind})
        return code

    async def update_code(self, code_id: int, values: dict[str, Any]) -> PromoCode:
        """Validate and update one promo code."""
        code = await self.get_code(code_id)
        data = self._validate_code(values, code=code)
        self.codes.update_fields(code, data)
        await self.session.commit()
        await self.session.refresh(code)
        logger.info("promo_code_updated", extra={"code_id": code.id})
        return code

    async def toggle_code(self, code_id: int) -> PromoCode:
        """Flip a code's active flag."""
        code = await self.get_code(code_id)
        code.active = not code.active
        await self.session.commit()
        await self.session.refresh(code)
        return code

    async def bulk_generate(
        self, values: dict[str, Any], *, count: int, prefix: str, admin_id: int | None
    ) -> list[PromoCode]:
        """Generate ``count`` unique codes sharing the same grant definition."""
        if count < 1 or count > 500:
            raise HTTPException(status_code=400, detail="Informe uma quantidade entre 1 e 500.")
        clean_prefix = (prefix.strip() or "NOVA").upper()
        if not re.match(r"^[A-Z0-9]{1,12}$", clean_prefix):
            raise HTTPException(
                status_code=400, detail="Prefixo inválido (use letras e números, até 12)."
            )
        base = self._validate_code(values, require_code=False)
        base.pop("code", None)
        created: list[PromoCode] = []
        seen: set[str] = set()
        attempts = 0
        while len(created) < count and attempts < count * 20:
            attempts += 1
            candidate = normalize_code(_random_code(clean_prefix))
            if candidate in seen:
                continue
            seen.add(candidate)
            if await self.codes.get_by_code(candidate) is not None:
                continue
            code = PromoCode(**base, code=candidate, created_by=admin_id)
            self.codes.add(code)
            created.append(code)
        if len(created) < count:
            raise HTTPException(status_code=500, detail="Não foi possível gerar códigos únicos.")
        await self.session.commit()
        for code in created:
            await self.session.refresh(code)
        logger.info("promo_codes_bulk_generated", extra={"count": len(created)})
        return created

    async def code_stats(self, code_id: int) -> dict[str, Any]:
        """Return redemption counters for one code."""
        code = await self.get_code(code_id)
        total = await self.redemptions.count_for_code(code_id)
        remaining = None if code.max_redemptions is None else max(code.max_redemptions - total, 0)
        return {"code": code, "redemptions": total, "remaining": remaining}

    async def list_redemptions(self, code_id: int) -> list[PromoRedemption]:
        """Return the redemption ledger for a code."""
        await self.get_code(code_id)
        return await self.redemptions.list_for_code(code_id)

    async def export_csv(self) -> str:
        """Return every code (with usage) as a CSV document."""
        codes = await self.codes.list(include_inactive=True, limit=10_000)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "code",
                "kind",
                "plan",
                "duration_days",
                "bonus_messages",
                "max_redemptions",
                "per_user_limit",
                "active",
                "valid_from",
                "valid_until",
                "redemptions",
                "remaining",
                "created_at",
            ]
        )
        for code in codes:
            total = await self.redemptions.count_for_code(code.id)
            remaining = "" if code.max_redemptions is None else max(code.max_redemptions - total, 0)
            writer.writerow(
                [
                    code.code,
                    code.kind,
                    code.plan or "",
                    code.duration_days if code.duration_days is not None else "",
                    code.bonus_messages if code.bonus_messages is not None else "",
                    code.max_redemptions if code.max_redemptions is not None else "",
                    code.per_user_limit,
                    "sim" if code.active else "nao",
                    code.valid_from.isoformat() if code.valid_from else "",
                    code.valid_until.isoformat() if code.valid_until else "",
                    total,
                    remaining,
                    code.created_at.isoformat() if code.created_at else "",
                ]
            )
        return buffer.getvalue()

    def _validate_code(
        self, values: dict[str, Any], *, code: PromoCode | None = None, require_code: bool = True
    ) -> dict[str, Any]:
        """Validate and normalise promo-code form input."""
        kind = str(values.get("kind", "")).strip()
        if kind not in {member.value for member in PromoCodeKind}:
            raise HTTPException(status_code=400, detail="Tipo de código inválido.")

        data: dict[str, Any] = {
            "kind": kind,
            "plan": None,
            "duration_days": None,
            "bonus_messages": None,
            "max_redemptions": None,
            "per_user_limit": 1,
            "valid_from": None,
            "valid_until": None,
            "active": bool(values.get("active", True)),
            "note": (str(values.get("note") or "").strip() or None),
        }

        raw_code = values.get("code")
        if raw_code is not None or require_code:
            normalised = normalize_code(str(raw_code or ""))
            if not _CODE_RE.match(normalised):
                raise HTTPException(
                    status_code=400,
                    detail="Código inválido (3 a 40 caracteres: letras, números, '-' ou '_').",
                )
            data["code"] = normalised

        if kind in (PromoCodeKind.GRANT_PLAN.value, PromoCodeKind.EXTEND_TRIAL.value):
            plan = str(values.get("plan") or "").strip() or (
                "trial" if kind == PromoCodeKind.EXTEND_TRIAL.value else ""
            )
            if plan not in GRANTABLE_PLANS:
                raise HTTPException(status_code=400, detail="Plano concedido inválido.")
            duration = self._positive_int(values.get("duration_days"), "duração")
            if duration is None:
                raise HTTPException(status_code=400, detail="Informe a duração em dias.")
            data["plan"] = plan
            data["duration_days"] = duration
        elif kind == PromoCodeKind.BONUS_QUOTA.value:
            bonus = self._positive_int(values.get("bonus_messages"), "mensagens de bônus")
            if bonus is None:
                raise HTTPException(status_code=400, detail="Informe as mensagens de bônus.")
            data["bonus_messages"] = bonus
            data["duration_days"] = self._positive_int(values.get("duration_days"), "duração")

        data["max_redemptions"] = self._positive_int(values.get("max_redemptions"), "limite total")
        per_user = self._positive_int(values.get("per_user_limit"), "limite por usuário")
        data["per_user_limit"] = per_user or 1
        data["valid_from"] = self._parse_dt(values.get("valid_from"))
        data["valid_until"] = self._parse_dt(values.get("valid_until"))
        if data["valid_from"] and data["valid_until"] and data["valid_from"] > data["valid_until"]:
            raise HTTPException(status_code=400, detail="A validade inicial deve ser anterior à final.")
        return data

    @staticmethod
    def _positive_int(raw: Any, label: str) -> int | None:
        """Parse an optional positive int, rejecting non-numeric input."""
        if raw is None or str(raw).strip() == "":
            return None
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"Valor inválido de {label}.") from exc
        if value <= 0:
            raise HTTPException(status_code=400, detail=f"Valor inválido de {label}.")
        return value

    @staticmethod
    def _parse_dt(raw: Any) -> datetime | None:
        """Parse a datetime-local form value into a UTC-aware datetime."""
        if raw is None or str(raw).strip() == "":
            return None
        text = str(raw).strip()
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Data inválida.") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed

    # -- Promotions ---------------------------------------------------------

    async def list_promotions(self, *, include_inactive: bool = True) -> list[Promotion]:
        """Return promotions, best first."""
        return await self.promotions.list(include_inactive=include_inactive, limit=500)

    async def get_promotion(self, promotion_id: int) -> Promotion:
        """Return a promotion or raise ``404``."""
        promotion = await self.promotions.get_by_id(promotion_id)
        if promotion is None:
            raise HTTPException(status_code=404, detail="Promoção não encontrada.")
        return promotion

    async def create_promotion(self, values: dict[str, Any]) -> Promotion:
        """Validate and create a promotion card."""
        data = self._validate_promotion(values)
        promotion = Promotion(**data)
        self.promotions.add(promotion)
        await self.session.commit()
        await self.session.refresh(promotion)
        logger.info("promotion_created", extra={"promotion_id": promotion.id})
        return promotion

    async def update_promotion(self, promotion_id: int, values: dict[str, Any]) -> Promotion:
        """Validate and update a promotion card."""
        promotion = await self.get_promotion(promotion_id)
        data = self._validate_promotion(values)
        self.promotions.update_fields(promotion, data)
        await self.session.commit()
        await self.session.refresh(promotion)
        logger.info("promotion_updated", extra={"promotion_id": promotion.id})
        return promotion

    async def toggle_promotion(self, promotion_id: int) -> Promotion:
        """Flip a promotion's active flag."""
        promotion = await self.get_promotion(promotion_id)
        promotion.active = not promotion.active
        await self.session.commit()
        await self.session.refresh(promotion)
        return promotion

    async def delete_promotion(self, promotion_id: int) -> None:
        """Delete a promotion card."""
        promotion = await self.get_promotion(promotion_id)
        await self.session.delete(promotion)
        await self.session.commit()
        logger.info("promotion_deleted", extra={"promotion_id": promotion_id})

    def _validate_promotion(self, values: dict[str, Any]) -> dict[str, Any]:
        """Validate and normalise promotion form input."""
        title = str(values.get("title") or "").strip()
        message = str(values.get("message") or "").strip()
        if not title or len(title) > 120:
            raise HTTPException(status_code=400, detail="Informe um título (até 120 caracteres).")
        if not message:
            raise HTTPException(status_code=400, detail="Informe a mensagem da promoção.")

        cta_target = str(values.get("cta_target") or "plans").strip()
        if cta_target not in {member.value for member in PromotionCtaTarget}:
            raise HTTPException(status_code=400, detail="Destino do botão inválido.")
        cta_label = str(values.get("cta_label") or "").strip() or None
        cta_url = str(values.get("cta_url") or "").strip() or None
        if cta_target == PromotionCtaTarget.URL.value and not cta_url:
            raise HTTPException(status_code=400, detail="Informe a URL do botão.")

        audience = str(values.get("audience") or "all").strip()
        if audience not in {member.value for member in PromotionAudience}:
            raise HTTPException(status_code=400, detail="Público inválido.")
        audience_plans = self._split_list(values.get("audience_plans"))
        audience_groups = self._split_list(values.get("audience_groups"))
        if audience == PromotionAudience.PLAN.value and not audience_plans:
            raise HTTPException(status_code=400, detail="Selecione ao menos um plano do público.")
        if audience == PromotionAudience.GROUPS.value and not audience_groups:
            raise HTTPException(status_code=400, detail="Selecione ao menos um grupo do público.")

        emoji = str(values.get("emoji") or "").strip() or None
        image_url = str(values.get("image_url") or "").strip() or None
        starts_at = self._parse_dt(values.get("starts_at"))
        ends_at = self._parse_dt(values.get("ends_at"))
        if starts_at and ends_at and starts_at > ends_at:
            raise HTTPException(status_code=400, detail="O início deve ser anterior ao fim.")

        return {
            "title": title,
            "message": message,
            "emoji": emoji,
            "image_url": image_url,
            "cta_label": cta_label,
            "cta_target": cta_target,
            "cta_url": cta_url,
            "audience": audience,
            "audience_plans": audience_plans or None,
            "audience_groups": audience_groups or None,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "active": bool(values.get("active", True)),
            "priority": self._int_or_zero(values.get("priority")),
        }

    @staticmethod
    def _split_list(raw: Any) -> list[str]:
        """Split a comma-separated form value into a clean list."""
        if raw is None:
            return []
        parts = (
            [str(item) for item in raw]
            if isinstance(raw, list)
            else str(raw).replace(";", ",").split(",")
        )
        return [part.strip() for part in parts if part.strip()]

    @staticmethod
    def _int_or_zero(raw: Any) -> int:
        """Parse an int, defaulting to ``0`` when missing or invalid."""
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return 0

    # -- Manual grants ------------------------------------------------------

    async def grant_plan(self, command: GrantPlanCommand) -> PromoRedemption:
        """Grant a plan to a user directly, recorded with ``source="manual"``."""
        if command.plan not in GRANTABLE_PLANS:
            raise HTTPException(status_code=400, detail="Plano inválido.")
        if command.duration_days <= 0 or command.duration_days > 3650:
            raise HTTPException(status_code=400, detail="Duração inválida (1 a 3650 dias).")
        if await UserRepository(self.session).get_by_id(command.user_id) is None:
            raise HTTPException(status_code=404, detail="Usuário não encontrado.")

        now = datetime.now(UTC)
        redemption = PromoRedemption(
            code_id=None,
            user_id=command.user_id,
            source="manual",
            plan=command.plan,
            granted_until=now + timedelta(days=command.duration_days),
            reason=command.reason.strip() or None,
            created_by=command.admin_id,
        )
        self.redemptions.add(redemption)
        await self.session.commit()
        await self.session.refresh(redemption)
        logger.info(
            "manual_plan_granted",
            extra={"user_id": command.user_id, "plan": command.plan},
        )
        return redemption


__all__ = ["PromoAdminService", "normalize_code"]
