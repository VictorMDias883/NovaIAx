"""
Admin service for the official-contacts directory.

The ``official_contacts`` table backs the community/emergency "who to call"
feature.  This service owns the operator CRUD (create / edit / delete) and all
of its validation; the read path used by the app stays in
:class:`~app.services.community_service.CommunityService`.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.community import OfficialContact, OfficialContactCategory
from app.repositories.community_repository import OfficialContactRepository

#: Editable text fields (everything except the category enum).
_TEXT_FIELDS = ("name", "phone", "description", "scope", "region", "url", "source")


class OfficialContactService:
    """Create, list, edit and delete official contacts."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = OfficialContactRepository(session)

    async def list(
        self, *, category: OfficialContactCategory | None = None, region: str | None = None
    ) -> list[OfficialContact]:
        """Return contacts, optionally filtered by category and/or region."""
        return await self.repo.list_all(category=category, region=region)

    async def get(self, contact_id: int) -> OfficialContact | None:
        return await self.repo.get(contact_id)

    async def create(self, values: dict[str, Any]) -> OfficialContact:
        """Create a contact from validated ``values``."""
        return await self.repo.create(**values)

    async def update(self, contact: OfficialContact, values: dict[str, Any]) -> OfficialContact:
        """Apply validated ``values`` to ``contact``."""
        return await self.repo.update(contact, **values)

    async def delete(self, contact: OfficialContact) -> None:
        await self.repo.delete(contact)

    @staticmethod
    def parse(form: dict[str, str]) -> dict[str, Any]:
        """Translate and validate a submitted contact form.

        Raises :class:`fastapi.HTTPException` (400) on missing/invalid input.
        """
        name = (form.get("name") or "").strip()
        phone = (form.get("phone") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="O nome é obrigatório.")
        if not phone:
            raise HTTPException(status_code=400, detail="O telefone é obrigatório.")

        raw_category = (form.get("category") or "").strip()
        try:
            category = OfficialContactCategory(raw_category)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Categoria inválida.") from exc

        url = (form.get("url") or "").strip()
        if url and not url.startswith(("http://", "https://")):
            raise HTTPException(
                status_code=400, detail="O link deve começar com http:// ou https://"
            )

        values: dict[str, Any] = {"category": category, "name": name, "phone": phone}
        for field in _TEXT_FIELDS:
            if field in {"name", "phone"}:
                continue
            text = (form.get(field) or "").strip()
            values[field] = text or None
        return values
