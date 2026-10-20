"""
Service for the remote app configuration (the ``app_config`` table).

The set of configuration keys is a **closed registry**: each key has a type, a
default and (where useful) a validator.  An unknown key is never persisted, so
a typo cannot introduce a config the mobile app does not understand, and every
key always has a value (its default) even before an operator ever touches it.

The public payload served by ``GET /api/v1/app-config`` is:

.. code-block:: json

    {
      "maintenance": {"enabled": false, "message": "..."},
      "versions": {"min_supported": "1.0.0", "recommended": "1.0.0", "store_url": "..."},
      "features": {"medications": true, "finance": true, ...},
      "support": {"email": "suporte@novaiax.com.br", "whatsapp_url": null},
      "announcements": []
    }

It is cached in Redis for a short TTL and busted on every change, so the app
sees an edit within seconds without the backend re-reading the table on every
poll.
"""

from __future__ import annotations

import contextlib
import copy
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.repositories.app_config_repository import AppConfigRepository

#: Modules whose feature flag may be toggled remotely.  A disabled module is
#: hidden/soft-locked in the app without a new build.
MODULE_FLAGS: tuple[str, ...] = (
    "medications",
    "finance",
    "study",
    "logbook",
    "community",
    "device_actions",
    "voice",
)

#: Redis key holding the cached public payload.
PUBLIC_CACHE_KEY = "app_config:public"

_VERSION_RE = re.compile(r"^\d+(?:\.\d+){0,2}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class ConfigSpec:
    """Type + default (and optional validator) for one configuration key."""

    key: str
    default: Any
    validate: Callable[[Any], Any]


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "on", "yes", "sim"}
    return bool(value)


def _as_message(value: Any) -> str:
    return str(value or "").strip()[:500]


def _as_version(value: Any) -> str:
    text = str(value or "").strip()
    if not _VERSION_RE.match(text):
        raise ValueError(f"versão inválida: {text!r} (use algo como 1.2.3)")
    return text


def _as_store_url(value: Any) -> str:
    text = str(value or "").strip()
    if text and not text.startswith(("http://", "https://")):
        raise ValueError("o link da loja deve começar com http:// ou https://")
    return text[:500]


def _as_email(value: Any) -> str:
    text = str(value or "").strip()
    if not _EMAIL_RE.match(text):
        raise ValueError(f"e-mail de suporte inválido: {text!r}")
    return text[:254]


def _as_whatsapp_url(value: Any) -> str:
    text = str(value or "").strip()
    if text and not text.startswith("https://"):
        raise ValueError("o link do WhatsApp deve começar com https://")
    return text[:500]


def _as_feature_flags(value: Any) -> dict[str, bool]:
    value = value if isinstance(value, dict) else {}
    return {module: _as_bool(value.get(module, True)) for module in MODULE_FLAGS}


