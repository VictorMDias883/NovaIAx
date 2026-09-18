"""
Server-rendered admin panel for the NovaIAx gateway.

This router is a thin **presentation layer** over the existing service
layer (``UserService``, ``AuthService``, ``SystemPromptService``) — it
contains no business logic of its own, only route protection and
template rendering.

Routes:
    GET  /admin/login                            — login form
    POST /admin/login                            — authenticate (ADMIN only), set session cookie
    GET  /admin/logout                           — clear the session cookie
    GET  /admin                                  — dashboard
    GET  /admin/users                            — list users
    POST /admin/users/{id}/promote|demote|delete — modify a user
    GET  /admin/system-prompts                   — list system prompts
    POST /admin/system-prompts                   — create a system prompt
    GET  /admin/system-prompts/{id}/edit         — edit form
    POST /admin/system-prompts/{id}              — update a system prompt
    POST /admin/system-prompts/{id}/delete       — delete a system prompt

Authentication is a JWT access token (produced by the existing login
flow) stored in an httpOnly cookie.  Every page except ``/admin/login``
requires a valid ADMIN session; anything else is redirected there.

.. note::
    The AuthMiddleware treats ``/admin`` as public because the panel has
    its *own* authentication scheme (cookie instead of ``Authorization``
    header).  Route-level authorization below is therefore the only gate,
    and is enforced on every handler via :func:`require_admin_panel`.
"""

from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_session
from app.commands.delete_user_command import DeleteUserCommand
from app.commands.demote_user_command import DemoteUserCommand
from app.commands.login_command import LoginCommand
from app.commands.promote_user_command import PromoteUserCommand
from app.core.config import get_settings
from app.core.security import decode_token
from app.models.user import UserRole
from app.repositories.user_repository import UserRepository
from app.schemas.system_prompt_schemas import SystemPromptRequest
from app.services.auth_service import AuthService
from app.services.system_prompt_service import SystemPromptService
from app.services.user_service import UserService

router = APIRouter(prefix="/admin", tags=["admin"])

# ``app/templates`` — sibling of the ``app/api`` package that holds this module.
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

_LOGIN_URL = "/admin/login"


class AdminLoginRedirect(Exception):
    """Internal signal that the browser must be redirected to the login page.

    Raised by :func:`require_admin_panel` (via a ``Depends`` dependency)
    whenever a request reaches a protected page without a valid ADMIN
    session.  A dedicated exception handler — installed by
    :func:`register_admin_exception_handler` — converts it into a real
    303 ``Location: /admin/login`` redirect.

    A plain :class:`HTTPException` cannot be used here because this
    project's global HTTP handler serializes exceptions to JSON and
    drops redirect headers.
    """

    def __init__(self) -> None:
        super().__init__("Admin login required")
        self.headers = {"Location": _LOGIN_URL}


def register_admin_exception_handler(app: FastAPI) -> None:
    """Register the exception handler that turns :class:`AdminLoginRedirect` into a redirect.

    Starlette resolves exception handlers by walking ``type(exc).__mro__``,
    so this handler wins even though the generic ``Exception`` catch-all
    from :func:`app.exceptions.handlers.register_exception_handlers` is
    registered on the same app.
    """

    @app.exception_handler(AdminLoginRedirect)
    async def _admin_login_redirect_handler(request: Request, exc: AdminLoginRedirect) -> RedirectResponse:
        return RedirectResponse(exc.headers["Location"], status_code=303)


def _redirect_login() -> NoReturn:
    """Terminate the request by redirecting the browser to the login page."""
    raise AdminLoginRedirect()


def _cookie_name() -> str:
    return get_settings().admin_cookie_name


