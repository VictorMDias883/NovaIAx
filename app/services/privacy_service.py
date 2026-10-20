"""
Privacy & compliance services: consents, account export and account deletion.

This module owns the "rights" side of the product's data-stewardship story:

    - :class:`ConsentService` — read/grant/withdraw the consent log
      (``consent_records``).  Registration auto-records ``terms`` and
      ``privacy``; the other types are opt-in.
    - :class:`ExportRateLimiter` — per-user hourly ceiling for the data export
      endpoint, keyed by user id on top of the generic per-IP middleware bucket.
    - :class:`AccountExportService` — bundles every piece of user-owned data
      into a portable, pre-serialized JSON document.  Secrets are deliberately
      excluded (``password_hash``, ``purchase_token_hash``).
    - :class:`AccountDeletionService` — hard-deletes the account and every
      owned row in one explicit, ordered pass, then purges the user's Redis
      keys (conversations, quota counters, lockout/export keys).  Requires the
      account password as proof of consent.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import Enum
from typing import Any

from fastapi import HTTPException
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.cache.login_rate_limiter import KEY_PREFIX as LOGIN_LOCKOUT_PREFIX
from app.cache.redis_client import RedisClient
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.models.agent_action import AgentAction
from app.models.ai_feedback import AIFeedback
from app.models.community import (
    CommunityReport,
    DraftComplaint,
    ReportConfirmation,
    ReportFlag,
    UserBlock,
)
from app.models.consent import CONSENT_TYPES, ConsentRecord
from app.models.finance import Bill, Budget, Expense, Income
from app.models.health import Medication, MedicationIntake
from app.models.logbook import LogEntry
from app.models.objective import Objective
from app.models.productivity import (
    Checklist,
    ChecklistItem,
    FocusSession,
    Habit,
    HabitCheckIn,
    Note,
    Reminder,
    Task,
)
from app.models.purchase_event import PurchaseEvent
from app.models.roadmap_day import RoadmapDay
from app.models.study import Flashcard, FlashcardDeck, FlashcardReview, StudyPlan
from app.models.subscription import Subscription
from app.models.user import User
from app.models.user_agent import UserAgent
from app.models.user_memory import UserMemory
from app.repositories.user_repository import UserRepository
from app.services.cache_flush_service import flush_user_cache
from app.services.user_service import _password_matches
from app.utils.community_media import delete_community_photo

logger = get_logger(__name__)


def _to_primitive(value: Any) -> Any:
    """Recursively turn datetimes, dates and enums into JSON-safe scalars.

    Bundles are serialized here *before* FastAPI's encoder runs so the
    export is a plain, portable document that does not depend on request-time
    serialization details (and can never leak a raw ORM object).
    """
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _to_primitive(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_primitive(item) for item in value]
    return value


class ConsentService:
    """Read, grant and withdraw consent-log entries for a user."""

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    async def list(self, user_id: int) -> list[dict[str, str]]:
        """Return the caller's consent log, sorted by type."""
        stmt = (
            select(ConsentRecord)
            .where(ConsentRecord.user_id == user_id)
            .order_by(ConsentRecord.type)
        )
        result = await self.session.execute(stmt)
        return [
            {
                "type": record.type,
                "version": record.version,
                "accepted_at": record.accepted_at.isoformat(),
            }
            for record in result.scalars().all()
        ]

    async def record(
        self,
        user_id: int,
        consent_type: str,
        version: str | None = None,
        *,
        commit: bool = True,
        accepted_at: datetime | None = None,
    ) -> ConsentRecord:
        """Grant (or re-acknowledge) one consent type, keeping a single row.

        Re-accepting updates the stored ``version`` and ``accepted_at`` in
        place, so the log always reflects the user's *current* stance.
        """
        if consent_type not in CONSENT_TYPES:
            raise HTTPException(status_code=400, detail=f"Tipo de consentimento desconhecido: '{consent_type}'.")
        version = version or self.settings.consent_versions.get(consent_type, "")
        accepted_at = accepted_at or datetime.now(UTC)

        stmt = select(ConsentRecord).where(
            ConsentRecord.user_id == user_id, ConsentRecord.type == consent_type
        )
        existing = (await self.session.execute(stmt)).scalar_one_or_none()
        if existing is None:
            record = ConsentRecord(user_id=user_id, type=consent_type, version=version, accepted_at=accepted_at)
            self.session.add(record)
        else:
            existing.version = version
            existing.accepted_at = accepted_at
            record = existing
        if commit:
            await self.session.commit()
        else:
            await self.session.flush()
        return record

    async def withdraw(self, user_id: int, consent_type: str, *, commit: bool = True) -> bool:
        """Remove a consent; returns ``True`` when a row was actually deleted.

        Withdrawing deletes the row, which is indistinguishable from "never
        accepted" — the intended semantics for a product that collects no
        data without an explicit, retractable consent.
        """
        if consent_type not in CONSENT_TYPES:
            raise HTTPException(status_code=400, detail=f"Tipo de consentimento desconhecido: '{consent_type}'.")
        result = await self.session.execute(
            delete(ConsentRecord).where(
                ConsentRecord.user_id == user_id, ConsentRecord.type == consent_type
            )
        )
        deleted = getattr(result, "rowcount", 0) > 0
        if commit:
            await self.session.commit()
        return deleted


