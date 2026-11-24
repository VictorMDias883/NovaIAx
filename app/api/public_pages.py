"""
Server-rendered public compliance pages (privacy / terms / account deletion).

These routes render the product's public legal and operational documents as
HTML (currently in pt-BR, the product's target market):

    GET  /privacy                  — privacy policy, including the list of
                                     third-party processors (derived from
                                     the configured AI providers).
    GET  /terms                    — terms of service.
    GET  /account-deletion         — account-deletion form.
    POST /account-deletion/request — process the form.

The pages are deliberately **public** (whitelisted in the auth middleware):
a user who wants to leave must be able to request it even if they can no
longer authenticate — that is the point of a deletion *request* path.  The
write side is protected by a CSRF token minted into a cookie, exactly like
the admin panel's, so a third-party site cannot drive the form.

Two accepted flows on the form:

* **email + password** — proof of ownership; the account is deleted
  immediately through the same :class:`AccountDeletionService` the API uses.
* **email only** — a :class:`DeletionRequest` row is queued so an operator
  resolves it out-of-band (the caller lost credentials or never wants to
  prove who they are).
"""

import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_redis_client, get_session
from app.cache.login_rate_limiter import LoginRateLimiter
from app.cache.redis_client import RedisClient
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.network import get_client_ip
from app.core.security import spend_password_check
from app.models.deletion_request import DeletionRequest
from app.repositories.user_repository import UserRepository
from app.services.privacy_service import AccountDeletionService
from app.services.user_service import _password_matches

logger = get_logger(__name__)

router = APIRouter(prefix="", tags=["public"])

_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

_CSRF_COOKIE = "novaiax_public_csrf"
_CSRF_COOKIE_PATH = "/"


def _render_public(request: Request, name: str, context: dict, mint_cookie: bool = True, **kwargs: Any):
    """Render a public template with a CSRF token in its context.

    Mirrors :func:`app.api.admin._render_admin`: the token handed to the
    template is always the value already stored in the cookie (minted once,
    on the first visit), so hidden fields and the cookie never drift apart.
    """
    settings = get_settings()
    if mint_cookie and not request.cookies.get(_CSRF_COOKIE):
        token = secrets.token_urlsafe(32)
        context = {**context, "csrf_token": token}
        response = templates.TemplateResponse(request=request, name=name, context=context, **kwargs)
        response.set_cookie(
            key=_CSRF_COOKIE,
            value=token,
            httponly=True,
            samesite="lax",
            secure=settings.environment == "production",
            path=_CSRF_COOKIE_PATH,
        )
        return response
    context = {**context, "csrf_token": request.cookies.get(_CSRF_COOKIE, "")}
    return templates.TemplateResponse(request=request, name=name, context=context, **kwargs)


def _csrf_valid(request: Request, csrf_token: str | None) -> bool:
    """Return whether an optional CSRF token matches the cookie.

    A missing cookie is rejected: accepting it would let a cross-site POST
    skip the token check entirely.
    """
    expected = request.cookies.get(_CSRF_COOKIE)
    if expected is None:
        return False
    provided = csrf_token or ""
    return bool(provided) and secrets.compare_digest(provided, expected)


#: Email-only deletion requests accepted per address and per IP each day.
_QUEUE_LIMIT_PER_DAY = 3
_QUEUE_WINDOW_SECONDS = 24 * 60 * 60


async def _queue_quota_exceeded(redis_client: RedisClient, email: str, ip: str) -> bool:
    """Count an email-only request and say whether the daily cap is exceeded.

    Without a cap anyone could flood ``deletion_requests`` (and the operator's
    inbox) with arbitrary addresses.
    """
    exceeded = False
    for key in (f"deletion_queue:email:{email}", f"deletion_queue:ip:{ip}"):
        count = await redis_client.incrby(key)
        if count == 1:
            await redis_client.expire(key, _QUEUE_WINDOW_SECONDS)
        exceeded = exceeded or count > _QUEUE_LIMIT_PER_DAY
    return exceeded


def _form_error(request: Request, message: str, status_code: int = 400) -> HTMLResponse:
    """Re-render the deletion form with an error, keeping the CSRF token."""
    return templates.TemplateResponse(
        request=request,
        name="public/account_deletion.html",
        context={
            "error": message,
            "message": None,
            "csrf_token": request.cookies.get(_CSRF_COOKIE, ""),
        },
        status_code=status_code,
    )