async def require_admin_panel(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """Authenticate an admin-panel request from its session cookie.

    Reads the JWT access token from the httpOnly cookie, verifies it, loads
    the user, and requires the ``ADMIN`` role.  On any failure the request is
    redirected (303) to ``/admin/login``.

    Returns a dictionary with ``id``, ``full_name``, ``email`` and ``role``,
    ready to be passed to the service layer as the acting user.
    """
    token = request.cookies.get(_cookie_name())
    if not token:
        _redirect_login()

    try:
        payload = decode_token(token)
    except Exception:
        _redirect_login()
    if payload.get("type") != "access":
        _redirect_login()

    user_id = payload.get("sub")
    if not user_id:
        _redirect_login()

    user = await UserRepository(session).get_by_id(int(user_id))
    if user is None or user.role != UserRole.ADMIN:
        _redirect_login()

    # Copy the identity into plain values now: the ``rollback()`` below
    # expires the ORM instance and attribute access afterwards would need
    # a lazy async refresh (which raises MissingGreenlet).
    identity = {
        "id": str(user.id),
        "full_name": user.full_name,
        "email": user.email,
        "role": user.role.value,
    }

    # The SELECT above auto-began a transaction on this request-scoped
    # session.  Release it so the service layer — which guards its writes
    # with ``async with session.begin():`` (e.g. ``UserService.promote``)
    # — can start its own transaction instead of hitting SQLAlchemy's
    # "A transaction is already begun on this Session" error.
    await session.rollback()

    return identity


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str | None = None) -> Response:
    """Render the admin login form."""
    # Already authenticated as an admin?  Straight to the dashboard.
    token = request.cookies.get(_cookie_name())
    if token:
        try:
            payload = decode_token(token)
            if payload.get("type") == "access":
                return RedirectResponse("/admin/", status_code=303)
        except Exception:
            pass

    return templates.TemplateResponse(
        request=request,
        name="admin/login.html",
        context={"error": error},
    )


@router.post("/login")
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_session),
) -> Any:
    """Authenticate credentials against the existing login flow.

    Only users with the ``ADMIN`` role are allowed in.  On success the JWT
    access token returned by :class:`AuthService` is stored in an httpOnly
    cookie and the browser is redirected to the dashboard.
    """
    settings = get_settings()
    try:
        result = await AuthService(session).login(LoginCommand(email=email.strip(), password=password))
    except HTTPException:
        return templates.TemplateResponse(
            request=request,
            name="admin/login.html",
            context={"error": "Invalid email or password."},
            status_code=401,
        )

    if result["user"]["role"] != UserRole.ADMIN.value:
        return templates.TemplateResponse(
            request=request,
            name="admin/login.html",
            context={"error": "Only administrators can access the panel."},
            status_code=403,
        )

    response = RedirectResponse("/admin/", status_code=303)
    response.set_cookie(
        key=settings.admin_cookie_name,
        value=result["access_token"],
        max_age=settings.access_token_ttl_minutes * 60,
        httponly=True,
        samesite="lax",
        secure=settings.environment == "production",
        path="/admin",
    )
    return response


