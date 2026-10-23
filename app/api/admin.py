"""
Server-rendered operator console for the NovaIAx gateway.

This router is a thin **presentation layer** over the service layer.  It owns
no business rules: it authenticates the panel session, validates CSRF,
records an audit entry for every mutation, and renders templates.

Authentication is deliberately separate from the mobile API:

* an opaque, httpOnly, SameSite=Strict cookie whose SHA-256 hash is stored in
  ``admin_sessions`` (idle + absolute timeouts, server-side revocation);
* only accounts whose **live** database role is ``ADMIN`` may log in — admins
  are provisioned directly in the database (``scripts/make_admin_sql.py``),
  never through the panel;
* an optional TOTP second factor and source-IP allowlist.

Routes:
    GET  /admin/login                             — login form
    POST /admin/login                             — authenticate, start session
    POST /admin/logout                            — revoke session
    GET  /admin/                                  — dashboard (aggregates)
    GET  /admin/users                             — list users
    POST /admin/users/{id}/promote|demote|delete  — modify a user
    GET  /admin/system-prompts                    — list system prompts
    POST /admin/system-prompts                    — create a system prompt
    GET  /admin/system-prompts/{id}/edit          — edit form
    POST /admin/system-prompts/{id}               — update a system prompt
    POST /admin/system-prompts/{id}/delete        — delete a system prompt
    GET  /admin/reports                           — moderation queue
    POST /admin/reports/{id}/status               — set a report's status
    GET  /admin/audit                             — audit trail viewer
    GET/POST /admin/security/totp                 — TOTP enrolment
    POST /admin/security/totp/disable             — disable TOTP

.. note::
    The panel has its own authentication scheme. The route-level dependencies
    below are its first authorization gate and run on every handler.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_ai_router, get_redis_client, get_session
from app.commands.delete_user_command import DeleteUserCommand
from app.commands.demote_user_command import DemoteUserCommand
from app.commands.grant_plan_command import GrantPlanCommand
from app.commands.promote_user_command import PromoteUserCommand
from app.core.config import Settings, get_settings
from app.core.network import get_client_ip
from app.models.ai_feedback import AI_FEEDBACK_STATUS_PENDING
from app.models.community import CommunityReport, OfficialContactCategory, ReportStatus
from app.models.deletion_request import DELETION_STATUS_PENDING
from app.models.promo_code import GRANTABLE_PLANS, PromoCodeKind
from app.repositories.agent_catalog_repository import AgentCatalogRepository
from app.repositories.ai_feedback_repository import AIFeedbackRepository
from app.repositories.community_repository import CommunityReportRepository
from app.repositories.deletion_request_repository import DeletionRequestRepository
from app.repositories.user_repository import UserRepository
from app.schemas.system_prompt_schemas import SystemPromptRequest
from app.services.admin_audit_service import AdminAuditService
from app.services.admin_auth_service import AdminAuthService
from app.services.admin_dashboard_service import AdminDashboardService
from app.services.agent_admin_service import AgentAdminService
from app.services.agent_preview_service import AgentPreviewService
from app.services.ai_ops_service import AIOpsService
from app.services.app_config_service import MODULE_FLAGS, AppConfigService
from app.services.community_service import CommunityService
from app.services.official_contact_service import OfficialContactService
from app.services.plan_limit_override_service import (
    EDITABLE_FIELDS,
    PANEL_PLANS,
    PlanLimitOverrideService,
)
from app.services.promo_admin_service import PromoAdminService
from app.services.system_prompt_service import SystemPromptService
from app.services.user_service import UserService
from app.utils.totp import generate_secret, provisioning_uri
from app.utils.totp import verify as verify_totp

_LOGIN_URL = "/admin/login"
_TOTP_URL = "/admin/security/totp"

#: Pre-authentication CSRF cookie, used only to protect the login form itself.
_LOGIN_CSRF_COOKIE = "novaiax_admin_login_csrf"
_COOKIE_PATH = "/admin"


# ---------------------------------------------------------------------------
# Panel gates (enabled flag + source-IP allowlist)
# ---------------------------------------------------------------------------


def _settings() -> Settings:
    return get_settings()


async def enforce_panel_enabled(request: Request) -> None:
    """Answer 404 for every admin route when the panel is disabled."""
    if not _settings().admin_panel_enabled:
        raise HTTPException(status_code=404, detail="Not Found")


async def enforce_panel_ip(request: Request) -> None:
    """Reject requests from source IPs outside the optional allowlist."""
    allowed = _settings().admin_allowed_ips
    if not allowed:
        return
    if get_client_ip(request) not in allowed:
        raise HTTPException(status_code=403, detail="Acesso restrito.")


router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(enforce_panel_enabled), Depends(enforce_panel_ip)],
)

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=_TEMPLATES_DIR)


# ---------------------------------------------------------------------------
# Redirect signalling
# ---------------------------------------------------------------------------


class AdminRedirect(Exception):
    """Internal signal that the browser must be redirected (303)."""

    def __init__(self, location: str) -> None:
        super().__init__(location)
        self.location = location
        self.headers = {"Location": location}


class AdminLoginRedirect(AdminRedirect):
    """Redirect to the login page; kept as its own type for compatibility."""

    def __init__(self) -> None:
        super().__init__(_LOGIN_URL)


def register_admin_exception_handler(app: FastAPI) -> None:
    """Convert :class:`AdminRedirect` into a real 303 redirect.

    Starlette resolves handlers by ``type(exc).__mro__``, so this narrower
    handler wins over the generic ``Exception`` catch-all.
    """

    @app.exception_handler(AdminRedirect)
    async def _admin_redirect_handler(request: Request, exc: AdminRedirect) -> RedirectResponse:
        return RedirectResponse(exc.location, status_code=303)


def _redirect_login() -> NoReturn:
    raise AdminLoginRedirect()


# ---------------------------------------------------------------------------
# Rendering / CSRF helpers
# ---------------------------------------------------------------------------


def _panel_cookie() -> str:
    return _settings().admin_panel_cookie_name


def _render(request: Request, name: str, context: dict[str, Any], *, status_code: int = 200) -> Response:
    """Render a admin template with the CSP nonce and settings in scope."""
    nonce = getattr(request.state, "csp_nonce", "")
    admin = context.get("admin") or {}
    pending = admin.get("pending") or {}
    merged = {"csp_nonce": nonce, "settings": _settings(), "pending": pending, **context}
    return templates.TemplateResponse(request=request, name=name, context=merged, status_code=status_code)


async def _moderation_counts(session: AsyncSession) -> dict[str, int]:
    """Return the pending-item counters shown as sidebar badges.

    Everything is a single aggregate query — the panel never loads content to
    render a badge.
    """
    pending_reports = int(
        (
            await session.execute(
                select(func.count())
                .select_from(CommunityReport)
                .where(
                    or_(
                        CommunityReport.hidden_at.is_not(None),
                        CommunityReport.status == ReportStatus.FLAGGED,
                    )
                )
            )
        ).scalar_one()
    )
    pending_feedback = await AIFeedbackRepository(session).count_by_status(
        AI_FEEDBACK_STATUS_PENDING
    )
    pending_deletions = await DeletionRequestRepository(session).count_by_status(
        DELETION_STATUS_PENDING
    )
    return {
        "reports": pending_reports,
        "feedback": pending_feedback,
        "deletions": pending_deletions,
    }


def _validate_login_csrf(request: Request, csrf_token: str | None) -> None:
    """Reject a login submission that lacks the pre-auth CSRF token."""
    expected = request.cookies.get(_LOGIN_CSRF_COOKIE)
    if not expected or not csrf_token or not secrets.compare_digest(csrf_token, expected):
        raise HTTPException(status_code=403, detail="Sessão de login inválida. Recarregue a página.")


def _validate_csrf(admin: dict[str, Any], csrf_token: str | None) -> None:
    """Reject a state-changing request whose token does not match the session."""
    expected = str(admin.get("csrf_token") or "")
    if not expected or not csrf_token or not secrets.compare_digest(csrf_token, expected):
        raise HTTPException(status_code=403, detail="Token CSRF inválido.")


async def _audit(
    session: AsyncSession,
    request: Request,
    admin: dict[str, Any],
    action: str,
    *,
    entity: str | None = None,
    entity_id: int | str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    """Record one admin action in the audit trail."""
    await AdminAuditService(session).record(
        admin=admin,
        action=action,
        entity=entity,
        entity_id=entity_id,
        before=before,
        after=after,
        ip_address=get_client_ip(request),
    )


# ---------------------------------------------------------------------------
# Authentication dependencies
# ---------------------------------------------------------------------------


async def _resolve_admin(
    request: Request,
    session: AsyncSession,
    redis_client: Any,
) -> dict[str, Any] | None:
    return await AdminAuthService(session, redis_client).resolve(request.cookies.get(_panel_cookie()))


async def require_admin_panel(
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis_client: Any = Depends(get_redis_client),
) -> dict[str, Any]:
    """Authenticate a panel request, forcing TOTP enrolment when required."""
    admin = await _resolve_admin(request, session, redis_client)
    if admin is None:
        _redirect_login()
    if admin.get("needs_totp_enrollment"):
        raise AdminRedirect(_TOTP_URL)
    # Sidebar badges for the moderation queue.  The read transaction is rolled
    # back so the route below starts with a clean session, exactly as it did
    # when :meth:`AdminAuthService.resolve` committed its own idle-clock update.
    admin["pending"] = await _moderation_counts(session)
    await session.rollback()
    return admin


async def require_admin_panel_raw(
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis_client: Any = Depends(get_redis_client),
) -> dict[str, Any]:
    """Authenticate a panel request without enforcing TOTP enrolment.

    Used by the enrolment page and logout, which must stay reachable while an
    admin still has to set up their second factor.
    """
    admin = await _resolve_admin(request, session, redis_client)
    if admin is None:
        _redirect_login()
    admin["pending"] = await _moderation_counts(session)
    await session.rollback()
    return admin


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    redis_client: Any = Depends(get_redis_client),
) -> Response:
    """Render the login form, mending a pre-auth CSRF cookie if needed."""
    existing = await _resolve_admin(request, session, redis_client)
    if existing is not None:
        return RedirectResponse("/admin/", status_code=303)

    csrf_token = request.cookies.get(_LOGIN_CSRF_COOKIE) or secrets.token_urlsafe(32)
    response = _render(
        request,
        "admin/login.html",
        {"error": None, "totp_required": _settings().admin_require_totp, "login_csrf_token": csrf_token},
    )
    response.set_cookie(
        key=_LOGIN_CSRF_COOKIE,
        value=csrf_token,
        httponly=True,
        samesite="strict",
        secure=_settings().is_production,
        path=_COOKIE_PATH,
    )
    return response


@router.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    totp_code: str = Form(""),
    csrf_token: str = Form(""),
    session: AsyncSession = Depends(get_session),
    redis_client: Any = Depends(get_redis_client),
) -> Response:
    """Verify credentials and start a server-side panel session."""
    _validate_login_csrf(request, csrf_token)
    service = AdminAuthService(session, redis_client)

    def _failure(message: str, status_code: int) -> Response:
        response = _render(
            request,
            "admin/login.html",
            {
                "error": message,
                "totp_required": _settings().admin_require_totp,
                "login_csrf_token": request.cookies.get(_LOGIN_CSRF_COOKIE, ""),
            },
            status_code=status_code,
        )
        return response

    try:
        user = await service.authenticate(
            email=email,
            password=password,
            totp_code=totp_code,
            ip=get_client_ip(request),
        )
    except HTTPException as exc:
        return _failure(str(exc.detail), exc.status_code)

    token, _csrf = await service.start_session(
        user_id=int(user["id"]),
        ip=get_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )

    await AdminAuditService(session).record(
        admin=user,
        action="admin.login",
        entity="admin_session",
        ip_address=get_client_ip(request),
    )
    response = RedirectResponse("/admin/", status_code=303)
    response.set_cookie(
        key=_panel_cookie(),
        value=token,
        max_age=_settings().admin_session_absolute_hours * 3600,
        httponly=True,
        samesite="strict",
        secure=_settings().is_production,
        path=_COOKIE_PATH,
    )
    response.delete_cookie(key=_LOGIN_CSRF_COOKIE, path=_COOKIE_PATH)
    return response


@router.post("/logout")
async def logout(
    request: Request,
    csrf_token: str = Form(""),
    admin: dict[str, Any] = Depends(require_admin_panel_raw),
    session: AsyncSession = Depends(get_session),
    redis_client: Any = Depends(get_redis_client),
) -> RedirectResponse:
    """Revoke the current session server-side and clear the cookie."""
    _validate_csrf(admin, csrf_token)
    await AdminAuthService(session, redis_client).revoke(request.cookies.get(_panel_cookie()))
    await _audit(session, request, admin, "admin.logout", entity="admin_session", entity_id=admin.get("session_id"))
    response = RedirectResponse(_LOGIN_URL, status_code=303)
    response.delete_cookie(key=_panel_cookie(), path=_COOKIE_PATH)
    response.delete_cookie(key=_LOGIN_CSRF_COOKIE, path=_COOKIE_PATH)
    return response


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Render the aggregate-only dashboard."""
    stats = await AdminDashboardService(session).stats()
    return _render(
        request,
        "admin/dashboard.html",
        {
            "admin": admin,
            "csrf_token": admin["csrf_token"],
            "active": "dashboard",
            "stats": stats,
            "message": request.query_params.get("message"),
        },
    )


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