def _as_announcements(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    items: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        items.append(
            {
                "title": title[:120],
                "body": str(raw.get("body") or "").strip()[:1000],
                "level": str(raw.get("level") or "info").strip()[:20],
            }
        )
    return items[:20]


#: The closed registry of configuration keys.
SPECS: dict[str, ConfigSpec] = {
    "maintenance_mode": ConfigSpec("maintenance_mode", False, _as_bool),
    "maintenance_message": ConfigSpec(
        "maintenance_message",
        "Estamos em manutenção. Voltamos em instantes.",
        _as_message,
    ),
    "min_supported_app_version": ConfigSpec("min_supported_app_version", "1.0.0", _as_version),
    "recommended_app_version": ConfigSpec("recommended_app_version", "1.0.0", _as_version),
    "store_url": ConfigSpec("store_url", "", _as_store_url),
    "support_email": ConfigSpec("support_email", "suporte@novaiax.com.br", _as_email),
    "support_whatsapp_url": ConfigSpec("support_whatsapp_url", "", _as_whatsapp_url),
    "feature_flags": ConfigSpec("feature_flags", _as_feature_flags({}), _as_feature_flags),
    "announcements": ConfigSpec("announcements", [], _as_announcements),
}


def default_config() -> dict[str, Any]:
    """Return a fresh copy of every key's default value."""
    return {key: copy.deepcopy(spec.default) for key, spec in SPECS.items()}


class AppConfigService:
    """Read, validate and persist the remote app configuration."""

    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.repo = AppConfigRepository(session)

    async def load(self) -> dict[str, Any]:
        """Return the full configuration (defaults merged with stored rows)."""
        config = default_config()
        for row in await self.repo.get_all():
            spec = SPECS.get(row.key)
            if spec is None:
                continue
            try:
                config[row.key] = spec.validate(row.value)
            except ValueError:
                config[row.key] = config[row.key]
        return config

    def to_public(self, config: dict[str, Any]) -> dict[str, Any]:
        """Project the full configuration onto the public payload shape."""
        return {
            "maintenance": {
                "enabled": bool(config["maintenance_mode"]),
                "message": config["maintenance_message"],
            },
            "versions": {
                "min_supported": config["min_supported_app_version"],
                "recommended": config["recommended_app_version"],
                "store_url": config["store_url"],
            },
            "features": config["feature_flags"],
            "support": {
                "email": config["support_email"],
                "whatsapp_url": config["support_whatsapp_url"] or None,
            },
            "announcements": config["announcements"],
        }

    async def load_support(self) -> dict[str, str | None]:
        """Return the support contact used in user-facing error messages.

        The email is always present; ``whatsapp_url`` is ``None`` until an
        operator configures one.
        """
        config = await self.load()
        return {
            "email": str(config["support_email"]),
            "whatsapp_url": str(config["support_whatsapp_url"]) or None,
        }

    async def load_public(self, redis: Any | None) -> dict[str, Any]:
        """Return the public payload, using the Redis cache when available."""
        if redis is not None:
            cached = await redis.get(PUBLIC_CACHE_KEY)
            if cached:
                try:
                    return json.loads(cached)
                except (ValueError, TypeError):
                    pass
        payload = self.to_public(await self.load())
        if redis is not None:
            await redis.set(
                PUBLIC_CACHE_KEY,
                json.dumps(payload),
                ex=self.settings.app_config_cache_ttl_seconds,
            )
        return payload

    async def invalidate_cache(self, redis: Any | None) -> None:
        """Drop the cached public payload after a change."""
        if redis is not None:
            await redis.delete(PUBLIC_CACHE_KEY)

    async def update(self, values: dict[str, Any], *, admin_id: int | None) -> dict[str, Any]:
        """Validate and persist ``values``; returns the resulting config.

        Raises :class:`ValueError` when a value fails validation.
        """
        validated: dict[str, Any] = {}
        for key, raw in values.items():
            spec = SPECS.get(key)
            if spec is None:
                continue
            validated[key] = spec.validate(raw)
        for key, value in validated.items():
            await self.repo.upsert(key, value, updated_by=admin_id)
        await self.session.commit()
        return await self.load()


#: Static ``account_suspended`` detail, used when the support contact cannot
#: be read from the config.
SUSPENDED_DETAIL = "Conta suspensa. Entre em contato com o suporte."


async def suspended_detail(session: AsyncSession) -> str:
    """Compose the ``account_suspended`` message with the support contact.

    The contact comes from the ``support`` block of the remote app config, so
    an operator can change it from the admin panel without a deploy.  A
    config failure never breaks the rejection: :data:`SUSPENDED_DETAIL` is
    returned instead.
    """
    try:
        support = await AppConfigService(session).load_support()
    except Exception:  # noqa: BLE001 - the rejection must always be raised
        with contextlib.suppress(Exception):
            await session.rollback()
        return SUSPENDED_DETAIL

    email = str(support.get("email") or "").strip()
    whatsapp = str(support.get("whatsapp_url") or "").strip()
    if email and whatsapp:
        return (
            "Conta suspensa. Entre em contato com o suporte em "
            f"{email} ou pelo WhatsApp {whatsapp}."
        )
    if email:
        return f"Conta suspensa. Entre em contato com o suporte em {email}."
    if whatsapp:
        return f"Conta suspensa. Entre em contato com o suporte pelo WhatsApp {whatsapp}."
    return SUSPENDED_DETAIL


__all__ = [
    "AppConfigService",
    "ConfigSpec",
    "MODULE_FLAGS",
    "PUBLIC_CACHE_KEY",
    "SPECS",
    "SUSPENDED_DETAIL",
    "default_config",
    "suspended_detail",
]