@router.post("/logout")
async def logout() -> RedirectResponse:
    """Clear the admin session cookie and return to the login page."""
    response = RedirectResponse(_LOGIN_URL, status_code=303)
    response.delete_cookie(key=_cookie_name(), path="/admin")
    return response


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    admin: dict[str, str] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the admin dashboard with a few headline numbers."""
    users = await UserService(session).list_users(page=1, limit=100)
    prompts = await SystemPromptService(session).list_prompts(page=1, limit=100)
    repo = UserRepository(session)
    total_admins = await repo.count_admins()

    return templates.TemplateResponse(
        request=request,
        name="admin/dashboard.html",
        context={
            "admin": admin,
            "stats": {
                "total_users": users["total"],
                "total_prompts": prompts["total"],
                "total_admins": total_admins,
            },
        },
    )


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

_USERS_PAGE = "/admin/users"


@router.get("/users", response_class=HTMLResponse)
async def users_page(
    request: Request,
    page: int = 1,
    admin: dict[str, str] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the user management page (paginated)."""
    result = await UserService(session).list_users(page=page, limit=25)
    return templates.TemplateResponse(
        request=request,
        name="admin/users.html",
        context={
            "admin": admin,
            "users": result["users"],
            "page": result["page"],
            "limit": result["limit"],
            "total": result["total"],
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


async def _user_action(
    user_id: int,
    action: str,
    admin: dict[str, str],
    session: AsyncSession,
) -> RedirectResponse:
    """Run a user-role action, keeping errors as a redirect flash."""
    service = UserService(session)
    try:
        if action == "promote":
            await service.promote(admin, PromoteUserCommand(target_id=user_id))
        elif action == "demote":
            await service.demote(admin, DemoteUserCommand(target_id=user_id))
        else:
            await service.delete(admin, DeleteUserCommand(target_id=user_id))
    except HTTPException as exc:
        return RedirectResponse(f"{_USERS_PAGE}?error={quote(exc.detail)}", status_code=303)
    return RedirectResponse(_USERS_PAGE, status_code=303)


@router.post("/users/{user_id}/promote")
async def promote_user(
    user_id: int,
    admin: dict[str, str] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Promote a user to ADMIN (reuses :meth:`UserService.promote`)."""
    return await _user_action(user_id, "promote", admin, session)


@router.post("/users/{user_id}/demote")
async def demote_user(
    user_id: int,
    admin: dict[str, str] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Demote a user to USER (reuses :meth:`UserService.demote`)."""
    return await _user_action(user_id, "demote", admin, session)


@router.post("/users/{user_id}/delete")
async def delete_user(
    user_id: int,
    admin: dict[str, str] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Delete a user (reuses :meth:`UserService.delete`)."""
    return await _user_action(user_id, "delete", admin, session)


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_PROMPTS_PAGE = "/admin/system-prompts"


@router.get("/system-prompts", response_class=HTMLResponse)
async def prompts_page(
    request: Request,
    page: int = 1,
    admin: dict[str, str] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render the system-prompt management page."""
    result = await SystemPromptService(session).list_prompts(page=page, limit=25)
    return templates.TemplateResponse(
        request=request,
        name="admin/prompts.html",
        context={
            "admin": admin,
            "prompts": result["prompts"],
            "page": result["page"],
            "limit": result["limit"],
            "total": result["total"],
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


def _validate_prompt(tipo: str, system_prompt: str) -> str | None:
    """Validate prompt form input using the API's own request schema.

    Returns ``None`` when valid, otherwise the validation error message.
    """
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
    admin: dict[str, str] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
):
    """Create a system prompt (reuses :meth:`SystemPromptService.create_prompt`)."""
    error = _validate_prompt(tipo, system_prompt)
    if error is not None:
        return RedirectResponse(f"{_PROMPTS_PAGE}?error={quote(str(error))}", status_code=303)
    await SystemPromptService(session).create_prompt(tipo=tipo.strip(), system_prompt=system_prompt)
    return RedirectResponse(_PROMPTS_PAGE, status_code=303)


@router.get("/system-prompts/{prompt_id}/edit", response_class=HTMLResponse)
async def edit_prompt_page(
    request: Request,
    prompt_id: int,
    admin: dict[str, str] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Render the edit form for a system prompt."""
    try:
        prompt = await SystemPromptService(session).get_prompt(prompt_id)
    except HTTPException:
        return RedirectResponse(f"{_PROMPTS_PAGE}?error=System%20prompt%20not%20found", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="admin/prompt_form.html",
        context={"admin": admin, "prompt": prompt},
    )


@router.post("/system-prompts/{prompt_id}")
async def update_prompt(
    prompt_id: int,
    tipo: str = Form(...),
    system_prompt: str = Form(...),
    admin: dict[str, str] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
):
    """Update a system prompt (reuses :meth:`SystemPromptService.update_prompt`)."""
    error = _validate_prompt(tipo, system_prompt)
    if error is not None:
        return RedirectResponse(f"{_PROMPTS_PAGE}?error={quote(str(error))}", status_code=303)
    await SystemPromptService(session).update_prompt(prompt_id, tipo=tipo.strip(), system_prompt=system_prompt)
    return RedirectResponse(_PROMPTS_PAGE, status_code=303)


@router.post("/system-prompts/{prompt_id}/delete")
async def delete_prompt(
    prompt_id: int,
    admin: dict[str, str] = Depends(require_admin_panel),
    session: AsyncSession = Depends(get_session),
) -> RedirectResponse:
    """Delete a system prompt (reuses :meth:`SystemPromptService.delete_prompt`)."""
    await SystemPromptService(session).delete_prompt(prompt_id)
    return RedirectResponse(_PROMPTS_PAGE, status_code=303)