_USERS_PAGE = "/admin/users"


async def _snapshot_user(session: AsyncSession, user_id: int) -> dict[str, Any] | None:
    """Read a user's metadata, then release the read transaction."""
    user = await UserRepository(session).get_by_id(user_id)
    if user is None:
        return None
    snapshot = {
        "id": user.id,
        "email": user.email,
        "role": user.role.value,
        "suspended": user.suspended_at is not None,
        "community_banned": user.community_banned_at is not None,
    }
    await session.rollback()
    return snapshot


def _user_redirect(location: str, message: str | None = None, error: str | None = None) -> RedirectResponse:
    """Build a PRG redirect, optionally carrying a flash message."""
    url = location
    if message:
        url += ("&" if "?" in url else "?") + f"message={quote(message)}"
    elif error:
        url += ("&" if "?" in url else "?") + f"error={quote(error)}"
    return RedirectResponse(url, status_code=303)


def _safe_next(next_url: str | None) -> str:
    """Allow redirects back only to an admin-panel path (no open redirect)."""
    if next_url and next_url.startswith("/admin/") and not next_url.startswith("//"):
        return next_url
    return _USERS_PAGE


@router.get("/users", response_class=HTMLResponse)
async def users_page(
    request: Request,
    page: int = 1,
    q: str = "",
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Render the user management page (paginated, searchable)."""
    result = await UserService(session).list_users(page=page, limit=25, query=q)
    return _render(
        request,
        "admin/users.html",
        {
            "admin": admin,
            "csrf_token": admin["csrf_token"],
            "active": "users",
            "users": result["users"],
            "page": result["page"],
            "limit": result["limit"],
            "total": result["total"],
            "query": result["query"],
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.get("/users/{user_id}", response_class=HTMLResponse)
async def user_detail_page(
    request: Request,
    user_id: int,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Render one user's metadata, plan, grants and consent records.

    Deliberately metadata-only: the operator never sees the user's private
    content (chats, notes, health, finance, study…).
    """
    try:
        detail = await UserService(session).get_user_detail(user_id)
    except HTTPException:
        return _user_redirect(_USERS_PAGE, error="Usuário não encontrado.")
    return _render(
        request,
        "admin/user_detail.html",
        {
            "admin": admin,
            "csrf_token": admin["csrf_token"],
            "active": "users",
            "detail": detail,
            "grantable_plans": sorted(GRANTABLE_PLANS),
            "back_url": request.query_params.get("next") or _USERS_PAGE,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


#: pt-BR confirmation flashed after each successful user action.
_USER_ACTION_MESSAGES = {
    "promote": "Usuário promovido a administrador.",
    "demote": "Usuário rebaixado.",
    "delete": "Usuário excluído.",
    "suspend": "Usuário suspenso.",
    "unsuspend": "Suspensão removida.",
    "force-logout": "Sessões do usuário encerradas.",
    "ban": "Usuário impedido de publicar relatos.",
    "unban": "Usuário autorizado a publicar relatos.",
}


async def _user_action(
    request: Request,
    user_id: int,
    action: str,
    admin: dict[str, Any],
    session: AsyncSession,
    *,
    next_url: str | None = None,
) -> RedirectResponse:
    """Run one user action, auditing the attempt and flashing errors."""
    target = _safe_next(next_url)
    before = await _snapshot_user(session, user_id)
    service = UserService(session)
    after: dict[str, Any] | None
    try:
        if action == "promote":
            await service.promote(admin, PromoteUserCommand(target_id=user_id))
            after = {"role": "ADMIN"}
        elif action == "demote":
            await service.demote(admin, DemoteUserCommand(target_id=user_id))
            after = {"role": "USER"}
        elif action == "delete":
            await service.delete(admin, DeleteUserCommand(target_id=user_id))
            after = None
        elif action == "suspend":
            await service.set_suspended(admin, user_id, suspended=True)
            after = {"suspended": True}
        elif action == "unsuspend":
            await service.set_suspended(admin, user_id, suspended=False)
            after = {"suspended": False}
        elif action == "force-logout":
            await service.force_logout(admin, user_id)
            after = {"force_logout": True}
        elif action == "ban":
            await service.set_community_banned(admin, user_id, banned=True)
            after = {"community_banned": True}
        elif action == "unban":
            await service.set_community_banned(admin, user_id, banned=False)
            after = {"community_banned": False}
        else:
            raise HTTPException(status_code=400, detail="Ação inválida.")
    except HTTPException as exc:
        await _audit(
            session,
            request,
            admin,
            f"user.{action}",
            entity="user",
            entity_id=user_id,
            before=before,
            after={"error": str(exc.detail)},
        )
        return _user_redirect(target, error=str(exc.detail))

    await _audit(
        session,
        request,
        admin,
        f"user.{action}",
        entity="user",
        entity_id=user_id,
        before=before,
        after=after,
    )
    if action == "delete":
        return _user_redirect(_USERS_PAGE, message=_USER_ACTION_MESSAGES[action])
    return _user_redirect(target, message=_USER_ACTION_MESSAGES[action])


@router.post("/users/{user_id}/promote")
async def promote_user(
    request: Request,
    user_id: int,
    csrf_token: str = Form(""),
    next_url: str = Form(""),
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Promote a user to ADMIN."""
    _validate_csrf(admin, csrf_token)
    return await _user_action(
        request, user_id, "promote", admin, session, next_url=next_url or None
    )


@router.post("/users/{user_id}/demote")
async def demote_user(
    request: Request,
    user_id: int,
    csrf_token: str = Form(""),
    next_url: str = Form(""),
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Demote an admin to USER (never the last one)."""
    _validate_csrf(admin, csrf_token)
    return await _user_action(
        request, user_id, "demote", admin, session, next_url=next_url or None
    )


@router.post("/users/{user_id}/delete")
async def delete_user(
    request: Request,
    user_id: int,
    csrf_token: str = Form(""),
    next_url: str = Form(""),
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Delete a user."""
    _validate_csrf(admin, csrf_token)
    return await _user_action(
        request, user_id, "delete", admin, session, next_url=next_url or None
    )


@router.post("/users/{user_id}/suspend")
async def suspend_user(
    request: Request,
    user_id: int,
    csrf_token: str = Form(""),
    next_url: str = Form(""),
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Suspend an account (403 ``account_suspended`` on every API call)."""
    _validate_csrf(admin, csrf_token)
    return await _user_action(
        request, user_id, "suspend", admin, session, next_url=next_url or None
    )


@router.post("/users/{user_id}/unsuspend")
async def unsuspend_user(
    request: Request,
    user_id: int,
    csrf_token: str = Form(""),
    next_url: str = Form(""),
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Lift a suspension."""
    _validate_csrf(admin, csrf_token)
    return await _user_action(
        request, user_id, "unsuspend", admin, session, next_url=next_url or None
    )


@router.post("/users/{user_id}/force-logout")
async def force_logout_user(
    request: Request,
    user_id: int,
    csrf_token: str = Form(""),
    next_url: str = Form(""),
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Invalidate every refresh token issued to the user so far."""
    _validate_csrf(admin, csrf_token)
    return await _user_action(
        request, user_id, "force-logout", admin, session, next_url=next_url or None
    )


@router.post("/users/{user_id}/ban")
async def ban_user_post(
    request: Request,
    user_id: int,
    csrf_token: str = Form(""),
    next_url: str = Form(""),
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Stop the user from posting community reports."""
    _validate_csrf(admin, csrf_token)
    return await _user_action(
        request, user_id, "ban", admin, session, next_url=next_url or None
    )


@router.post("/users/{user_id}/unban")
async def unban_user_post(
    request: Request,
    user_id: int,
    csrf_token: str = Form(""),
    next_url: str = Form(""),
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Allow the user to post community reports again."""
    _validate_csrf(admin, csrf_token)
    return await _user_action(
        request, user_id, "unban", admin, session, next_url=next_url or None
    )


@router.post("/users/{user_id}/grant")
async def grant_user_plan(
    request: Request,
    user_id: int,
    plan: str = Form(...),
    duration_days: int = Form(...),
    reason: str = Form(""),
    csrf_token: str = Form(""),
    next_url: str = Form(""),
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Grant a plan to a user as a complimentary (non-monetary) entitlement."""
    _validate_csrf(admin, csrf_token)
    target = _safe_next(next_url)
    before = await _snapshot_user(session, user_id)
    try:
        redemption = await PromoAdminService(session).grant_plan(
            GrantPlanCommand(
                user_id=user_id,
                plan=plan.strip(),
                duration_days=duration_days,
                reason=(reason or "").strip(),
                admin_id=int(str(admin["id"])),
            )
        )
    except HTTPException as exc:
        await _audit(
            session,
            request,
            admin,
            "user.grant_plan",
            entity="user",
            entity_id=user_id,
            before=before,
            after={"error": str(exc.detail)},
        )
        return _user_redirect(target, error=str(exc.detail))

    await _audit(
        session,
        request,
        admin,
        "user.grant_plan",
        entity="user",
        entity_id=user_id,
        before=before,
        after={"plan": plan, "duration_days": duration_days, "redemption_id": redemption.id},
    )
    return _user_redirect(target, message="Plano concedido ao usuário.")


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_PROMPTS_PAGE = "/admin/system-prompts"


@router.get("/system-prompts", response_class=HTMLResponse)
async def prompts_page(
    request: Request,
    page: int = 1,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Render the system-prompt management page."""
    result = await SystemPromptService(session).list_prompts(page=page, limit=25)
    return _render(
        request,
        "admin/prompts.html",
        {
            "admin": admin,
            "csrf_token": admin["csrf_token"],
            "active": "prompts",
            "prompts": result["prompts"],
            "page": result["page"],
            "limit": result["limit"],
            "total": result["total"],
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


def _validate_prompt(tipo: str, system_prompt: str) -> str | None:
    """Validate prompt form input with the API's own request schema."""
    try:
        SystemPromptRequest(tipo=tipo, system_prompt=system_prompt)
    except ValidationError as exc:
        return str(exc.errors()[0]["msg"])
    return None


@router.post("/system-prompts")
async def create_prompt(
    request: Request,
    tipo: str = Form(...),
    system_prompt: str = Form(...),
    csrf_token: str = Form(""),
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Create a system prompt."""
    _validate_csrf(admin, csrf_token)
    error = _validate_prompt(tipo, system_prompt)
    if error is not None:
        return RedirectResponse(f"{_PROMPTS_PAGE}?error={quote(str(error))}", status_code=303)
    created = await SystemPromptService(session).create_prompt(tipo=tipo.strip(), system_prompt=system_prompt)
    await _audit(
        session,
        request,
        admin,
        "system_prompt.create",
        entity="system_prompt",
        entity_id=str(created["id"]),
        after={"tipo": created["tipo"]},
    )
    return RedirectResponse(f"{_PROMPTS_PAGE}?message={quote('Prompt criado')}", status_code=303)


@router.get("/system-prompts/{prompt_id}/edit", response_class=HTMLResponse)
async def edit_prompt_page(
    request: Request,
    prompt_id: int,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Render the edit form for a system prompt."""
    try:
        prompt = await SystemPromptService(session).get_prompt(prompt_id)
    except HTTPException:
        return RedirectResponse(f"{_PROMPTS_PAGE}?error=Prompt%20n%C3%A3o%20encontrado", status_code=303)
    return _render(
        request,
        "admin/prompt_form.html",
        {
            "admin": admin,
            "csrf_token": admin["csrf_token"],
            "active": "prompts",
            "prompt": prompt,
        },
    )


@router.post("/system-prompts/{prompt_id}")
async def update_prompt(
    request: Request,
    prompt_id: int,
    tipo: str = Form(...),
    system_prompt: str = Form(...),
    csrf_token: str = Form(""),
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Update a system prompt."""
    _validate_csrf(admin, csrf_token)
    error = _validate_prompt(tipo, system_prompt)
    if error is not None:
        return RedirectResponse(f"{_PROMPTS_PAGE}?error={quote(str(error))}", status_code=303)
    service = SystemPromptService(session)
    before = await service.get_prompt(prompt_id)
    await service.update_prompt(prompt_id, tipo=tipo.strip(), system_prompt=system_prompt)
    await _audit(
        session,
        request,
        admin,
        "system_prompt.update",
        entity="system_prompt",
        entity_id=prompt_id,
        before={"tipo": before["tipo"]},
        after={"tipo": tipo.strip()},
    )
    return RedirectResponse(f"{_PROMPTS_PAGE}?message={quote('Prompt atualizado')}", status_code=303)


@router.post("/system-prompts/{prompt_id}/delete")
async def delete_prompt(
    request: Request,
    prompt_id: int,
    csrf_token: str = Form(""),
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Delete a system prompt."""
    _validate_csrf(admin, csrf_token)
    service = SystemPromptService(session)
    try:
        before = await service.get_prompt(prompt_id)
    except HTTPException as exc:
        return RedirectResponse(f"{_PROMPTS_PAGE}?error={quote(str(exc.detail))}", status_code=303)
    await service.delete_prompt(prompt_id)
    await _audit(
        session,
        request,
        admin,
        "system_prompt.delete",
        entity="system_prompt",
        entity_id=prompt_id,
        before={"tipo": before["tipo"]},
    )
    return RedirectResponse(f"{_PROMPTS_PAGE}?message={quote('Prompt removido')}", status_code=303)


# ---------------------------------------------------------------------------
# Agent catalog ("Agentes")
# ---------------------------------------------------------------------------

_AGENTS_PAGE = "/admin/agents"
_AGENT_ENTITY_LABELS = {
    "group": "Grupo",
    "sub_agent": "Sub-agente",
    "agent": "Agente",
}
_AGENT_MODELS = {"group": "agent_group", "sub_agent": "sub_agent", "agent": "system_prompt"}
_ALLOWED_PLANS = ("free", "trial", "starter", "pro", "premium")


def _coerce_int(raw: str | None, default: int = 0) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _coerce_bool(raw: str | None) -> bool:
    return str(raw).strip().lower() in {"1", "true", "on", "yes", "sim"}


async def _form_values(request: Request) -> dict[str, str]:
    form = await request.form()
    return {str(key): str(value) for key, value in form.items()}


def _normalize_checkbox(form: dict[str, str], field: str) -> None:
    """Turn an absent checkbox field into an explicit ``""`` (false)."""
    form[field] = "true" if _coerce_bool(form.get(field)) else ""


def _build_agent_values(entity: str, form: dict[str, str], *, creating: bool) -> dict[str, Any]:
    """Translate a submitted form into service kwargs for one entity kind."""
    values: dict[str, Any] = {
        "description": form.get("description", "").strip(),
        "icon": form.get("icon", "").strip() or ("🤖" if entity == "agent" else "🧩"),
        "audience": form.get("audience", "").strip(),
        "sort_order": _coerce_int(form.get("sort_order")),
        "active": _coerce_bool(form.get("active")),
        "system_prompt": form.get("system_prompt", ""),
    }
    if entity in ("group", "sub_agent"):
        values["name"] = form.get("name", "").strip()
        if creating:
            values["slug"] = form.get("slug", "").strip()
    if entity in ("sub_agent", "agent"):
        raw_plan = form.get("min_plan", "free").strip() or "free"
        values["min_plan"] = raw_plan if raw_plan in _ALLOWED_PLANS else "free"
    if entity == "sub_agent":
        group_id = _coerce_int(form.get("group_id"), 0)
        values["group_id"] = group_id or None
    if entity == "agent":
        values["tipo"] = form.get("tipo", "").strip()
    return values


async def _agent_redirect(
    request: Request, message: str | None = None, error: str | None = None, tab: str | None = None
) -> RedirectResponse:
    suffix = f"&tab={tab}" if tab in _AGENT_ENTITY_LABELS else ""
    if error:
        return RedirectResponse(f"{_AGENTS_PAGE}?error={quote(error)}{suffix}", status_code=303)
    return RedirectResponse(f"{_AGENTS_PAGE}?message={quote(message or '')}{suffix}", status_code=303)


@router.get("/agents", response_class=HTMLResponse)
async def agents_page(
    request: Request,
    tab: str = "group",
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
    redis_client: Any = Depends(get_redis_client),
) -> Response:
    """Render the agent-catalog console (groups, sub-agents and agents)."""
    service = AgentAdminService(session, redis_client)
    active_tab = tab if tab in _AGENT_ENTITY_LABELS else "group"
    return _render(
        request,
        "admin/agents.html",
        {
            "admin": admin,
            "csrf_token": admin["csrf_token"],
            "active": "agents",
            "tab": active_tab,
            "labels": _AGENT_ENTITY_LABELS,
            "groups": await service.list_groups(),
            "sub_agents": await service.list_sub_agents(),
            "agents": await service.list_agents(),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/agents/test", response_class=HTMLResponse)
async def agent_preview(
    request: Request,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
    redis_client: Any = Depends(get_redis_client),
    ai_router: Any = Depends(get_ai_router),
) -> Response:
    """Run an unsaved draft prompt through the router (rate-limited sandbox).

    Registered before the generic ``/agents/{entity}`` POST so the literal
    ``test`` segment is not swallowed as an entity name.
    """
    form = await _form_values(request)
    _validate_csrf(admin, form.get("csrf_token"))
    draft_prompt = form.get("draft_prompt", "")
    sample_message = form.get("sample_message", "").strip()
    service = AgentPreviewService(ai_router, redis_client, _settings())
    context: dict[str, Any] = {
        "result": None,
        "error": None,
        "draft_prompt": draft_prompt,
        "sample_message": sample_message,
    }
    try:
        await service.check_rate_limit(int(admin["id"]))
    except HTTPException as exc:
        context["error"] = str(exc.detail)
    else:
        error = AgentAdminService.validate_prompt(draft_prompt, _settings().agent_prompt_max_chars)
        if error is not None:
            context["error"] = error
        elif not sample_message:
            context["error"] = "Informe uma mensagem de exemplo."
        else:
            context["result"] = await service.run(draft_prompt, sample_message)
    await _audit(
        session,
        request,
        admin,
        "agent.preview",
        entity="agent_preview",
        after={"ok": context["error"] is None},
    )
    return _render(
        request,
        "admin/agent_preview.html",
        {"admin": admin, "csrf_token": admin["csrf_token"], "active": "agents", **context},
    )


async def _load_group_options(session: AsyncSession) -> list[Any]:
    return await AgentCatalogRepository(session).list_groups(active_only=False)


@router.get("/agents/{entity}/new", response_class=HTMLResponse)
async def agent_new_page(
    request: Request,
    entity: str,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Render the empty create form for a catalog entity."""
    if entity not in _AGENT_ENTITY_LABELS:
        return await _agent_redirect(request, error="Entidade invalida")
    return _render(
        request,
        "admin/agent_form.html",
        {
            "admin": admin,
            "csrf_token": admin["csrf_token"],
            "active": "agents",
            "entity": entity,
            "label": _AGENT_ENTITY_LABELS[entity],
            "item": None,
            "group_options": await _load_group_options(session) if entity == "sub_agent" else [],
            "max_chars": _settings().agent_prompt_max_chars,
        },
    )


@router.post("/agents/{entity}")
async def agent_create(
    request: Request,
    entity: str,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
    redis_client: Any = Depends(get_redis_client),
) -> RedirectResponse:
    """Create a catalog entity and record its first prompt version."""
    form = await _form_values(request)
    _validate_csrf(admin, form.get("csrf_token"))
    if entity not in _AGENT_ENTITY_LABELS:
        return await _agent_redirect(request, error="Entidade invalida")
    values = _build_agent_values(entity, form, creating=True)
    service = AgentAdminService(session, redis_client)
    try:
        row = await service.create(
            entity, values, admin_id=int(admin["id"]), max_chars=_settings().agent_prompt_max_chars
        )
    except HTTPException as exc:
        return await _agent_redirect(request, error=str(exc.detail), tab=entity)
    await _audit(
        session,
        request,
        admin,
        "agent.create",
        entity=_AGENT_MODELS[entity],
        entity_id=int(row.id),
        after={"name": values.get("name") or values.get("tipo")},
    )
    return await _agent_redirect(request, message=f"{_AGENT_ENTITY_LABELS[entity]} criado", tab=entity)


@router.get("/agents/{entity}/{item_id}/edit", response_class=HTMLResponse)
async def agent_edit_page(
    request: Request,
    entity: str,
    item_id: int,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Render the edit form for a catalog entity."""
    if entity not in _AGENT_ENTITY_LABELS:
        return await _agent_redirect(request, error="Entidade invalida")
    service = AgentAdminService(session, None)
    try:
        item = await service.get(entity, item_id)
    except HTTPException:
        return await _agent_redirect(request, error="Item nao encontrado")
    return _render(
        request,
        "admin/agent_form.html",
        {
            "admin": admin,
            "csrf_token": admin["csrf_token"],
            "active": "agents",
            "entity": entity,
            "label": _AGENT_ENTITY_LABELS[entity],
            "item": item,
            "group_options": await _load_group_options(session) if entity == "sub_agent" else [],
            "max_chars": _settings().agent_prompt_max_chars,
        },
    )


@router.post("/agents/{entity}/{item_id}")
async def agent_update(
    request: Request,
    entity: str,
    item_id: int,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
    redis_client: Any = Depends(get_redis_client),
) -> RedirectResponse:
    """Update a catalog entity, versioning the prompt when it changed."""
    form = await _form_values(request)
    _validate_csrf(admin, form.get("csrf_token"))
    if entity not in _AGENT_ENTITY_LABELS:
        return await _agent_redirect(request, error="Entidade invalida")
    values = _build_agent_values(entity, form, creating=False)
    service = AgentAdminService(session, redis_client)
    try:
        row = await service.update(
            entity,
            item_id,
            values,
            admin_id=int(admin["id"]),
            max_chars=_settings().agent_prompt_max_chars,
            note=form.get("note") or None,
        )
    except HTTPException as exc:
        return await _agent_redirect(request, error=str(exc.detail), tab=entity)
    await _audit(
        session,
        request,
        admin,
        "agent.update",
        entity=_AGENT_MODELS[entity],
        entity_id=int(row.id),
        after={"name": values.get("name") or values.get("tipo")},
    )
    return await _agent_redirect(request, message=f"{_AGENT_ENTITY_LABELS[entity]} atualizado", tab=entity)


@router.post("/agents/{entity}/{item_id}/toggle")
async def agent_toggle(
    request: Request,
    entity: str,
    item_id: int,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
    redis_client: Any = Depends(get_redis_client),
) -> RedirectResponse:
    """Enable/disable a catalog entity."""
    form = await _form_values(request)
    _validate_csrf(admin, form.get("csrf_token"))
    if entity not in _AGENT_ENTITY_LABELS:
        return await _agent_redirect(request, error="Entidade invalida")
    service = AgentAdminService(session, redis_client)
    try:
        item = await service.get(entity, item_id)
        row = await service.set_active(entity, item_id, not bool(item.active))
    except HTTPException as exc:
        return await _agent_redirect(request, error=str(exc.detail), tab=entity)
    await _audit(
        session,
        request,
        admin,
        "agent.toggle",
        entity=_AGENT_MODELS[entity],
        entity_id=int(row.id),
        after={"active": bool(row.active)},
    )
    return await _agent_redirect(request, message="Estado atualizado", tab=entity)


@router.post("/agents/{entity}/{item_id}/duplicate")
async def agent_duplicate(
    request: Request,
    entity: str,
    item_id: int,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
    redis_client: Any = Depends(get_redis_client),
) -> RedirectResponse:
    """Clone a catalog entity as an inactive draft."""
    form = await _form_values(request)
    _validate_csrf(admin, form.get("csrf_token"))
    if entity not in _AGENT_ENTITY_LABELS:
        return await _agent_redirect(request, error="Entidade invalida")
    service = AgentAdminService(session, redis_client)
    try:
        row = await service.duplicate(
            entity, item_id, admin_id=int(admin["id"]), max_chars=_settings().agent_prompt_max_chars
        )
    except HTTPException as exc:
        return await _agent_redirect(request, error=str(exc.detail), tab=entity)
    await _audit(
        session,
        request,
        admin,
        "agent.duplicate",
        entity=_AGENT_MODELS[entity],
        entity_id=int(row.id),
        after={"source_id": item_id},
    )
    return await _agent_redirect(request, message="Copia criada como rascunho inativo", tab=entity)


@router.get("/agents/{entity}/{item_id}/versions", response_class=HTMLResponse)
async def agent_versions_page(
    request: Request,
    entity: str,
    item_id: int,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Render the version history (with diffs) and rollback controls."""
    if entity not in _AGENT_ENTITY_LABELS:
        return await _agent_redirect(request, error="Entidade invalida")
    service = AgentAdminService(session, None)
    try:
        item = await service.get(entity, item_id)
        versions = await service.list_versions(entity, item_id)
    except HTTPException:
        return await _agent_redirect(request, error="Item nao encontrado")
    return _render(
        request,
        "admin/agent_versions.html",
        {
            "admin": admin,
            "csrf_token": admin["csrf_token"],
            "active": "agents",
            "entity": entity,
            "label": _AGENT_ENTITY_LABELS[entity],
            "item": item,
            "versions": versions,
            "diffs": service.diffs(versions),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/agents/{entity}/{item_id}/rollback")
async def agent_rollback(
    request: Request,
    entity: str,
    item_id: int,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
    redis_client: Any = Depends(get_redis_client),
) -> RedirectResponse:
    """Restore a previous prompt version (append-only)."""
    form = await _form_values(request)
    _validate_csrf(admin, form.get("csrf_token"))
    if entity not in _AGENT_ENTITY_LABELS:
        return await _agent_redirect(request, error="Entidade invalida")
    version_id = _coerce_int(form.get("version_id"), 0)
    if version_id <= 0:
        return await _agent_redirect(request, error="Versao invalida")
    service = AgentAdminService(session, redis_client)
    try:
        await service.rollback(
            entity,
            item_id,
            version_id,
            admin_id=int(admin["id"]),
            max_chars=_settings().agent_prompt_max_chars,
        )
    except HTTPException as exc:
        return await _agent_redirect(request, error=str(exc.detail))
    await _audit(
        session,
        request,
        admin,
        "agent.rollback",
        entity=_AGENT_MODELS[entity],
        entity_id=item_id,
        after={"version_id": version_id},
    )
    return RedirectResponse(
        f"{_AGENTS_PAGE}/{entity}/{item_id}/versions?message={quote('Versao restaurada')}", status_code=303
    )


# ---------------------------------------------------------------------------
# Promotions & promo codes
# ---------------------------------------------------------------------------

_PROMOS_PAGE = "/admin/promotions"
_PROMO_TABS = ("codes", "promotions")
_PROMOTION_AUDIENCES = ("all", "plan", "groups")
_PROMOTION_CTA_TARGETS = ("plans", "redeem", "url")


def _promo_redirect(
    request: Request, message: str | None = None, error: str | None = None, tab: str | None = None
) -> RedirectResponse:
    suffix = f"&tab={tab}" if tab in _PROMO_TABS else ""
    if error:
        return RedirectResponse(f"{_PROMOS_PAGE}?error={quote(error)}{suffix}", status_code=303)
    return RedirectResponse(f"{_PROMOS_PAGE}?message={quote(message or '')}{suffix}", status_code=303)


@router.get("/promotions", response_class=HTMLResponse)
async def promotions_page(
    request: Request,
    tab: str = "codes",
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Render the promo-code and promotion console."""
    service = PromoAdminService(session)
    active_tab = tab if tab in _PROMO_TABS else "codes"
    return _render(
        request,
        "admin/promotions.html",
        {
            "admin": admin,
            "csrf_token": admin["csrf_token"],
            "active": "promotions",
            "tab": active_tab,
            "codes": await service.list_codes_with_usage(),
            "promotions": await service.list_promotions(),
            "kinds": [member.value for member in PromoCodeKind],
            "plans": ("trial", "starter", "pro", "premium"),
            "audiences": _PROMOTION_AUDIENCES,
            "cta_targets": _PROMOTION_CTA_TARGETS,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/promotions/codes")
async def create_promo_code(
    request: Request,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Create one promo code."""
    form = await _form_values(request)
    _validate_csrf(admin, form.get("csrf_token"))
    _normalize_checkbox(form, "active")
    try:
        code = await PromoAdminService(session).create_code(form, admin_id=int(admin["id"]))
    except HTTPException as exc:
        return _promo_redirect(request, error=str(exc.detail), tab="codes")
    await _audit(
        session,
        request,
        admin,
        "promo_code.create",
        entity="promo_code",
        entity_id=code.id,
        after={"code": code.code, "kind": code.kind},
    )
    return _promo_redirect(request, message="Código criado", tab="codes")


@router.post("/promotions/codes/generate")
async def generate_promo_codes(
    request: Request,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Bulk-generate promo codes sharing one grant definition."""
    form = await _form_values(request)
    _validate_csrf(admin, form.get("csrf_token"))
    _normalize_checkbox(form, "active")
    count = _coerce_int(form.get("count"), 0)
    try:
        codes = await PromoAdminService(session).bulk_generate(
            form, count=count, prefix=form.get("prefix", "NOVA"), admin_id=int(admin["id"])
        )
    except HTTPException as exc:
        return _promo_redirect(request, error=str(exc.detail), tab="codes")
    await _audit(
        session,
        request,
        admin,
        "promo_code.generate",
        entity="promo_code",
        after={"count": len(codes), "prefix": form.get("prefix", "NOVA")},
    )
    return _promo_redirect(request, message=f"{len(codes)} códigos gerados", tab="codes")


@router.get("/promotions/codes/export")
async def export_promo_codes(
    request: Request,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Download every promo code (with usage) as CSV."""
    csv_text = await PromoAdminService(session).export_csv()
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="codigos-promocionais.csv"'},
    )


@router.get("/promotions/codes/{code_id}", response_class=HTMLResponse)
async def promo_code_detail_page(
    request: Request,
    code_id: int,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Render one code's usage stats and redemption ledger."""
    service = PromoAdminService(session)
    try:
        stats = await service.code_stats(code_id)
        redemptions = await service.list_redemptions(code_id)
    except HTTPException as exc:
        return _promo_redirect(request, error=str(exc.detail), tab="codes")
    return _render(
        request,
        "admin/promo_code_detail.html",
        {
            "admin": admin,
            "csrf_token": admin["csrf_token"],
            "active": "promotions",
            "stats": stats,
            "redemptions": redemptions,
            "kinds": [member.value for member in PromoCodeKind],
            "plans": ("trial", "starter", "pro", "premium"),
        },
    )


@router.post("/promotions/codes/{code_id}/toggle")
async def toggle_promo_code(
    request: Request,
    code_id: int,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Activate/deactivate a promo code."""
    form = await _form_values(request)
    _validate_csrf(admin, form.get("csrf_token"))
    try:
        code = await PromoAdminService(session).toggle_code(code_id)
    except HTTPException as exc:
        return _promo_redirect(request, error=str(exc.detail), tab="codes")
    await _audit(
        session,
        request,
        admin,
        "promo_code.toggle",
        entity="promo_code",
        entity_id=code_id,
        after={"active": code.active},
    )
    return _promo_redirect(request, message="Estado atualizado", tab="codes")


@router.post("/promotions/codes/{code_id}")
async def update_promo_code(
    request: Request,
    code_id: int,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Update a promo code."""
    form = await _form_values(request)
    _validate_csrf(admin, form.get("csrf_token"))
    _normalize_checkbox(form, "active")
    try:
        code = await PromoAdminService(session).update_code(code_id, form)
    except HTTPException as exc:
        return _promo_redirect(request, error=str(exc.detail), tab="codes")
    await _audit(
        session,
        request,
        admin,
        "promo_code.update",
        entity="promo_code",
        entity_id=code.id,
        after={"code": code.code, "kind": code.kind, "active": code.active},
    )
    return _promo_redirect(request, message="Código atualizado", tab="codes")


@router.get("/promotions/grants", response_class=HTMLResponse)
async def grants_page(
    request: Request,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Render the manual plan-grant form and recent grants."""
    return _render(
        request,
        "admin/promotion_grants.html",
        {
            "admin": admin,
            "csrf_token": admin["csrf_token"],
            "active": "promotions",
            "plans": ("starter", "pro", "premium"),
            "grants": await PromoAdminService(session).list_recent_manual_grants(),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/promotions/grants")
async def grant_plan_to_user(
    request: Request,
    user_id: int = Form(...),
    plan: str = Form(...),
    duration_days: int = Form(...),
    reason: str = Form(""),
    csrf_token: str = Form(""),
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Grant a plan to a user directly (complimentary, no code)."""
    _validate_csrf(admin, csrf_token)
    command = GrantPlanCommand(
        user_id=user_id,
        plan=plan.strip(),
        duration_days=duration_days,
        reason=reason,
        admin_id=int(admin["id"]),
    )
    try:
        grant = await PromoAdminService(session).grant_plan(command)
    except HTTPException as exc:
        return RedirectResponse(
            f"/admin/promotions/grants?error={quote(str(exc.detail))}", status_code=303
        )
    await _audit(
        session,
        request,
        admin,
        "promo.grant_plan",
        entity="promo_redemption",
        entity_id=grant.id,
        after={"user_id": user_id, "plan": plan.strip(), "duration_days": duration_days},
    )
    return RedirectResponse(
        f"/admin/promotions/grants?message={quote('Plano concedido')}", status_code=303
    )


@router.get("/promotions/new", response_class=HTMLResponse)
async def promotion_new_page(
    request: Request,
    admin: dict[str, Any] = Depends(require_admin_panel),
) -> Response:
    """Render the empty promotion form."""
    return _render(
        request,
        "admin/promotion_form.html",
        {
            "admin": admin,
            "csrf_token": admin["csrf_token"],
            "active": "promotions",
            "promotion": None,
            "audiences": _PROMOTION_AUDIENCES,
            "cta_targets": _PROMOTION_CTA_TARGETS,
        },
    )


@router.get("/promotions/{promotion_id}/edit", response_class=HTMLResponse)
async def promotion_edit_page(
    request: Request,
    promotion_id: int,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Render the edit form for a promotion."""
    try:
        promotion = await PromoAdminService(session).get_promotion(promotion_id)
    except HTTPException as exc:
        return _promo_redirect(request, error=str(exc.detail), tab="promotions")
    return _render(
        request,
        "admin/promotion_form.html",
        {
            "admin": admin,
            "csrf_token": admin["csrf_token"],
            "active": "promotions",
            "promotion": promotion,
            "audiences": _PROMOTION_AUDIENCES,
            "cta_targets": _PROMOTION_CTA_TARGETS,
        },
    )


@router.post("/promotions")
async def create_promotion(
    request: Request,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Create a promotion card."""
    form = await _form_values(request)
    _validate_csrf(admin, form.get("csrf_token"))
    _normalize_checkbox(form, "active")
    try:
        promotion = await PromoAdminService(session).create_promotion(form)
    except HTTPException as exc:
        return _promo_redirect(request, error=str(exc.detail), tab="promotions")
    await _audit(
        session,
        request,
        admin,
        "promotion.create",
        entity="promotion",
        entity_id=promotion.id,
        after={"title": promotion.title, "audience": promotion.audience},
    )
    return _promo_redirect(request, message="Promoção criada", tab="promotions")


@router.post("/promotions/{promotion_id}/toggle")
async def toggle_promotion(
    request: Request,
    promotion_id: int,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Enable/disable a promotion card."""
    form = await _form_values(request)
    _validate_csrf(admin, form.get("csrf_token"))
    try:
        promotion = await PromoAdminService(session).toggle_promotion(promotion_id)
    except HTTPException as exc:
        return _promo_redirect(request, error=str(exc.detail), tab="promotions")
    await _audit(
        session,
        request,
        admin,
        "promotion.toggle",
        entity="promotion",
        entity_id=promotion_id,
        after={"active": promotion.active},
    )
    return _promo_redirect(request, message="Estado atualizado", tab="promotions")


@router.post("/promotions/{promotion_id}/delete")
async def delete_promotion(
    request: Request,
    promotion_id: int,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Delete a promotion card."""
    form = await _form_values(request)
    _validate_csrf(admin, form.get("csrf_token"))
    try:
        await PromoAdminService(session).delete_promotion(promotion_id)
    except HTTPException as exc:
        return _promo_redirect(request, error=str(exc.detail), tab="promotions")
    await _audit(
        session,
        request,
        admin,
        "promotion.delete",
        entity="promotion",
        entity_id=promotion_id,
    )
    return _promo_redirect(request, message="Promoção removida", tab="promotions")


@router.post("/promotions/{promotion_id}")
async def update_promotion(
    request: Request,
    promotion_id: int,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Update a promotion card."""
    form = await _form_values(request)
    _validate_csrf(admin, form.get("csrf_token"))
    _normalize_checkbox(form, "active")
    try:
        promotion = await PromoAdminService(session).update_promotion(promotion_id, form)
    except HTTPException as exc:
        return _promo_redirect(request, error=str(exc.detail), tab="promotions")
    await _audit(
        session,
        request,
        admin,
        "promotion.update",
        entity="promotion",
        entity_id=promotion.id,
        after={"title": promotion.title, "active": promotion.active},
    )
    return _promo_redirect(request, message="Promoção atualizada", tab="promotions")


# ---------------------------------------------------------------------------
# Community-report moderation
# ---------------------------------------------------------------------------

_REPORTS_PAGE = "/admin/reports"

_STATUS_FILTERS = {
    "submitted": ReportStatus.SUBMITTED,
    "confirmed": ReportStatus.CONFIRMED,
    "flagged": ReportStatus.FLAGGED,
    "removed": ReportStatus.REMOVED,
}


def _parse_status_filter(raw: str | None) -> ReportStatus | None:
    """Map an optional querystring value to a :class:`ReportStatus`."""
    if not raw:
        return None
    return _STATUS_FILTERS.get(raw.strip().lower())


@router.get("/reports", response_class=HTMLResponse)
async def reports_page(
    request: Request,
    page: int = 1,
    status: str | None = None,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Render the community-report moderation queue (paginated)."""
    status_filter = _parse_status_filter(status)
    reports, total = await CommunityService(session).list_reports_for_moderation(
        status=status_filter, page=page, limit=25
    )
    return _render(
        request,
        "admin/reports.html",
        {
            "admin": admin,
            "csrf_token": admin["csrf_token"],
            "active": "reports",
            "reports": reports,
            "page": page,
            "limit": 25,
            "total": total,
            "status_filter": status if status_filter is not None else "",
            "statuses": list(_STATUS_FILTERS),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/reports/{report_id}/status")
async def moderate_report(
    request: Request,
    report_id: int,
    status: str = Form(...),
    csrf_token: str = Form(""),
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Apply a moderation decision to a report."""
    _validate_csrf(admin, csrf_token)
    new_status = _parse_status_filter(status)
    if new_status is None:
        return RedirectResponse(f"{_REPORTS_PAGE}?error={quote('Status inválido')}", status_code=303)

    existing = await CommunityReportRepository(session).get(report_id)
    before = {"status": existing.status.value} if existing is not None else None
    await session.rollback()
    try:
        await CommunityService(session).moderate_report(report_id, new_status)
    except HTTPException as exc:
        await _audit(
            session,
            request,
            admin,
            "report.moderate",
            entity="community_report",
            entity_id=report_id,
            before=before,
            after={"error": str(exc.detail)},
        )
        return RedirectResponse(f"{_REPORTS_PAGE}?error={quote(str(exc.detail))}", status_code=303)
    await _audit(
        session,
        request,
        admin,
        "report.moderate",
        entity="community_report",
        entity_id=report_id,
        before=before,
        after={"status": new_status.value},
    )
    return RedirectResponse(f"{_REPORTS_PAGE}?message={quote('Relato atualizado')}", status_code=303)


# ---------------------------------------------------------------------------
# Audit trail viewer
# ---------------------------------------------------------------------------

_AUDIT_PAGE = "/admin/audit"


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(
    request: Request,
    page: int = 1,
    action: str | None = None,
    entity: str | None = None,
    admin_id: int | None = None,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Render the read-only audit trail with filters."""
    service = AdminAuditService(session)
    rows, total = await service.repo.list_entries(
        page=page,
        limit=50,
        admin_id=admin_id,
        action=(action or None),
        entity=(entity or None),
    )
    actions = await service.repo.distinct_actions()
    return _render(
        request,
        "admin/audit.html",
        {
            "admin": admin,
            "csrf_token": admin["csrf_token"],
            "active": "audit",
            "entries": rows,
            "page": page,
            "limit": 50,
            "total": total,
            "actions": actions,
            "action_filter": action or "",
            "entity_filter": entity or "",
            "admin_id_filter": admin_id,
        },
    )


# ---------------------------------------------------------------------------
# TOTP enrolment
# ---------------------------------------------------------------------------


@router.get("/security/totp", response_class=HTMLResponse)
async def totp_page(
    request: Request,
    admin: dict[str, Any] = Depends(require_admin_panel_raw),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Render the TOTP enrolment form with a freshly generated secret."""
    candidate = generate_secret()
    uri = provisioning_uri(candidate, admin["email"], _settings().admin_totp_issuer)
    return _render(
        request,
        "admin/totp.html",
        {
            "admin": admin,
            "csrf_token": admin["csrf_token"],
            "active": "security",
            "secret": candidate,
            "otpauth_uri": uri,
            "error": request.query_params.get("error"),
        },
    )


@router.post("/security/totp")
async def totp_enroll(
    request: Request,
    secret: str = Form(...),
    code: str = Form(...),
    csrf_token: str = Form(""),
    admin: dict[str, Any] = Depends(require_admin_panel_raw),
    session: AsyncSession = Depends(get_session),
    redis_client: Any = Depends(get_redis_client),
) -> RedirectResponse:
    """Verify the first code and persist the TOTP secret."""
    _validate_csrf(admin, csrf_token)
    if not verify_totp(secret, code):
        return RedirectResponse(f"{_TOTP_URL}?error={quote('Código inválido. Tente novamente.')}", status_code=303)
    await AdminAuthService(session, redis_client).set_totp_secret(int(admin["id"]), secret)
    await _audit(session, request, admin, "admin.totp_enroll", entity="user", entity_id=str(admin["id"]))
    return RedirectResponse(f"{_TOTP_URL}?message={quote('Verificação em duas etapas ativada')}", status_code=303)


@router.post("/security/totp/disable")
async def totp_disable(
    request: Request,
    csrf_token: str = Form(""),
    admin: dict[str, Any] = Depends(require_admin_panel_raw),
    session: AsyncSession = Depends(get_session),
    redis_client: Any = Depends(get_redis_client),
) -> RedirectResponse:
    """Disable TOTP for the current admin (refused when 2FA is mandatory)."""
    _validate_csrf(admin, csrf_token)
    if _settings().admin_require_totp:
        return RedirectResponse(
            f"{_TOTP_URL}?error={quote('A verificação em duas etapas é obrigatória nesta instalação.')}",
            status_code=303,
        )
    await AdminAuthService(session, redis_client).clear_totp_secret(int(admin["id"]))
    await _audit(session, request, admin, "admin.totp_disable", entity="user", entity_id=str(admin["id"]))
    return RedirectResponse(f"{_TOTP_URL}?message={quote('Verificação em duas etapas desativada')}", status_code=303)


# ---------------------------------------------------------------------------
# Configurações (remote app config / plan ceilings / AI ops / contacts)
# ---------------------------------------------------------------------------

_SETTINGS_PAGE = "/admin/settings"
_SETTINGS_TABS = ("app", "limits", "ai", "contacts")


def _settings_redirect(
    tab: str, message: str | None = None, error: str | None = None
) -> RedirectResponse:
    """Redirect (303) back to a settings tab with a flash message."""
    active = tab if tab in _SETTINGS_TABS else "app"
    if error:
        return RedirectResponse(f"{_SETTINGS_PAGE}?tab={active}&error={quote(error)}", status_code=303)
    return RedirectResponse(f"{_SETTINGS_PAGE}?tab={active}&message={quote(message or '')}", status_code=303)


def _parse_announcements(raw: str) -> list[dict[str, str]]:
    """Parse the announcements textarea (``Título | Corpo | nível`` per line)."""
    items: list[dict[str, str]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split("|")]
        items.append(
            {
                "title": parts[0],
                "body": parts[1] if len(parts) > 1 else "",
                "level": parts[2] if len(parts) > 2 and parts[2] else "info",
            }
        )
    return items


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    tab: str = "app",
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
    redis_client: Any = Depends(get_redis_client),
    router_ai: Any = Depends(get_ai_router),
) -> Response:
    """Render the Configurações section (app config, limits, AI ops, contacts)."""
    settings = _settings()
    active_tab = tab if tab in _SETTINGS_TABS else "app"
    context: dict[str, Any] = {
        "admin": admin,
        "csrf_token": admin["csrf_token"],
        "active": "settings",
        "tab": active_tab,
        "message": request.query_params.get("message"),
        "error": request.query_params.get("error"),
    }
    if active_tab == "limits":
        context["plans"] = await PlanLimitOverrideService(session, settings).effective_rows()
        context["editable_fields"] = EDITABLE_FIELDS
    elif active_tab == "ai":
        ops = AIOpsService(session, settings=settings, redis=redis_client, router=router_ai)
        context["providers"] = ops.provider_overview()
        context["total_fallbacks"] = ops.total_fallbacks()
        context["usage"] = await ops.usage(days=14)
    elif active_tab == "contacts":
        context["contacts"] = await OfficialContactService(session).list()
        context["categories"] = [category.value for category in OfficialContactCategory]
    else:
        context["app_config"] = await AppConfigService(session, settings).load()
        context["modules"] = MODULE_FLAGS
    return _render(request, "admin/settings.html", context)


@router.post("/settings/app")
async def update_app_config(
    request: Request,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
    redis_client: Any = Depends(get_redis_client),
) -> RedirectResponse:
    """Validate and persist the remote app configuration."""
    form = await _form_values(request)
    _validate_csrf(admin, form.get("csrf_token"))
    _normalize_checkbox(form, "maintenance_mode")
    values: dict[str, Any] = {
        "maintenance_mode": _coerce_bool(form.get("maintenance_mode")),
        "maintenance_message": form.get("maintenance_message", ""),
        "min_supported_app_version": form.get("min_supported_app_version", ""),
        "recommended_app_version": form.get("recommended_app_version", ""),
        "store_url": form.get("store_url", ""),
        "support_email": form.get("support_email", ""),
        "support_whatsapp_url": form.get("support_whatsapp_url", ""),
        "feature_flags": {
            module: _coerce_bool(form.get(f"feature_{module}")) for module in MODULE_FLAGS
        },
        "announcements": _parse_announcements(form.get("announcements", "")),
    }
    service = AppConfigService(session, _settings())
    try:
        after = await service.update(values, admin_id=int(admin["id"]))
    except ValueError as exc:
        return _settings_redirect("app", error=str(exc))
    await service.invalidate_cache(redis_client)
    await _audit(session, request, admin, "config.update", entity="app_config", after=after)
    return _settings_redirect("app", message="Configuração salva.")


@router.post("/settings/limits/{plan}")
async def update_plan_limits(
    request: Request,
    plan: str,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Validate and persist one plan's ceiling overrides."""
    form = await _form_values(request)
    _validate_csrf(admin, form.get("csrf_token"))
    if plan not in {tier.value for tier in PANEL_PLANS}:
        return _settings_redirect("limits", error="Plano inválido.")
    service = PlanLimitOverrideService(session, _settings())
    try:
        values = service.parse(form)
    except HTTPException as exc:
        return _settings_redirect("limits", error=str(exc.detail))
    await service.update(plan, values, admin_id=int(admin["id"]))
    await _audit(
        session, request, admin, "config.plan_limits", entity="plan", entity_id=plan, after=values
    )
    return _settings_redirect("limits", message=f"Limites de {plan} atualizados.")


@router.post("/settings/limits/{plan}/clear")
async def clear_plan_limits(
    request: Request,
    plan: str,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Drop every override for ``plan``, reverting to the code defaults."""
    form = await _form_values(request)
    _validate_csrf(admin, form.get("csrf_token"))
    await PlanLimitOverrideService(session, _settings()).clear(plan, admin_id=int(admin["id"]))
    await _audit(
        session, request, admin, "config.plan_limits_clear", entity="plan", entity_id=plan
    )
    return _settings_redirect("limits", message=f"Limites de {plan} restaurados ao padrão.")


@router.post("/settings/ai/{name}")
async def update_ai_provider(
    request: Request,
    name: str,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
    redis_client: Any = Depends(get_redis_client),
    router_ai: Any = Depends(get_ai_router),
) -> RedirectResponse:
    """Toggle a provider and/or change its priority."""
    form = await _form_values(request)
    _validate_csrf(admin, form.get("csrf_token"))
    _normalize_checkbox(form, "enabled")
    priority = _coerce_int(form.get("priority"), 100)
    ops = AIOpsService(session, settings=_settings(), redis=redis_client, router=router_ai)
    try:
        await ops.update_provider(
            name, enabled=_coerce_bool(form.get("enabled")), priority=priority, admin_id=int(admin["id"])
        )
    except HTTPException as exc:
        return _settings_redirect("ai", error=str(exc.detail))
    await _audit(
        session,
        request,
        admin,
        "config.ai_provider",
        entity="ai_provider",
        entity_id=name,
        after={"enabled": _coerce_bool(form.get("enabled")), "priority": priority},
    )
    return _settings_redirect("ai", message=f"Provedor {name} atualizado.")


@router.post("/settings/contacts")
async def create_official_contact(
    request: Request,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Create a new official contact from the submitted form."""
    form = await _form_values(request)
    _validate_csrf(admin, form.get("csrf_token"))
    service = OfficialContactService(session)
    try:
        values = service.parse(form)
    except HTTPException as exc:
        return _settings_redirect("contacts", error=str(exc.detail))
    contact = await service.create(values)
    await _audit(
        session, request, admin, "config.contact_create", entity="official_contact", entity_id=contact.id
    )
    return _settings_redirect("contacts", message="Contato criado.")


@router.post("/settings/contacts/{contact_id}")
async def update_official_contact(
    request: Request,
    contact_id: int,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Update an existing official contact."""
    form = await _form_values(request)
    _validate_csrf(admin, form.get("csrf_token"))
    service = OfficialContactService(session)
    contact = await service.get(contact_id)
    if contact is None:
        return _settings_redirect("contacts", error="Contato não encontrado.")
    try:
        values = service.parse(form)
    except HTTPException as exc:
        return _settings_redirect("contacts", error=str(exc.detail))
    await service.update(contact, values)
    await _audit(
        session, request, admin, "config.contact_update", entity="official_contact", entity_id=contact_id
    )
    return _settings_redirect("contacts", message="Contato atualizado.")


@router.post("/settings/contacts/{contact_id}/delete")
async def delete_official_contact(
    request: Request,
    contact_id: int,
    admin: dict[str, Any] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Delete an official contact."""
    form = await _form_values(request)
    _validate_csrf(admin, form.get("csrf_token"))
    service = OfficialContactService(session)
    contact = await service.get(contact_id)
    if contact is None:
        return _settings_redirect("contacts", error="Contato não encontrado.")
    await service.delete(contact)
    await _audit(
        session, request, admin, "config.contact_delete", entity="official_contact", entity_id=contact_id
    )
    return _settings_redirect("contacts", message="Contato removido.")