class ExportRateLimiter:
    """Per-user hourly ceiling for the account-export endpoint.

    Export bundles the user's entire account, so it is throttled per *user*
    (belt) on top of the per-*IP* middleware bucket (suspenders).
    """

    KEY_PREFIX = "export_limit:"

    def __init__(self, client: RedisClient | None = None, settings: Settings | None = None) -> None:
        self.client = client or RedisClient()
        self.settings = settings or get_settings()

    def _key(self, user_id: int) -> str:
        return f"{self.KEY_PREFIX}{user_id}"

    async def allow(self, user_id: int) -> bool:
        """Return whether the user still has export budget left this hour."""
        ceiling = self.settings.data_export_max_per_hour
        if ceiling <= 0:
            return True
        raw = await self.client.get(self._key(user_id))
        if raw is None:
            return True
        try:
            used = int(raw)
        except (TypeError, ValueError):
            return True
        return used < ceiling

    async def record(self, user_id: int) -> None:
        """Count one export; the counter window starts on the first."""
        key = self._key(user_id)
        await self.client.incrby(key, 1)
        if await self.client.ttl(key) == -1:
            await self.client.expire(key, self.settings.data_export_window_seconds)


class AccountExportService:
    """Assemble a portable JSON bundle of one user's data."""

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()

    async def build_bundle(self, user_id: int) -> dict[str, Any]:
        """Return the user's complete, pre-serialized data bundle.

        Raised :class:`HTTPException` (404) when the account no longer exists.
        Secrets (``password_hash``, ``purchase_token_hash``) are never
        included.
        """
        user = await self.session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="Usuário não encontrado.")

        objectives_stmt = (
            select(Objective)
            .where(Objective.user_id == user_id)
            .options(selectinload(Objective.days))
            .order_by(Objective.id)
        )
        objectives = (await self.session.execute(objectives_stmt)).scalars().all()

        sub_stmt = select(Subscription).where(Subscription.user_id == user_id)
        subscription = (await self.session.execute(sub_stmt)).scalar_one_or_none()

        sub_ids = select(Subscription.id).where(Subscription.user_id == user_id)
        purchase_events_stmt = select(PurchaseEvent).where(
            or_(PurchaseEvent.user_id == user_id, PurchaseEvent.subscription_id.in_(sub_ids))
        )
        purchase_events = (await self.session.execute(purchase_events_stmt)).scalars().all()

        # Personalization profile is optional.
        user_agent_stmt = select(UserAgent).where(UserAgent.user_id == user_id)
        user_agent = (await self.session.execute(user_agent_stmt)).scalar_one_or_none()

        memories = (await self.session.execute(
            select(UserMemory).where(UserMemory.user_id == user_id).order_by(UserMemory.key)
        )).scalars().all()
        agent_actions = (await self.session.execute(
            select(AgentAction).where(AgentAction.user_id == user_id).order_by(AgentAction.created_at)
        )).scalars().all()
        feedback_rows = (await self.session.execute(
            select(AIFeedback).where(AIFeedback.user_id == user_id).order_by(AIFeedback.created_at)
        )).scalars().all()

        productivity = await self._productivity_bundle(user_id)
        health = await self._health_bundle(user_id)
        finance = await self._finance_bundle(user_id)
        study = await self._study_bundle(user_id)
        logbook = await self._logbook_bundle(user_id)
        community = await self._community_bundle(user_id)

        return _to_primitive(
            {
                "schema_version": 1,
                "generated_at": datetime.now(UTC),
                "user": {
                    "id": user.id,
                    "full_name": user.full_name,
                    "email": user.email,
                    "role": user.role,
                    "birth_date": user.birth_date,
                    "created_at": user.created_at,
                },
                "consents": await ConsentService(self.session, self.settings).list(user_id),
                "objectives": [
                    {
                        "id": objective.id,
                        "title": objective.title,
                        "description": objective.description,
                        "due_date": objective.due_date,
                        "roadmap": objective.roadmap,
                        "roadmap_updated_at": objective.roadmap_updated_at,
                        "created_at": objective.created_at,
                        "days": [
                            {
                                "day_number": day.day_number,
                                "day_date": day.day_date,
                                "status": day.status,
                                "content": day.content,
                                "completed_at": day.completed_at,
                            }
                            for day in objective.days
                        ],
                    }
                    for objective in objectives
                ],
                "user_agent": None
                if user_agent is None
                else {
                    "agent_name": user_agent.agent_name,
                    "tone": user_agent.tone,
                    "language": user_agent.language,
                    "timezone": user_agent.timezone,
                    "active_group_slugs": user_agent.active_group_slugs,
                    "active_sub_agent_slugs": user_agent.active_sub_agent_slugs,
                    "simplified_mode": user_agent.simplified_mode,
                    "learning_enabled": user_agent.learning_enabled,
                    "created_at": user_agent.created_at,
                },
                "memories": [
                    {
                        "key": memory.key,
                        "value": memory.value,
                        "source": memory.source,
                        "enabled": memory.enabled,
                        "created_at": memory.created_at,
                    }
                    for memory in memories
                ],
                "agent_actions": [
                    {
                        "message_id": action.message_id,
                        "type": action.type,
                        "status": action.status,
                        "params": action.params,
                        "summary": action.summary,
                        "requires_confirmation": action.requires_confirmation,
                        "is_device": action.is_device,
                        "prepares_only": action.prepares_only,
                        "result": action.result,
                        "error": action.error,
                        "created_at": action.created_at,
                    }
                    for action in agent_actions
                ],
                "ai_feedback": [
                    {
                        "message_id": row.message_id,
                        "reason": row.reason,
                        "details": row.details,
                        "created_at": row.created_at,
                    }
                    for row in feedback_rows
                ],
                "subscription": None
                if subscription is None
                else {
                    "plan": subscription.plan,
                    "status": subscription.status,
                    "product_id": subscription.product_id,
                    "expires_at": subscription.expires_at,
                    "auto_renewing": subscription.auto_renewing,
                    "is_trial": subscription.is_trial,
                    "created_at": subscription.created_at,
                },
                "purchase_events": [
                    {
                        "event_type": event.event_type,
                        "notification_type": event.notification_type,
                        "product_id": event.product_id,
                        "payload": event.payload,
                        "created_at": event.created_at,
                    }
                    for event in purchase_events
                ],
                "productivity": productivity,
                "health": health,
                "finance": finance,
                "study": study,
                "logbook": logbook,
                "community": community,
                "third_party_processors": [
                    {
                        "name": provider.name,
                        "base_url": provider.base_url,
                        "purpose": "AI assistant provider — conversation text is sent to complete your requests.",
                    }
                    for provider in self.settings.ai_providers
                ],
            }
        )

    async def _productivity_bundle(self, user_id: int) -> dict[str, Any]:
        """Assemble the owner-scoped productivity rows, chronologically each."""
        tasks = (await self.session.execute(
            select(Task).where(Task.user_id == user_id).order_by(Task.created_at)
        )).scalars().all()
        reminders = (await self.session.execute(
            select(Reminder).where(Reminder.user_id == user_id).order_by(Reminder.created_at)
        )).scalars().all()
        notes = (await self.session.execute(
            select(Note).where(Note.user_id == user_id).order_by(Note.created_at)
        )).scalars().all()
        checklists = (await self.session.execute(
            select(Checklist)
            .where(Checklist.user_id == user_id)
            .options(selectinload(Checklist.items))
            .order_by(Checklist.created_at)
        )).scalars().all()
        habits = (await self.session.execute(
            select(Habit)
            .where(Habit.user_id == user_id)
            .options(selectinload(Habit.check_ins))
            .order_by(Habit.created_at)
        )).scalars().all()
        focus_sessions = (await self.session.execute(
            select(FocusSession).where(FocusSession.user_id == user_id).order_by(FocusSession.start_at)
        )).scalars().all()

        return {
            "tasks": [
                {
                    "id": task.id,
                    "title": task.title,
                    "notes": task.notes,
                    "due_at": task.due_at,
                    "priority": task.priority,
                    "status": task.status,
                    "recurrence": task.recurrence,
                    "created_at": task.created_at,
                }
                for task in tasks
            ],
            "reminders": [
                {
                    "id": reminder.id,
                    "title": reminder.title,
                    "body": reminder.body,
                    "remind_at": reminder.remind_at,
                    "recurrence": reminder.recurrence,
                    "status": reminder.status,
                    "snoozed_until": reminder.snoozed_until,
                    "created_at": reminder.created_at,
                }
                for reminder in reminders
            ],
            "notes": [
                {
                    "id": note.id,
                    "title": note.title,
                    "content": note.content,
                    "tags": note.tags,
                    "pinned": note.pinned,
                    "created_at": note.created_at,
                }
                for note in notes
            ],
            "checklists": [
                {
                    "id": checklist.id,
                    "title": checklist.title,
                    "category": checklist.category,
                    "created_at": checklist.created_at,
                    "items": [
                        {
                            "text": item.text,
                            "done": item.done,
                            "position": item.position,
                            "created_at": item.created_at,
                        }
                        for item in checklist.items
                    ],
                }
                for checklist in checklists
            ],
            "habits": [
                {
                    "id": habit.id,
                    "title": habit.title,
                    "created_at": habit.created_at,
                    "check_ins": [
                        {
                            "check_date": check_in.check_date,
                            "created_at": check_in.created_at,
                        }
                        for check_in in habit.check_ins
                    ],
                }
                for habit in habits
            ],
            "focus_sessions": [
                {
                    "id": session.id,
                    "label": session.label,
                    "start_at": session.start_at,
                    "end_at": session.end_at,
                    "created_at": session.created_at,
                }
                for session in focus_sessions
            ],
        }

    async def _health_bundle(self, user_id: int) -> dict[str, Any]:
        """Medications and their intake log."""
        medications = (await self.session.execute(
            select(Medication).where(Medication.user_id == user_id).order_by(Medication.id)
        )).scalars().all()
        intakes = (await self.session.execute(
            select(MedicationIntake)
            .where(MedicationIntake.user_id == user_id)
            .order_by(MedicationIntake.id)
        )).scalars().all()

        return {
            "medications": [
                {
                    "id": medication.id,
                    "name": medication.name,
                    "dosage": medication.dosage,
                    "frequency": medication.frequency,
                    "schedule_times": medication.schedule_times,
                    "start_date": medication.start_date,
                    "end_date": medication.end_date,
                    "instructions": medication.instructions,
                    "active": medication.active,
                    "created_at": medication.created_at,
                    "updated_at": medication.updated_at,
                }
                for medication in medications
            ],
            "intakes": [
                {
                    "id": intake.id,
                    "medication_id": intake.medication_id,
                    "scheduled_at": intake.scheduled_at,
                    "taken_at": intake.taken_at,
                    "status": intake.status,
                    "notes": intake.notes,
                    "created_at": intake.created_at,
                }
                for intake in intakes
            ],
        }

    async def _finance_bundle(self, user_id: int) -> dict[str, Any]:
        """Expenses, incomes, bills and monthly budgets."""
        expenses = (await self.session.execute(
            select(Expense).where(Expense.user_id == user_id).order_by(Expense.id)
        )).scalars().all()
        incomes = (await self.session.execute(
            select(Income).where(Income.user_id == user_id).order_by(Income.id)
        )).scalars().all()
        bills = (await self.session.execute(
            select(Bill).where(Bill.user_id == user_id).order_by(Bill.id)
        )).scalars().all()
        budgets = (await self.session.execute(
            select(Budget).where(Budget.user_id == user_id).order_by(Budget.id)
        )).scalars().all()

        return {
            "expenses": [
                {
                    "id": expense.id,
                    "amount_cents": expense.amount_cents,
                    "category": expense.category,
                    "description": expense.description,
                    "occurred_at": expense.occurred_at,
                    "created_at": expense.created_at,
                }
                for expense in expenses
            ],
            "incomes": [
                {
                    "id": income.id,
                    "amount_cents": income.amount_cents,
                    "source": income.source,
                    "description": income.description,
                    "occurred_at": income.occurred_at,
                    "created_at": income.created_at,
                }
                for income in incomes
            ],
            "bills": [
                {
                    "id": bill.id,
                    "name": bill.name,
                    "amount_cents": bill.amount_cents,
                    "due_date": bill.due_date,
                    "status": bill.status,
                    "recurrence": bill.recurrence,
                    "paid_at": bill.paid_at,
                    "created_at": bill.created_at,
                    "updated_at": bill.updated_at,
                }
                for bill in bills
            ],
            "budgets": [
                {
                    "id": budget.id,
                    "month": budget.month,
                    "category": budget.category,
                    "amount_cents": budget.amount_cents,
                    "created_at": budget.created_at,
                    "updated_at": budget.updated_at,
                }
                for budget in budgets
            ],
        }

    async def _study_bundle(self, user_id: int) -> dict[str, Any]:
        """Study plans, flashcard decks (with cards) and the review log."""
        plans = (await self.session.execute(
            select(StudyPlan).where(StudyPlan.user_id == user_id).order_by(StudyPlan.id)
        )).scalars().all()
        decks = (await self.session.execute(
            select(FlashcardDeck)
            .where(FlashcardDeck.user_id == user_id)
            .options(selectinload(FlashcardDeck.cards))
            .order_by(FlashcardDeck.id)
        )).scalars().all()
        reviews = (await self.session.execute(
            select(FlashcardReview).where(FlashcardReview.user_id == user_id).order_by(FlashcardReview.id)
        )).scalars().all()

        return {
            "plans": [
                {
                    "id": plan.id,
                    "subject": plan.subject,
                    "focus": plan.focus,
                    "status": plan.status,
                    "target_date": plan.target_date,
                    "created_at": plan.created_at,
                    "updated_at": plan.updated_at,
                }
                for plan in plans
            ],
            "decks": [
                {
                    "id": deck.id,
                    "name": deck.name,
                    "description": deck.description,
                    "created_at": deck.created_at,
                    "updated_at": deck.updated_at,
                    "cards": [
                        {
                            "id": card.id,
                            "front": card.front,
                            "back": card.back,
                            "ease": card.ease,
                            "interval_days": card.interval_days,
                            "repetitions": card.repetitions,
                            "due_at": card.due_at,
                            "last_reviewed_at": card.last_reviewed_at,
                            "created_at": card.created_at,
                            "updated_at": card.updated_at,
                        }
                        for card in deck.cards
                    ],
                }
                for deck in decks
            ],
            "reviews": [
                {
                    "id": review.id,
                    "flashcard_id": review.flashcard_id,
                    "grade": review.grade,
                    "reviewed_at": review.reviewed_at,
                }
                for review in reviews
            ],
        }

    async def _logbook_bundle(self, user_id: int) -> dict[str, Any]:
        """Trips, shifts and child-care log entries."""
        entries = (await self.session.execute(
            select(LogEntry).where(LogEntry.user_id == user_id).order_by(LogEntry.id)
        )).scalars().all()
        return {
            "entries": [
                {
                    "id": entry.id,
                    "kind": entry.kind,
                    "title": entry.title,
                    "category": entry.category,
                    "started_at": entry.started_at,
                    "ended_at": entry.ended_at,
                    "duration_minutes": entry.duration_minutes,
                    "notes": entry.notes,
                    "metadata": entry.metadata_,
                    "created_at": entry.created_at,
                    "updated_at": entry.updated_at,
                }
                for entry in entries
            ],
        }

    async def _community_bundle(self, user_id: int) -> dict[str, Any]:
        """Reports authored by the user plus their social interactions."""
        reports = (await self.session.execute(
            select(CommunityReport)
            .where(CommunityReport.user_id == user_id)
            .order_by(CommunityReport.id)
        )).scalars().all()
        confirmations = (await self.session.execute(
            select(ReportConfirmation)
            .where(ReportConfirmation.user_id == user_id)
            .order_by(ReportConfirmation.id)
        )).scalars().all()
        flags = (await self.session.execute(
            select(ReportFlag).where(ReportFlag.user_id == user_id).order_by(ReportFlag.id)
        )).scalars().all()
        blocks = (await self.session.execute(
            select(UserBlock).where(UserBlock.blocker_id == user_id).order_by(UserBlock.id)
        )).scalars().all()
        drafts = (await self.session.execute(
            select(DraftComplaint).where(DraftComplaint.user_id == user_id).order_by(DraftComplaint.id)
        )).scalars().all()

        return {
            "reports": [
                {
                    "id": report.id,
                    "category": report.category,
                    "title": report.title,
                    "description": report.description,
                    "location": report.location,
                    "occurred_at": report.occurred_at,
                    "status": report.status,
                    "is_anonymous": report.is_anonymous,
                    "photo_path": report.photo_path,
                    "created_at": report.created_at,
                    "updated_at": report.updated_at,
                }
                for report in reports
            ],
            "confirmations": [
                {
                    "id": confirmation.id,
                    "report_id": confirmation.report_id,
                    "created_at": confirmation.created_at,
                }
                for confirmation in confirmations
            ],
            "flags": [
                {
                    "id": flag.id,
                    "report_id": flag.report_id,
                    "reason": flag.reason,
                    "created_at": flag.created_at,
                }
                for flag in flags
            ],
            "blocks": [
                {
                    "id": block.id,
                    "blocked_id": block.blocked_id,
                    "created_at": block.created_at,
                }
                for block in blocks
            ],
            "drafts": [
                {
                    "id": draft.id,
                    "report_id": draft.report_id,
                    "recipient": draft.recipient,
                    "body": draft.body,
                    "created_at": draft.created_at,
                    "updated_at": draft.updated_at,
                }
                for draft in drafts
            ],
        }