@router.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request) -> HTMLResponse:
    """Render the privacy policy with the live third-party-processor list."""
    settings = get_settings()
    processors = [
        {
            "name": provider.name,
            "base_url": provider.base_url,
            "purpose": "Envio do texto das conversas para completar suas respostas (assistente de IA).",
        }
        for provider in settings.ai_providers
    ]
    context = {
        "processors": processors,
        "conversation_retention_days": settings.conversation_history_ttl_seconds // (24 * 60 * 60),
        "ai_logs_retention_days": settings.ai_logs_retention_days,
    }
    return _render_public(request, "public/privacy.html", context)


@router.get("/terms", response_class=HTMLResponse)
async def terms_page(request: Request) -> HTMLResponse:
    """Render the terms-of-service document."""
    return _render_public(request, "public/terms.html", {"app_name": get_settings().app_name})


@router.get("/account-deletion", response_class=HTMLResponse)
async def account_deletion_page(
    request: Request,
    error: str | None = None,
    message: str | None = None,
) -> HTMLResponse:
    """Render the account-deletion form."""
    return _render_public(
        request,
        "public/account_deletion.html",
        {"error": error, "message": message},
    )


@router.post("/account-deletion/request", response_class=HTMLResponse)
async def account_deletion_request(
    request: Request,
    email: str = Form(...),
    password: str = Form(""),
    csrf_token: str = Form(""),
    session: AsyncSession = Depends(get_session),
    redis_client: RedisClient = Depends(get_redis_client),
) -> HTMLResponse:
    """Process the deletion request form.

    With both ``email`` and a matching ``password`` the account is wiped
    immediately (identity proof).  Wrong credentials stay on the form with a
    400 message — never a redirect that could drop the user's typed input.

    With ``email`` only, a pending :class:`DeletionRequest` is queued so an
    operator can follow up out-of-band.
    """
    if not _csrf_valid(request, csrf_token):
        raise HTTPException(status_code=403, detail="Token CSRF inválido.")

    email = email.strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="E-mail é obrigatório.")

    client_ip = get_client_ip(request)

    if password:
        # Same per-(email, IP) lockout as the login: without it this form is a
        # password-guessing oracle whose prize is deleting the victim's account.
        limiter = LoginRateLimiter(redis_client)
        if await limiter.is_locked(email, client_ip):
            return _form_error(
                request,
                "Muitas tentativas. Tente novamente mais tarde.",
                status_code=429,
            )

        user = await UserRepository(session).get_by_email(email)
        if user is None:
            spend_password_check(password)

        if user is None or not _password_matches(password, user.password_hash):
            await limiter.on_failure(email, client_ip)
            return _form_error(
                request,
                "Credenciais inválidas: não foi possível confirmar sua identidade.",
            )
        await limiter.on_success(email, client_ip)

        # Capture the id now: the rollback below expires the ORM instance and
        # a later attribute access would need a lazy async refresh.
        user_id = user.id

        # The SELECT above auto-began a transaction; release it so the
        # deletion service can own its own ``session.begin()`` (see
        # :func:`app.api.deps._release_read_transaction`).
        await session.rollback()

        # Identity proved — hard-delete through the same service the API uses.
        result = await AccountDeletionService(session, redis_client).delete_account(
            user_id, password
        )
        logger.info("Account deleted via public page", extra={"user_id": user_id})
        message = "Sua conta e todos os seus dados foram removidos permanentemente."
        if result.get("subscription_management_url"):
            message += (
                " Você ainda tem uma assinatura ativa: cancele-a em "
                + result["subscription_management_url"]
                + " para encerrar cobranças."
            )
        return templates.TemplateResponse(
            request=request,
            name="public/account_deletion_confirmation.html",
            context={"message": message},
        )

    # No password: queue the request for manual handling (rate-capped).
    if await _queue_quota_exceeded(redis_client, email, client_ip):
        return _form_error(
            request,
            "Recebemos muitas solicitações recentes. Tente novamente amanhã.",
            status_code=429,
        )
    session.add(DeletionRequest(email=email))
    await session.commit()
    logger.info("Deletion request queued")
    return templates.TemplateResponse(
        request=request,
        name="public/account_deletion_confirmation.html",
        context={
            "message": (
                "Recebemos sua solicitação de exclusão de conta. "
                "Nossa equipe entrará em contato pelo e-mail informado para "
                "confirmar e concluir a exclusão em até 30 dias."
            )
        },
    )
