"""
Service layer for the community-reporting domain.

Safety invariants (do not weaken):

* the **support** endpoint is read-only — nothing a caller sends there is
  stored or published;
* report photos are stored **EXIF-stripped** (see
  :mod:`app.utils.image_exif`); geolocation never leaves the device;
* a draft complaint is a *template the user edits and sends themselves* — the
  backend never files it and never gives legal advice; every generated draft
  carries a ``disclaimer``;
* a caller never sees reports authored by users they have blocked, and a
  removed report is hidden from everyone but its author.

    Router → Service → Repository → Database
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.ai_router import AIRouter, run_ai_request
from app.clients.ai_types import AIRequest, AITaskType, PlanTier
from app.core.config import get_settings
from app.models.community import (
    CommunityReport,
    DraftComplaint,
    OfficialContactCategory,
    ReportCategory,
    ReportStatus,
    UserBlock,
)
from app.repositories.community_repository import (
    CommunityReportRepository,
    DraftComplaintRepository,
    OfficialContactRepository,
    ReportConfirmationRepository,
    ReportFlagRepository,
    UserBlockRepository,
)
from app.repositories.user_repository import UserRepository
from app.utils.community_media import delete_community_photo
from app.utils.json_extraction import extract_json

#: Shown alongside every generated draft complaint.
DRAFT_DISCLAIMER = (
    "Rascunho gerado por IA para você revisar e editar. Não é orientação "
    "jurídica; verifique os dados e envie você mesmo pelo canal oficial."
)

_DRAFT_SYSTEM_PROMPT = (
    "Você redige um rascunho de reclamação em português a partir do relato do "
    "usuário. Responda EXCLUSIVAMENTE em JSON no formato "
    '{"recipient": "órgão ou destinatário sugerido", "body": "texto do rascunho"}. '
    "Não dê orientação jurídica, não prometa resultados e não invente leis. "
    "Mantenha o texto objetivo e revise os fatos relatados. Não inclua texto fora do JSON."
)

#: Read-only directory of domestic-violence support lines (never persisted).
SUPPORT_RESOURCES: list[dict[str, str]] = [
    {
        "name": "Ligue 180 — Central de Atendimento à Mulher",
        "phone": "180",
        "description": "Orientação e denúncia de violência contra a mulher. Gratuito e sigiloso.",
    },
    {
        "name": "Disque 100 — Direitos Humanos",
        "phone": "100",
        "description": "Denúncias de violações de direitos humanos. Gratuito e anônimo.",
    },
    {
        "name": "Polícia Militar — emergência",
        "phone": "190",
        "description": "Emergências em andamento.",
    },
    {
        "name": "SAMU — emergência médica",
        "phone": "192",
        "description": "Emergências médicas.",
    },
    {
        "name": "CVV — apoio emocional",
        "phone": "188",
        "description": "Apoio emocional e prevenção do suicídio, 24h.",
    },
]


class CommunityService:
    """Reports, moderation signals, blocks and drafts for one user."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        ai_client: AIRouter | None = None,
    ) -> None:
        self.session = session
        self.ai_client = ai_client
        self.reports = CommunityReportRepository(session)
        self.confirmations = ReportConfirmationRepository(session)
        self.flags = ReportFlagRepository(session)
        self.blocks = UserBlockRepository(session)
        self.contacts = OfficialContactRepository(session)
        self.drafts = DraftComplaintRepository(session)

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------
    async def create_report(self, user_id: int, payload) -> CommunityReport:  # noqa: ANN001
        author = await UserRepository(self.session).get_by_id(user_id)
        if author is not None and author.community_banned_at is not None:
            raise HTTPException(
                status_code=403,
                detail="Você está impedido de publicar relatos no momento.",
            )
        return await self.reports.create(
            user_id,
            category=payload.category,
            title=payload.title,
            description=payload.description,
            location=payload.location,
            occurred_at=payload.occurred_at,
            is_anonymous=payload.is_anonymous,
            status=ReportStatus.SUBMITTED,
        )

    async def _report_view(self, report: CommunityReport, user_id: int, *, confirmed: bool) -> dict:
        return {
            "id": report.id,
            "category": report.category,
            "title": report.title,
            "description": report.description,
            "location": report.location,
            "occurred_at": report.occurred_at,
            "status": report.status,
            "is_anonymous": report.is_anonymous,
            "author_id": None if report.is_anonymous else report.user_id,
            "photo_path": report.photo_path,
            "confirmations": await self.confirmations.count_for_report(report.id),
            "flags": await self.flags.count_for_report(report.id),
            "confirmed_by_me": confirmed,
            "hidden": report.hidden_at is not None,
            "created_at": report.created_at,
            "updated_at": report.updated_at,
        }

    async def list_reports(
        self,
        user_id: int,
        *,
        page: int,
        limit: int,
        category: ReportCategory | None = None,
    ) -> tuple[list[dict], int]:
        blocked = await self.blocks.list_blocked_ids(user_id)
        rows, total = await self.reports.list_visible(
            user_id,
            category=category,
            excluded_user_ids=blocked,
            offset=(page - 1) * limit,
            limit=limit,
        )
        report_ids = [row.id for row in rows]
        confirmation_counts = await self.confirmations.counts_for_reports(report_ids)
        flag_counts = await self.flags.counts_for_reports(report_ids)
        my_confirmations = await self.confirmations.report_ids_confirmed_by_user(report_ids, user_id)
        views = [
            {
                "id": row.id,
                "category": row.category,
                "title": row.title,
                "description": row.description,
                "location": row.location,
                "occurred_at": row.occurred_at,
                "status": row.status,
                "is_anonymous": row.is_anonymous,
                "author_id": None if row.is_anonymous else row.user_id,
                "photo_path": row.photo_path,
                "confirmations": confirmation_counts.get(row.id, 0),
                "flags": flag_counts.get(row.id, 0),
                "confirmed_by_me": row.id in my_confirmations,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
            for row in rows
        ]
        return views, total

    async def _owned_report(self, user_id: int, report_id: int) -> CommunityReport:
        report = await self.reports.get(report_id)
        if report is None or report.user_id != user_id:
            raise HTTPException(status_code=404, detail="Relato não encontrado")
        return report

    async def get_report(self, user_id: int, report_id: int) -> dict:
        report = await self.reports.get(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Relato não encontrado")
        blocked = await self.blocks.list_blocked_ids(user_id)
        if report.status is ReportStatus.REMOVED and report.user_id != user_id:
            raise HTTPException(status_code=404, detail="Relato não encontrado")
        if report.hidden_at is not None and report.user_id != user_id:
            raise HTTPException(status_code=404, detail="Relato não encontrado")
        if report.user_id in blocked and report.user_id != user_id:
            raise HTTPException(status_code=404, detail="Relato não encontrado")
        confirmed = await self.confirmations.get(report_id, user_id) is not None
        return await self._report_view(report, user_id, confirmed=confirmed)

    async def update_report(self, user_id: int, report_id: int, payload) -> dict:  # noqa: ANN001
        report = await self._owned_report(user_id, report_id)
        await self.reports.update(report, **payload.model_dump(exclude_unset=True))
        confirmed = await self.confirmations.get(report_id, user_id) is not None
        return await self._report_view(report, user_id, confirmed=confirmed)

    async def delete_report(self, user_id: int, report_id: int) -> None:
        report = await self._owned_report(user_id, report_id)
        photo_path = report.photo_path
        await self.reports.delete(report)
        if photo_path:
            delete_community_photo(photo_path, get_settings().community_media_dir)

    async def set_photo_path(self, user_id: int, report_id: int, photo_path: str) -> CommunityReport:
        report = await self._owned_report(user_id, report_id)
        return await self.reports.update(report, photo_path=photo_path)

    # ------------------------------------------------------------------
    # Moderation (admin only)
    # ------------------------------------------------------------------
    async def list_reports_for_moderation(
        self,
        *,
        status: ReportStatus | None = None,
        page: int = 1,
        limit: int = 25,
    ) -> tuple[list[dict], int]:
        """Return a page of reports for the admin moderation queue.

        Unlike :meth:`list_reports` this is **not** user-scoped: it is called
        only from the ADMIN-only panel, so it lists every report (optionally
        filtered by ``status``) and discloses the author id even for anonymous
        reports — a moderator must be able to act on abuse.
        """
        rows, total = await self.reports.list_for_moderation(
            status=status, offset=(page - 1) * limit, limit=limit
        )
        report_ids = [row.id for row in rows]
        confirmation_counts = await self.confirmations.counts_for_reports(report_ids)
        flag_counts = await self.flags.counts_for_reports(report_ids)
        reason_map: dict[int, list[str]] = {}
        for row in rows:
            flags = await self.flags.list_for_report(row.id)
            reason_map[row.id] = [flag.reason for flag in flags if flag.reason]
        views = [
            {
                "id": row.id,
                "category": row.category.value,
                "title": row.title,
                "description": row.description,
                "location": row.location,
                "occurred_at": row.occurred_at,
                "status": row.status.value,
                "is_anonymous": row.is_anonymous,
                "author_id": row.user_id,
                "photo_path": row.photo_path,
                "confirmations": confirmation_counts.get(row.id, 0),
                "flags": flag_counts.get(row.id, 0),
                "flag_reasons": reason_map.get(row.id, []),
                "hidden": row.hidden_at is not None,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
            for row in rows
        ]
        return views, total

    async def moderate_report(self, report_id: int, status: ReportStatus) -> dict:
        """Apply an admin moderation decision to a report.

        Setting the status to ``REMOVED`` hides the report from everyone but
        its author (see :meth:`get_report`).  Raises 404 when the report no
        longer exists.
        """
        report = await self.reports.get(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Relato não encontrado")
        await self.reports.update(report, status=status)
        return {
            "id": report.id,
            "status": report.status.value,
        }

    async def restore_report(self, report_id: int) -> dict:
        """Bring an auto-hidden or removed report back to the visible feed."""
        report = await self.reports.get(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Relato não encontrado")
        await self.reports.update(
            report,
            status=ReportStatus.CONFIRMED,
            hidden_at=None,
        )
        return {"id": report.id, "status": report.status.value, "hidden": False}

    async def resolve_flags(self, report_id: int) -> dict:
        """Clear every user flag on a report and un-flag it if needed."""
        report = await self.reports.get(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Relato não encontrado")
        removed = await self.flags.delete_for_report(report_id)
        if report.status is ReportStatus.FLAGGED:
            await self.reports.update(report, status=ReportStatus.CONFIRMED)
        return {"id": report.id, "resolved_flags": removed}

    # ------------------------------------------------------------------
    # Confirmations / flags
    # ------------------------------------------------------------------
    async def _visible_report(self, user_id: int, report_id: int) -> CommunityReport:
        report = await self.reports.get(report_id)
        if report is None or report.status is ReportStatus.REMOVED:
            raise HTTPException(status_code=404, detail="Relato não encontrado")
        return report

    async def confirm_report(self, user_id: int, report_id: int) -> dict:
        report = await self._visible_report(user_id, report_id)
        if report.user_id == user_id:
            raise HTTPException(status_code=400, detail="Você não pode confirmar o próprio relato")
        existing = await self.confirmations.get(report_id, user_id)
        if existing is None:
            await self.confirmations.create(report_id, user_id)
        count = await self.confirmations.count_for_report(report_id)
        return {"report_id": report_id, "confirmed": True, "confirmations": count}

    async def unconfirm_report(self, user_id: int, report_id: int) -> dict:
        await self._visible_report(user_id, report_id)
        existing = await self.confirmations.get(report_id, user_id)
        if existing is not None:
            await self.confirmations.delete(existing)
        count = await self.confirmations.count_for_report(report_id)
        return {"report_id": report_id, "confirmed": False, "confirmations": count}

    async def flag_report(self, user_id: int, report_id: int, reason: str | None) -> dict:
        report = await self._visible_report(user_id, report_id)
        if await self.flags.get(report_id, user_id) is None:
            await self.flags.create(report_id, user_id, reason)
        count = await self.flags.count_for_report(report_id)
        # Auto-hide once enough distinct users flag the report.  The report is
        # hidden from the public feed (its ``hidden_at`` is stamped) and shows
        # up first in the admin moderation queue for a human decision.
        threshold = get_settings().community_report_flag_threshold
        if threshold > 0 and count >= threshold and report.hidden_at is None:
            await self.reports.set_hidden(report, datetime.now(UTC))
            if report.status is not ReportStatus.REMOVED:
                await self.reports.update(report, status=ReportStatus.FLAGGED)
        return {"report_id": report_id, "flagged": True, "flags": count}

    # ------------------------------------------------------------------
    # Blocks
    # ------------------------------------------------------------------
    async def block_user(self, user_id: int, blocked_id: int) -> UserBlock:
        if blocked_id == user_id:
            raise HTTPException(status_code=400, detail="Você não pode bloquear a si mesmo")
        existing = await self.blocks.get(user_id, blocked_id)
        if existing is not None:
            return existing
        return await self.blocks.create(user_id, blocked_id)

    async def unblock_user(self, user_id: int, blocked_id: int) -> None:
        existing = await self.blocks.get(user_id, blocked_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Bloqueio não encontrado")
        await self.blocks.delete(existing)

    async def list_blocks(self, user_id: int) -> list[UserBlock]:
        blocked_ids = await self.blocks.list_blocked_ids(user_id)
        result: list[UserBlock] = []
        for blocked_id in blocked_ids:
            block = await self.blocks.get(user_id, blocked_id)
            if block is not None:
                result.append(block)
        return result

    # ------------------------------------------------------------------
    # Official contacts / support (read-only)
    # ------------------------------------------------------------------
    async def list_official_contacts(self, *, category: OfficialContactCategory | None = None):
        return await self.contacts.list_all(category=category)

    @staticmethod
    def support_resources() -> list[dict[str, str]]:
        return list(SUPPORT_RESOURCES)

    # ------------------------------------------------------------------
    # Draft complaints
    # ------------------------------------------------------------------
    @staticmethod
    def _draft_view(draft: DraftComplaint) -> dict:
        return {
            "id": draft.id,
            "report_id": draft.report_id,
            "recipient": draft.recipient,
            "body": draft.body,
            "disclaimer": DRAFT_DISCLAIMER,
            "created_at": draft.created_at,
            "updated_at": draft.updated_at,
        }

    async def create_draft(self, user_id: int, payload) -> dict:  # noqa: ANN001
        if payload.report_id is not None:
            await self._owned_report(user_id, payload.report_id)
        draft = await self.drafts.create(
            user_id, body=payload.body, recipient=payload.recipient, report_id=payload.report_id
        )
        return self._draft_view(draft)

    async def list_drafts(self, user_id: int, *, page: int, limit: int) -> tuple[list[dict], int]:
        rows, total = await self.drafts.list_for_user(user_id, offset=(page - 1) * limit, limit=limit)
        return [self._draft_view(row) for row in rows], total

    async def update_draft(self, user_id: int, draft_id: int, payload) -> dict:  # noqa: ANN001
        draft = await self.drafts.get(draft_id)
        if draft is None or draft.user_id != user_id:
            raise HTTPException(status_code=404, detail="Rascunho não encontrado")
        await self.drafts.update(draft, **payload.model_dump(exclude_unset=True))
        return self._draft_view(draft)

    async def delete_draft(self, user_id: int, draft_id: int) -> None:
        draft = await self.drafts.get(draft_id)
        if draft is None or draft.user_id != user_id:
            raise HTTPException(status_code=404, detail="Rascunho não encontrado")
        await self.drafts.delete(draft)

    async def generate_draft(self, user_id: int, *, plan_tier: PlanTier, report_id: int) -> dict:
        if self.ai_client is None:
            raise HTTPException(status_code=503, detail="Serviço de IA indisponível")
        report = await self._owned_report(user_id, report_id)
        assistant = await run_ai_request(
            self.ai_client,
            AIRequest(
                system_prompt=_DRAFT_SYSTEM_PROMPT,
                user_message=f"Relato: {report.title}. {report.description}",
                task_type=AITaskType.STRICT_JSON,
                plan_tier=plan_tier,
                user_id=user_id,
            ),
        )
        parsed = extract_json(assistant.text, context="Draft complaint")
        recipient = parsed.get("recipient") if isinstance(parsed, dict) else None
        body = parsed.get("body") if isinstance(parsed, dict) else None
        if not isinstance(body, str) or not body.strip():
            raise HTTPException(status_code=502, detail="Não foi possível gerar o rascunho")
        draft = await self.drafts.create(
            user_id,
            body=body.strip()[:20_000],
            recipient=(str(recipient)[:200] if recipient else None),
            report_id=report_id,
        )
        return self._draft_view(draft)