class AccountDeletionService:
    """Hard-delete an account and every byte of data it owns."""

    def __init__(self, session: AsyncSession, redis_client: RedisClient | None = None, settings: Settings | None = None) -> None:
        self.session = session
        self.redis_client = redis_client or RedisClient()
        self.settings = settings or get_settings()

    async def delete_account(self, user_id: int, password: str) -> dict[str, Any]:
        """Verify ``password`` then delete the account and all owned data.

        Runs inside a single database transaction so a failure mid-way leaves
        the account intact (no partial wipe).  Redis keys belonging to the
        user are purged afterwards; the caveat is that a conversation cached
        moments before deletion may survive the operation until the next
        retention sweep, but the account itself is gone instantly.
        """
        async with self.session.begin():
            user = await UserRepository(self.session).get_by_id(user_id, for_update=True)
            if user is None:
                raise HTTPException(status_code=404, detail="Usuário não encontrado.")
            if not _password_matches(password, user.password_hash):
                raise HTTPException(status_code=400, detail="A senha atual está incorreta.")

            email, photo_names, subscription = await self._purge_locked(user)

        return await self._finalize_purge(user_id, email, photo_names, subscription)

    async def purge_user(self, user_id: int) -> dict[str, Any]:
        """Delete an account and all owned data **without** a password.

        This is the operator's one-click execution from the admin console's
        deletion-request queue: identity was already established out-of-band,
        so the password check cannot apply.  Every other step is the exact
        same purge :meth:`delete_account` runs.
        """
        async with self.session.begin():
            user = await UserRepository(self.session).get_by_id(user_id, for_update=True)
            if user is None:
                raise HTTPException(status_code=404, detail="Usuário não encontrado.")

            email, photo_names, subscription = await self._purge_locked(user)

        return await self._finalize_purge(user_id, email, photo_names, subscription)

    async def _purge_locked(
        self, user: User
    ) -> tuple[str, list[str], Subscription | None]:
        """Delete every owned row and the user itself (inside a transaction).

        Returns the e-mail, the community photo files to remove afterwards and
        the subscription row (read before deletion) for the purge's return
        value.  The caller owns the surrounding ``session.begin()``.
        """
        user_id = int(user.id)
        email = str(user.email)
        sub_stmt = select(Subscription).where(Subscription.user_id == user_id)
        subscription = (await self.session.execute(sub_stmt)).scalar_one_or_none()

        photo_names = (await self.session.execute(
            select(CommunityReport.photo_path).where(
                CommunityReport.user_id == user_id,
                CommunityReport.photo_path.is_not(None),
            )
        )).scalars().all()

        await self._purge_database_rows(user_id)
        await self.session.delete(user)
        return email, [name for name in photo_names if name is not None], subscription

    async def _finalize_purge(
        self,
        user_id: int,
        email: str,
        photo_names: list[str],
        subscription: Subscription | None,
    ) -> dict[str, Any]:
        """Flush Redis and photo files, then build the result payload."""
        await self._purge_redis_keys(user_id, email)

        for photo_name in photo_names:
            delete_community_photo(photo_name, self.settings.community_media_dir)

        management_url = None
        if subscription is not None:
            package_name = self.settings.google_play_package_name
            management_url = (
                f"https://play.google.com/store/account/subscriptions"
                f"?sku={subscription.product_id}&package={package_name}"
            )

        return {
            "message": "Account deleted.",
            "subscription_management_url": management_url,
        }

    async def _purge_database_rows(self, user_id: int) -> None:
        """Delete every user-owned row in dependency order.

        The order mirrors the FK graph: children before parents, and
        subscription-related rows before the subscription itself, so no
        ``ON DELETE SET NULL``/FK pass is ever forced to fight a constraint.
        """
        s = self.session
        await s.execute(delete(ConsentRecord).where(ConsentRecord.user_id == user_id))

        objective_ids = select(Objective.id).where(Objective.user_id == user_id)

        # roadmap_days → objectives
        await s.execute(delete(RoadmapDay).where(RoadmapDay.objective_id.in_(objective_ids)))
        await s.execute(delete(Objective).where(Objective.user_id == user_id))

        # checklist_items → checklists ; habit_check_ins → habits
        checklist_ids = select(Checklist.id).where(Checklist.user_id == user_id)
        await s.execute(delete(ChecklistItem).where(ChecklistItem.checklist_id.in_(checklist_ids)))
        habit_ids = select(Habit.id).where(Habit.user_id == user_id)
        await s.execute(delete(HabitCheckIn).where(HabitCheckIn.habit_id.in_(habit_ids)))

        await s.execute(delete(Task).where(Task.user_id == user_id))
        await s.execute(delete(Reminder).where(Reminder.user_id == user_id))
        await s.execute(delete(Note).where(Note.user_id == user_id))
        await s.execute(delete(Checklist).where(Checklist.user_id == user_id))
        await s.execute(delete(Habit).where(Habit.user_id == user_id))
        await s.execute(delete(FocusSession).where(FocusSession.user_id == user_id))

        # medication_intakes → medications
        medication_ids = select(Medication.id).where(Medication.user_id == user_id)
        await s.execute(
            delete(MedicationIntake).where(
                or_(
                    MedicationIntake.user_id == user_id,
                    MedicationIntake.medication_id.in_(medication_ids),
                )
            )
        )
        await s.execute(delete(Medication).where(Medication.user_id == user_id))

        # finance
        await s.execute(delete(Expense).where(Expense.user_id == user_id))
        await s.execute(delete(Income).where(Income.user_id == user_id))
        await s.execute(delete(Bill).where(Bill.user_id == user_id))
        await s.execute(delete(Budget).where(Budget.user_id == user_id))

        # study: flashcard_reviews → flashcards → decks ; study_plans
        card_ids = select(Flashcard.id).where(Flashcard.user_id == user_id)
        deck_ids = select(FlashcardDeck.id).where(FlashcardDeck.user_id == user_id)
        await s.execute(
            delete(FlashcardReview).where(
                or_(
                    FlashcardReview.user_id == user_id,
                    FlashcardReview.flashcard_id.in_(card_ids),
                )
            )
        )
        await s.execute(
            delete(Flashcard).where(
                or_(Flashcard.user_id == user_id, Flashcard.deck_id.in_(deck_ids))
            )
        )
        await s.execute(delete(FlashcardDeck).where(FlashcardDeck.user_id == user_id))
        await s.execute(delete(StudyPlan).where(StudyPlan.user_id == user_id))

        # logbook
        await s.execute(delete(LogEntry).where(LogEntry.user_id == user_id))

        # community: drafts, then interactions (own + on own reports), then reports.
        # Drafts referencing the user's reports are nulled by the SET NULL FK.
        # Confirmations/flags by *other* users on the user's reports are removed
        # too so no dangling social row survives the report.
        report_ids = select(CommunityReport.id).where(CommunityReport.user_id == user_id)
        await s.execute(delete(DraftComplaint).where(DraftComplaint.user_id == user_id))
        await s.execute(
            delete(ReportConfirmation).where(
                or_(
                    ReportConfirmation.user_id == user_id,
                    ReportConfirmation.report_id.in_(report_ids),
                )
            )
        )
        await s.execute(
            delete(ReportFlag).where(
                or_(ReportFlag.user_id == user_id, ReportFlag.report_id.in_(report_ids))
            )
        )
        await s.execute(
            delete(UserBlock).where(
                or_(UserBlock.blocker_id == user_id, UserBlock.blocked_id == user_id)
            )
        )
        await s.execute(delete(CommunityReport).where(CommunityReport.user_id == user_id))

        await s.execute(delete(UserMemory).where(UserMemory.user_id == user_id))
        await s.execute(delete(UserAgent).where(UserAgent.user_id == user_id))
        await s.execute(delete(AIFeedback).where(AIFeedback.user_id == user_id))
        await s.execute(delete(AgentAction).where(AgentAction.user_id == user_id))

        subscription_ids = select(Subscription.id).where(Subscription.user_id == user_id)
        await s.execute(
            delete(PurchaseEvent).where(
                or_(PurchaseEvent.user_id == user_id, PurchaseEvent.subscription_id.in_(subscription_ids))
            )
        )
        await s.execute(delete(Subscription).where(Subscription.user_id == user_id))

    async def _purge_redis_keys(self, user_id: int, email: str) -> dict[str, int]:
        """Remove Redis keys owned by the user (conversations, quotas, locks)."""
        quota_keys = await self.redis_client.scan_keys(f"ai_quota:*:{user_id}:*")
        await self.redis_client.delete_many([*quota_keys, f"{LOGIN_LOCKOUT_PREFIX}{email.strip().lower()}", f"{ExportRateLimiter.KEY_PREFIX}{user_id}"])
        cleared = await flush_user_cache(self.redis_client, user_id, include_conversations=True)
        return {**cleared, "quota_keys_cleared": len(quota_keys)}
