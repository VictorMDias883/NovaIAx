# NovaIAx — Technical Project Summary

> Read-only analysis of the repository at the current `main` working tree.
> Every claim below cites the file (and, where useful, the line) it came from.
> Where a statement is an inference rather than a literal fact in the code, it is
> marked as such.

---

## 1. Project purpose and what it does

NovaIAx (`pyproject.toml:6-8`, name `novaiax-gateway`, version `0.1.0`) is a **Python/FastAPI
API Gateway** that sits between a mobile client and a set of downstream services, and it is
also the backend for a first-party set of AI features.

Concretely, it provides:

1. **Cross-cutting gateway concerns** — JWT authentication, RBAC, per-IP rate limiting, Redis
   caching, structured JSON logging, security headers, CORS, payload size limits, and a
   generic reverse proxy (`app/main.py:11-13`, `app/middlewares/`, `app/api/v1/proxy.py`).
2. **A goal/objective ("meta") product built on AI** (`app/services/objective_service.py`).
   A user registers a long-term goal; the backend asks Groq Cloud to generate a **7-day roadmap
   window** decomposed into one *meta básica* per calendar day. Each day is persisted as a
   `RoadmapDay` row that the owner (or an admin) can mark as fulfilled / pending / skipped or
   delete. Every 7 days the roadmap window can be renewed, feeding the previous window back to
   the model for continuity.
3. **Three conversational AI endpoints**:
   - `POST /api/v1/objectives/assistant` — a multi-turn assistant that collects goal data and
     **auto-registers the objective** when the model emits a complete JSON payload
     (`app/services/objective_assistant_service.py:54-116`).
   - `POST /api/v1/agents/general/chat` — a general assistant that is given the user's
     objectives and their roadmap-day statuses as context and can report progress
     (`app/services/general_agent_service.py:56-108`).
   - `POST /api/v1/ai/chat` — a stateless completion bound to an admin-managed system prompt
     stored in the database, with a per-user response cache
     (`app/services/chat_completion_service.py:40-66`).
4. **A server-rendered admin panel** (Jinja2 HTML + httpOnly cookie + CSRF) for user role
   management and system-prompt CRUD (`app/api/admin.py`, `app/templates/admin/`).

The target client is a **Flutter mobile app** (`API_REFERENCE.md:3`, `DEPLOY.md:252-257`).
No Flutter/Dart source is present in this repository — this is backend-only.

**Current measured state** (verified in this working tree): `ruff check .` → *All checks
passed*; `mypy app` → *Success: no issues found in 81 source files*; `pytest` → **142 tests
passing, 0 failures** (run with an isolated SQLite DB path so the repo's `test.db` was not
touched).

---

## 2. Stack, versions and main dependencies

Declared in two places that must be kept in sync: `pyproject.toml:11-27` (canonical) and
`requirements.txt` (used by the Docker build, `Dockerfile:19`). `requirements.txt` additionally
lists `pytest`/`pytest-asyncio` in the runtime requirements (`requirements.txt:16-17`), which
are declared as a `dev` extra in `pyproject.toml:29-35`.

| Layer | Package | Constraint (declared) | Installed in `.venv` |
| --- | --- | --- | --- |
| Web framework | `fastapi` | `>=0.111.0` | 0.141.1 |
| ASGI server | `uvicorn[standard]` | `>=0.30.0` | — |
| Validation / config | `pydantic` | `>=2.7.0` | 2.13.4 |
| Config | `pydantic-settings` | `>=2.3.0` | — |
| ORM | `sqlalchemy` | `>=2.0.0` | 2.0.51 |
| Migrations | `alembic` | `>=1.13.0` | 1.20.0 |
| Postgres driver | `asyncpg` | `>=0.27.0` | 0.31.0 |
| SQLite driver (tests) | `aiosqlite` | `>=0.10.0` | 0.22.1 |
| Cache | `redis` | `>=5.0.0` | — |
| HTTP client | `httpx` | `>=0.27.0` | — |
| JWT | `python-jose[cryptography]` | `>=3.3.0` | — |
| Password hashing | `passlib[bcrypt]` | `>=1.7.0` | — |
| Email validation | `email-validator` | `>=2.0.0` | — |
| Templating (admin panel) | `jinja2` | `>=3.1.0` | — |
| Form parsing (admin panel) | `python-multipart` | `>=0.0.9` | — |
| Tests | `pytest`, `pytest-asyncio` | `>=8.0.0`, `>=0.23.0` | dev extra |
| Lint / types | `ruff`, `mypy` | `>=0.6.0`, `>=1.11.0` | dev extra |

- **Python**: `requires-python = ">=3.12"` (`pyproject.toml:10`); Docker base image is
  `python:3.12-slim` (`Dockerfile:11`); the local `.venv` is Python **3.14**, so the developer
  environment is ahead of the declared/deployed floor.
- **Tooling config**: `pytest` with `testpaths=["tests"]`, `asyncio_mode="auto"`,
  `addopts="-q"` (`pyproject.toml:40-43`); `ruff` targeting `py312`, line length 120, rules
  `E,F,W,I,UP,B,SIM` with 4 documented ignores (`pyproject.toml:45-61`); `mypy` with the
  pydantic plugin and `check_untyped_defs` (`pyproject.toml:63-70`).
- **Locking**: every dependency is a lower bound only. There is **no** lock file
  (`requirements.txt` has no hashes, no `uv.lock`, no `poetry.lock`), so builds are not
  reproducible.

---

## 3. Architecture and patterns used

### 3.1 Layering

The codebase follows a strict, consistently-applied vertical slice
(`app/api/v1/objective_router.py:15-16` states it explicitly as
`Router → Command → Service → Repository → Database`):

```
HTTP request
  └─ Middleware stack (outermost → innermost)
       RateLimit → Auth → Logging → SecurityHeaders → CORS
  └─ Route handler (app/api/v1/*, app/api/admin.py)
       ├─ Depends(...) DI guards: get_session, get_redis_client,
       │   get_current_user / require_db_user / require_admin_db_role
       ├─ Pydantic request schema (app/schemas/*)  → HTTP 422
       ├─ Command dataclass (app/commands/*)        → ValueError → HTTP 422
       ├─ Service (app/services/*)                  → HTTPException
       ├─ Repository (app/repositories/*)           → commits its own transaction
       └─ SQLAlchemy ORM (app/models/*) → DB
```

Middleware execution order is the reverse of registration (`app/main.py:120-145`), and the
reasoning is documented in the comment at `app/main.py:121-133`.

### 3.2 Patterns actually in use

| Pattern | Where | Notes |
| --- | --- | --- |
| Layered/hexagonal-ish | whole `app/` | Router→Command→Service→Repository is applied without exception. |
| Command objects as frozen dataclasses | `app/commands/*.py` | All 11 are `@dataclass(frozen=True)`; they are DTOs, not behavioural objects (no handlers). |
| DI via `Depends` | `app/api/deps.py` | `get_session`, `get_redis_client`, `get_api_key_service`, `get_token_denylist`, `get_settings_dep`. |
| Service singletons per request | `app/api/deps.py:39-74` | Redis client is **process-wide** (`_persistent_redis_client`), deliberately, so conversation state survives between turns. |
| Settings singleton | `app/core/config.py:366-382` | Cached module-level `_settings`; first call runs 3 validators. |
| Repository that owns its transaction | `app/repositories/*.py` | Every repository write method calls `await self.session.commit()` internally. |
| Service-owned transaction | `app/services/user_service.py`, `app/services/auth_service.py:70`, `app/db/bootstrap.py:57` | `async with self.session.begin():` — the *opposite* convention from the repositories. See §9.4. |
| Strategy / ABC for the AI provider | `app/clients/ai_client.py:254-276` | `AIClient` ABC with two abstract coroutines; `GroqAIClient` is the only implementation. |
| Decorator/registry singletons | `app/cache/token_denylist.py:59-70`, `app/cache/redis_client.py:176-180` | `get_token_denylist()`, `RedisClient.reset_all_memory_stores()`. |
| Graceful degradation | `app/cache/redis_client.py:186-243` | Redis silently falls back to a per-instance `InMemoryStore`, with a 30 s re-probe cooldown. |
| Resilient HTTP client | `app/clients/ai_client.py:336-503` | Process-wide sliding-window pacer + concurrency semaphore + correlation-id logging + 429 handling. |
| Global exception handlers | `app/exceptions/handlers.py:40-79` | 3 handlers: `RequestValidationError`→422, `HTTPException`→passthrough, `Exception`→500. |
| Custom exception for a control-flow redirect | `app/api/admin.py:67-97` | `AdminLoginRedirect` + `register_admin_exception_handler`, needed because the global `HTTPException` handler serialises to JSON and drops headers. |
| Declarative migrations | `migrations/versions/99a87c4cfb97_initial_schema.py` | Single base revision; app never calls `create_all()` in production. |
| Server-rendered HTML | `app/api/admin.py` + `app/templates/admin/*.html` | POST/redirect/GET (PRG) pattern, flash via query string. |
| Anti-CSRF double-submit | `app/api/admin.py:109-154` | `novaiax_csrf` cookie + hidden `csrf_token` form field, `secrets.compare_digest`. |

### 3.3 Annotated folder tree

```
NovaIAx/
├── app/
│   ├── main.py                       # Entry point: FastAPI factory, middleware order,
│   │                                 #   router mounting, lifespan, GET /health
│   ├── api/
│   │   ├── deps.py                   # DI providers + auth guards (get_current_user,
│   │   │                             #   require_db_user, require_admin_db_role)
│   │   ├── admin.py                  # Server-rendered admin panel (Jinja2 + cookie + CSRF);
│   │   │                             #   presentation layer only, reuses the services
│   │   └── v1/
│   │       ├── router.py             # Central hub: includes all 10 v1 sub-routers
│   │       ├── auth.py               # /auth: register, login, refresh, logout, me
│   │       ├── user_router.py        # /users: self-service + admin role management
│   │       ├── objective_router.py   # /objectives: register, list, roadmap renew
│   │       ├── objective_assistant_router.py  # /objectives/assistant (AI goal collection)
│   │       ├── roadmap_day_router.py # /objectives/{id}/roadmap/days CRUD (day-level)
│   │       ├── general_agent_router.py# /agents/general/chat + conversation reset
│   │       ├── chat_router.py        # /ai/chat (system-prompt-backed completion)
│   │       ├── system_prompt_router.py# /system-prompts CRUD (read=user, write=admin)
│   │       ├── cache_router.py       # /cache/flush (per-user cache purge)
│   │       └── proxy.py              # /proxy/{service}/... reverse proxy + response cache
│   ├── cache/
│   │   ├── redis_client.py           # RedisClient + InMemoryStore fallback (silent degrade)
│   │   ├── conversation_cache.py     # Per-(agent,user) JSON message history with trimming
│   │   └── token_denylist.py         # Revoked-JWT `jti` store (TTL == remaining lifetime)
│   ├── clients/
│   │   └── ai_client.py              # AIClient ABC, GroqAIClient, _GroqPacer, shared httpx
│   ├── commands/                     # 11 frozen dataclasses (router ↔ service DTOs)
│   ├── core/
│   │   ├── config.py                 # Settings singleton, prod secret validation, DB URL norm
│   │   ├── security.py               # JWT create/decode, pwd_context, ApiKeyService
│   │   ├── logging.py                # JsonFormatter + configure_logging (stdout JSON lines)
│   │   ├── health.py                 # check_database (SELECT 1), check_redis (fresh ping)
│   │   └── network.py                # get_client_ip (X-Forwarded-For only when trusted)
│   ├── db/
│   │   ├── session.py                # AsyncEngine, SessionLocal, Base, init_db (tests only)
│   │   └── bootstrap.py              # ensure_default_admin() (idempotent, runs in lifespan)
│   ├── exceptions/handlers.py        # Global JSON error handlers
│   ├── middlewares/
│   │   ├── rate_limit_middleware.py  # Redis sliding window per client IP
│   │   ├── auth_middleware.py        # Central auth gate with a public-path allowlist
│   │   ├── logging_middleware.py     # Per-request JSON access log (method/path/status/ms/IP)
│   │   └── security_headers_middleware.py # nosniff, DENY, HSTS, CSP, COOP/CORP…
│   ├── models/                       # SQLAlchemy 2.0 mapped classes + enums
│   │   ├── user.py                   # User + UserRole(USER|ADMIN|SERVICE)
│   │   ├── objective.py              # Objective (goal + roadmap markdown)
│   │   ├── roadmap_day.py            # RoadmapDay + RoadmapDayStatus
│   │   └── system_prompt.py          # SystemPrompt
│   ├── repositories/                 # 4 data-access classes, each commits its own writes
│   ├── schemas/                      # 9 Pydantic request/response modules
│   │   └── password_rules.py         # PasswordRulesMixin (digit + uppercase)
│   ├── services/                     # 9 business-logic modules (incl. cache flush)
│   ├── static/admin/admin.css        # Panel stylesheet (served at /admin/static)
│   └── templates/admin/*.html        # 6 Jinja2 templates (base, login, dashboard,
│                                     #   users, prompts, prompt_form)
├── migrations/
│   ├── env.py                        # Async Alembic env; URL injected from Settings
│   ├── script.py.mako
│   └── versions/99a87c4cfb97_initial_schema.py  # The only migration
├── tests/                            # 19 test modules (~4.3k lines) + helpers.py
├── conftest.py                       # 2 autouse fixtures (test DB, Redis memory reset)
├── pyproject.toml / requirements.txt # Dependencies + tool config
├── alembic.ini                       # No sqlalchemy.url — injected by migrations/env.py
├── entrypoint.sh                     # `alembic upgrade head` then exec uvicorn
├── Dockerfile                        # Single-stage python:3.12-slim
├── docker-compose.yml                # Full stack: postgres + redis + api
├── docker-compose.data.yml           # Data services only
├── docker-compose.dev.yml            # API only
├── render.yaml                       # Render Blueprint: web svc + managed Postgres
├── .github/workflows/ci.yml          # 3 jobs: lint, test, migrate (Postgres 16)
├── .env.example                      # 26 documented env vars
├── README.md / API_REFERENCE.md / DEPLOY.md / PROJECT_STATUS.md  # Docs (mostly PT-BR)
├── Infra/project.wsd                 # PlantUML architecture sketch
└── PROJECT_SUMMARY.md                # This file
```

---

## 4. Main end-to-end flow

### 4.1 Every request (universal path)

1. **Boot** (`app/main.py:61-108`): `configure_logging()` → `get_settings()` (runs
   `_validate_admin_password`, `_validate_master_api_key_visibility`,
   `_validate_production_secrets`) → app created → middlewares registered → routers mounted at
   `/api/v1` (`app/main.py:152`), admin router unprefixed (`:157`), static mount (`:160`) →
   exception handlers registered (`:167-170`).
2. **Lifespan start** (`app/main.py:85-108`): `ensure_default_admin()` — takes a table lock,
   and either creates the first admin, re-syncs `ADMIN_DEFAULT_PASSWORD`, or no-ops.
3. **Per request**: `RateLimitMiddleware` prunes a 60 s Redis sorted set and enforces 60/min
   (or 10/min on AI paths) keyed by resolved client IP
   (`app/middlewares/rate_limit_middleware.py:88-137`) → `AuthMiddleware` resolves the identity
   unless the path matches the public allowlist (`app/middlewares/auth_middleware.py:43-58`) →
   `LoggingMiddleware` times and logs the response → `SecurityHeadersMiddleware` injects headers
   → CORS → handler.
4. **Auth resolution** (`app/api/deps.py:127-215`): try `Authorization: Bearer`; decode JWT,
   require `type == "access"`, reject a denylisted `jti`, load the user row, build the identity
   dict. Else try `X-API-Key` → `{"id": "api-key", role: "SERVICE"}`. Else 401.
5. **Session release** (`app/api/deps.py:99-124`): because a read autobegins a transaction,
   the auth dependency rolls it back so a downstream `async with session.begin():` can work.

### 4.2 The flagship business flow — create an objective with an AI roadmap

`POST /api/v1/objectives/register` → `app/api/v1/objective_router.py:62-97`

1. `RegisterObjectiveRequest` validates `title` (2–255), `description` (≤1000), `due_date`
   (`app/schemas/objective_schemas.py:16-29`).
2. `require_db_user` rejects API-key identities with 403 (`app/api/deps.py:225-236`).
3. `ObjectiveService(session, ai_client=GroqAIClient())` is constructed — **the AI client is
   created unconditionally**, so this endpoint always makes a provider call
   (`app/api/v1/objective_router.py:90`).
4. `ObjectiveService.register` (`app/services/objective_service.py:70-122`):
   - `_ensure_aware(due_date)`; reject the past with `400 "due_date cannot be in the past"`.
   - `period_start = now`, `period_end = min(now + 7d, due_date)`.
   - `_generate_roadmap(...)` → hardcoded PT-BR prompt (`:353-366`) → `GroqAIClient` → parse the
     model's JSON; on parse failure it **falls back to storing the raw text** as the roadmap
     (`:344-346`, `:397`).
   - `ObjectiveRepository.create(...)` → **commit #1**.
   - `_create_roadmap_days(...)` → `RoadmapDayService.create_days` → `create_many` → **commit #2**.
5. `ObjectiveResponse(**result)` serialises the objective plus its days.

### 4.3 The AI-request path (shared by all four AI features)

`GroqAIClient._request` (`app/clients/ai_client.py:336-503`):
`_get_httpx_client()` → `_estimate_payload_tokens()` → `await pacer.acquire(tokens)` (shared
60 s sliding window, 20 RPM / 6K TPM defaults) → `await semaphore.acquire()` (concurrency 2) →
`POST {GROQ_API_BASE_URL}/chat/completions` with `Authorization: Bearer <GROQ_API_KEY>` →
on 429 log `x-ratelimit-*` and raise `HTTPException(429)` with a `Retry-After` header (no retry
by default, `GROQ_MAX_ATTEMPTS=1`) → on 401/other error raise 502 → on timeout raise 504 →
validate `choices[0].message.content` is a non-empty string → `release()` and return
`.strip()`.

### 4.4 The cache-flush flow

`POST /api/v1/cache/flush` → `app/api/v1/cache/flush` → `flush_user_cache`
(`app/services/cache_flush_service.py:26-58`) deletes, via `KEYS` glob:
`conversation:*:<user_id>`, `ai_response:*:<user_id>:*`, and every member of the
`proxy_cache:index:<user_id>` set (then the set itself). Returns per-category counts.

---

## 5. Endpoints

**Auth legend** — `public`: no credentials; `user`: valid JWT **or** `X-API-Key`; `db-user`:
real database user (API-key identity → 403); `admin`: `require_admin_db_role` (live DB role
re-read, API-key → 403). Note `AuthMiddleware` is the outer gate; the `Depends` guard is what
actually distinguishes the four levels.

Counts below were obtained by introspecting the live `app` object, not from the docs:
**50 registered routes** = 45 application routes + 5 framework routes (`/docs`,
`/docs/oauth2-redirect`, `/openapi.json`, `/redoc`, `/health`).

### 5.1 `app/api/v1/auth.py` — prefix `/api/v1/auth` (tags `auth`)

| Method | Route | Handler | Auth | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| POST | `/api/v1/auth/register` | `register_user` (`:35`) | public | 201 | First registrant becomes ADMIN; 409 on duplicate email; returns `{user, access_token, refresh_token}` |
| POST | `/api/v1/auth/login` | `login` (`:66`) | public | 200 | 401 `"Invalid credentials"` on bad email **or** password |
| POST | `/api/v1/auth/refresh` | `refresh_tokens` (`:92`) | public | 200 | Rotates the pair; revokes the presented `jti` |
| POST | `/api/v1/auth/logout` | `logout` (`:114`) | public¹ | 200 | Denylists the refresh `jti` (+ access `jti` if supplied); returns `{"access_token":"", "refresh_token":""}` |
| GET | `/api/v1/auth/me` | `me` (`:145`) | db-user | 200 | Identity straight from the validated token |

¹ Middleware-public, but a *valid refresh token* must be in the body.

### 5.2 `app/api/v1/user_router.py` — prefix `/api/v1/users` (tags `users`)

| Method | Route | Handler | Auth | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| PATCH | `/api/v1/users/me` | `update_my_profile` (`:46`) | db-user | 200 | E-mail deliberately not editable |
| PATCH | `/api/v1/users/me/password` | `change_my_password` (`:76`) | db-user | 200 | 400 on wrong current password; bumps `refresh_token_valid_after` |
| GET | `/api/v1/users/` | `list_users` (`:151`) | admin | 200 | `page≥1`, `limit 1..100` |
| PATCH | `/api/v1/users/{user_id}/promote` | `promote_user` (`:118`) | admin | 200 | 400 if demoting the last admin |
| PATCH | `/api/v1/users/{user_id}/demote` | `demote_user` (`:129`) | admin | 200 | |
| DELETE | `/api/v1/users/{user_id}` | `delete_user` (`:140`) | admin | 200 | 400 `"Cannot delete the last administrator."` |

### 5.3 `app/api/v1/objective_router.py` — prefix `/api/v1/objectives` (tags `objectives`)

| Method | Route | Handler | Auth | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| GET | `/api/v1/objectives/` | `list_objectives` (`:32`) | db-user | 200 | `offset≥0`, `limit 1..100`; N+1 on days (§9.2) |
| POST | `/api/v1/objectives/register` | `register_objective` (`:62`) | db-user | 200 | 400 if `due_date` in the past; always calls Groq |
| POST | `/api/v1/objectives/{objective_id}/roadmap/renew` | `renew_objective_roadmap` (`:100`) | db-user | 200 | 403 non-owner, 409 before 7 days elapse, 400 `"Objective already expired"` |

### 5.4 `app/api/v1/roadmap_day_router.py` — prefix `/api/v1/objectives` (tags `objectives`)

| Method | Route | Handler | Auth | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| GET | `/api/v1/objectives/{objective_id}/roadmap/days` | `list_roadmap_days` (`:29`) | db-user | 200 | Owner or ADMIN |
| PATCH | `/api/v1/objectives/{objective_id}/roadmap/days/{day_id}` | `update_roadmap_day_status` (`:45`) | db-user | 200 | Sets/clears `completed_at` atomically with the status |
| DELETE | `/api/v1/objectives/{objective_id}/roadmap/days/{day_id}` | `delete_roadmap_day` (`:73`) | db-user | 204 | Guards against cross-objective day ids |

### 5.5 AI endpoints

| Method | Route | Handler | Auth | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| POST | `/api/v1/objectives/assistant` | `objective_assistant` (`:17`) | db-user | 200 | `""` path (no trailing slash). Clears the cached conversation on success |
| POST | `/api/v1/agents/general/chat` | `general_agent_chat` (`:27`) | db-user | 200 | Injects objectives + day statuses as context |
| DELETE | `/api/v1/agents/general/conversation` | `clear_general_agent_conversation` (`:56`) | db-user | 200 | Clears this user's general-agent history |
| POST | `/api/v1/ai/chat` | `chat_completion` (`:18`) | user | 200 | 404 `"Agent not found"`; per-user cache, TTL 300 s |

### 5.6 `app/api/v1/system_prompt_router.py` — prefix `/api/v1/system-prompts`

| Method | Route | Handler | Auth | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| GET | `/api/v1/system-prompts/` | `list_system_prompts` (`:19`) | **user** | 200 | Deliberately open to all authenticated identities (covered by `tests/test_system_prompt_read_access.py`) |
| POST | `/api/v1/system-prompts/` | `create_system_prompt` (`:31`) | admin | 201 | |
| GET | `/api/v1/system-prompts/{prompt_id}` | `get_system_prompt` (`:42`) | **user** | 200 | 404 `"System prompt not found"` |
| PUT | `/api/v1/system-prompts/{prompt_id}` | `update_system_prompt` (`:53`) | admin | 200 | |
| DELETE | `/api/v1/system-prompts/{prompt_id}` | `delete_system_prompt` (`:65`) | admin | 204 | |

### 5.7 `app/api/v1/cache_router.py` and `app/api/v1/proxy.py`

| Method | Route | Handler | Auth | Status | Notes |
| --- | --- | --- | --- | --- | --- |
| POST | `/api/v1/cache/flush` | `flush_cache` (`:22`) | db-user | 200 | `{"status":"ok","user_id":…,"cleared":{…}}` |
| GET/POST/PUT/PATCH/DELETE | `/api/v1/proxy/{service_name:path}` | `proxy_request` (`app/api/v1/proxy.py:135-140`) | user | mirrors downstream | 404 unknown service, 413 body > `MAX_PAYLOAD_BYTES`, 502 on `httpx.HTTPError` |

### 5.8 Framework / ops routes

| Method | Route | Handler | Auth | Notes |
| --- | --- | --- | --- | --- |
| GET | `/health` | `health` (`app/main.py:176`) | public, rate-limit-exempt | Real `SELECT 1` + fresh Redis ping; **200** `{"status":"ok","db":…,"redis":…}` or **503** |
| GET | `/docs`, `/openapi.json` | Swagger/ReDoc | **public** | Explicitly allowlisted (`app/middlewares/auth_middleware.py:46-47`) |
| GET | `/redoc` | ReDoc | **not allowlisted** → requires auth | Inconsistency vs `/docs` (see §9.7) |

### 5.9 `app/api/admin.py` — server-rendered admin panel (no prefix; all at `/admin`)

Every route except the two login routes depends on `require_admin_panel`
(`app/api/admin.py:157-206`), which decodes the httpOnly cookie JWT, requires
`type == "access"`, loads the user and requires the **live** DB role `ADMIN`, else raises
`AdminLoginRedirect` → HTTP 303 to `/admin/login`.

| Method | Route | Handler | Auth | Notes |
| --- | --- | --- | --- | --- |
| GET | `/admin/login` | `login_page` (`:214`) | public | Redirects to `/admin/` if already an admin |
| POST | `/admin/login` | `login_submit` (`:241`) | public | Form-encoded; 403 for non-admins; sets `novaiax_admin_session` cookie |
| POST | `/admin/logout` | `logout` (`:288`) | cookie | Deletes the cookie |
| GET | `/admin/` | `dashboard` (`:305`) | admin cookie | Counts users / admins / prompts |
| GET | `/admin/users` | `users_page` (`:339`) | admin cookie | Paginated, 25/page |
| POST | `/admin/users/{user_id}/promote` \| `/demote` \| `/delete` | `promote_user` (`:384`), `demote_user` (`:397`), `delete_user` (`:410`) | admin cookie | Errors flash via `?error=` |
| GET | `/admin/system-prompts` | `prompts_page` (`:430`) | admin cookie | List + create form |
| POST | `/admin/system-prompts` | `create_prompt` (`:467`) | admin cookie | Re-validates with `SystemPromptRequest` (`:455-464`) |
| GET | `/admin/system-prompts/{prompt_id}/edit` | `edit_prompt_page` (`:485`) | admin cookie | |
| POST | `/admin/system-prompts/{prompt_id}` | `update_prompt` (`:505`) | admin cookie | |
| POST | `/admin/system-prompts/{prompt_id}/delete` | `delete_prompt` (`:524`) | admin cookie | |
| GET | `/admin/static/*` | StaticFiles mount (`app/main.py:160`) | public | `app/static/admin/admin.css` |

---

## 6. Data models and entities

Four tables, created by the single migration `migrations/versions/99a87c4cfb97_initial_schema.py`
(2 native PostgreSQL enum types, 9 indexes, no seed data, no `ondelete` on either FK).

### 6.1 `users` — `app/models/user.py:67-89`

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| `id` | INTEGER | NOT NULL | — | PK, `index=True` |
| `full_name` | VARCHAR(255) | NOT NULL | — | |
| `email` | VARCHAR(255) | NOT NULL | — | `unique=True, index=True` → unique **index** `ix_users_email` |
| `password_hash` | VARCHAR(255) | NOT NULL | — | PBKDF2-SHA256 via passlib (`app/core/security.py:124`) |
| `role` | native PG enum `user_role` | NOT NULL | `server_default='USER'` | `UserRole` = `USER`/`ADMIN`/`SERVICE` |
| `refresh_token_valid_after` | TIMESTAMPTZ | NOT NULL | `server_default=now()` | Token-cutoff for password/role changes |
| `created_at` | TIMESTAMPTZ | NOT NULL | `server_default=now()` | |

`UserRole.SERVICE` is documented as **never persisted** — it exists only in the in-memory
identity dict for API keys (`app/models/user.py:30-36`, produced at `app/api/deps.py:212`).
Relationship: `objectives` with `cascade="all, delete-orphan"` (`app/models/user.py:89`).

### 6.2 `objectives` — `app/models/objective.py:48-70`

| Column | Type | Null | Default |
| --- | --- | --- | --- |
| `id` | INTEGER | NOT NULL | PK |
| `title` | VARCHAR(255) | NOT NULL | |
| `description` | VARCHAR(1000) | NULL | |
| `roadmap` | TEXT | NULL | AI markdown for the current window |
| `roadmap_updated_at` | TIMESTAMPTZ | NULL | 7-day renewal gate |
| `due_date` | TIMESTAMPTZ | NOT NULL | |
| `created_at` | TIMESTAMPTZ | NOT NULL | **Python** `default` only, no `server_default` |
| `user_id` | INTEGER | NOT NULL | FK → `users.id`, **not indexed** |

### 6.3 `roadmap_days` — `app/models/roadmap_day.py:56-77`

| Column | Type | Null | Default |
| --- | --- | --- | --- |
| `id` | INTEGER | NOT NULL | PK |
| `objective_id` | INTEGER | NOT NULL | FK → `objectives.id`, `index=True` |
| `day_number` | INTEGER | NOT NULL | no `CHECK >= 1` |
| `day_date` | TIMESTAMPTZ | NOT NULL | semantically a date; always written as midnight UTC (`app/services/roadmap_day_service.py:82`) |
| `status` | native PG enum `roadmap_day_status` | NOT NULL | `server_default='PENDING'`; `PENDING`/`COMPLETED`/`SKIPPED` |
| `content` | TEXT | NULL | the day's *meta básica* |
| `completed_at` | TIMESTAMPTZ | NULL | set/cleared with the status |
| `created_at` | TIMESTAMPTZ | NOT NULL | `server_default=now()` |

**No unique constraint** on `(objective_id, day_number)` or `(objective_id, day_date)`.

### 6.4 `system_prompts` — `app/models/system_prompt.py:21-28`

| Column | Type | Null | Default | Notes |
| --- | --- | --- | --- | --- |
| `id` | INTEGER | NOT NULL | PK | |
| `tipo` | VARCHAR(100) | NOT NULL | — | `index=True`, **not unique** |
| `system_prompt` | VARCHAR(5000) | NOT NULL | — | |
| `created_at` | TIMESTAMPTZ | NOT NULL | `server_default=now()` | no `updated_at` |

### 6.5 Redis keyspace (non-relational entities)

| Key pattern | Meaning | TTL | Source |
| --- | --- | --- | --- |
| `rate_limit:<ip>` (sorted set) | Sliding-window request timestamps | none (pruned) | `app/middlewares/rate_limit_middleware.py:95-106` |
| `token_denylist:<jti>` | Revoked token | remaining token lifetime | `app/cache/token_denylist.py:23,42` |
| `api_key:<8-char-prefix>` | PBKDF2 hash of a non-master API key | none | `app/core/security.py:171` |
| `conversation:<agent>:<user_id>` | JSON message history | none | `app/cache/conversation_cache.py:36,62` |
| `ai_response:<agent_id>:<user_id>:<sha256(msg)>` | Cached AI reply | `CACHE_TTL_AI` (300 s) | `app/services/chat_completion_service.py:53,63` |
| `cache:<path>:<sha256>` | Proxied GET/HEAD response (base64 body) | `CACHE_TTL_DEFAULT` (60 s) | `app/api/v1/proxy.py:75,256-260` |
| `proxy_cache:index:<user_id>` (set) | Index of that user's `cache:` keys | `CACHE_TTL_DEFAULT` | `app/api/v1/proxy.py:270` |

---

## 7. How it communicates with the other project(s)

There is **no client code in this repository**. Two integration surfaces exist, and both are
declared only by configuration/docs — neither host is hardcoded in code.

### 7.1 Flutter mobile app (the primary consumer)

| Aspect | Contract | Source |
| --- | --- | --- |
| Base URL | Not hardcoded. Deployed as Render web service `novaiax-gateway` (`render.yaml:36`), region `virginia`, plan `free`. Clients must raise their HTTP timeout to ≥60–90 s for cold starts. | `render.yaml:36-38`, `DEPLOY.md:252-257,307` |
| API root | `/api/v1` (JSON) | `app/main.py:152`, `API_REFERENCE.md:12` |
| Admin | `/admin` (server-rendered HTML) — **explicitly not used by the mobile app** | `API_REFERENCE.md:842` |
| Auth header | `Authorization: Bearer <access_token>` (primary) | `app/api/deps.py:178-179` |
| Alt. auth header | `X-API-Key: <key>` → `SERVICE` identity (service-to-service) | `app/api/deps.py:166,211` |
| Token TTLs | access 15 min, refresh 7 days | `app/core/config.py:80-81` |
| Claim set | `sub`, `type` (`access`\|`refresh`), `jti`, `iat`, `exp`, plus `email`/`role` on access tokens | `app/core/security.py:64-75,90-96` |
| Accepted claims | Only `type == "access"`; a denylisted `jti` → 401; API-key identity on a user-scoped route → **403** `"API-key authentication cannot be used on this endpoint"` | `app/api/deps.py:187-192`, `:234-235` |
| Key responses | `AuthResponse{user{id,full_name,email,role}, access_token, refresh_token}`; `ObjectiveResponse{id,title,description,roadmap,roadmap_updated_at,due_date,user_id,days[]}`; `RoadmapDayResponse{…,status,completed_at}`; `ActionResponse{message}`; list envelopes `{users/prompts, page, limit, total}` | `app/schemas/*.py` |
| Error envelope | `{"detail": "..."}`; validation errors are flattened to `{"detail":"Invalid request"}` (details logged, not returned) | `app/exceptions/handlers.py:59,68,79` |
| Rate-limit feedback | `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `Retry-After` on every response; 429 on exceed | `app/middlewares/rate_limit_middleware.py:119-121,134-136` |
| CORS | `ALLOWED_ORIGINS` (CSV or JSON); default `http://localhost:3000,http://127.0.0.1:3000`; `allow_credentials=True`, methods/headers `*` | `app/core/config.py:124-151`, `app/main.py:135-141` |
| Proxied-hit marker | `X-Cache: HIT` on a cache-served proxy response | `app/api/v1/proxy.py:130` |
| Not used by the app | `/health`, `/docs`, `/openapi.json`, `/admin/static/*`, and (per `API_REFERENCE.md:874`) some admin endpoints | `API_REFERENCE.md:868-880` |

### 7.2 Downstream services (reverse proxy)

- Configured as a JSON list of `{name, base_url, timeout_seconds, allowed_paths?}` in
  `DOWNSTREAM_SERVICES_JSON`; default
  `[{"name":"ai","base_url":"http://mock-ai-service:8001","timeout_seconds":5}]`
  (`app/core/config.py:49-57,210-215`; `.env.example:65`).
- Request: `GET/POST/PUT/PATCH/DELETE /api/v1/proxy/{service}/{sub/path...}` →
  `app/api/v1/proxy.py:224` builds `{base_url}/{rest}`.
- **Request headers sent downstream**: everything from the client **except** `host`,
  `authorization`, `cookie`, `x-api-key` (stripped, `proxy.py:208-212`), **plus** injected
  `X-Forwarded-For: <resolved client IP>` and `X-Gateway-User: <full_name|"anonymous">`
  (`proxy.py:217-218`).
- **Response**: downstream status + body are mirrored; `content-length`,
  `content-encoding`, `transfer-encoding` are dropped (`proxy.py:250-252`). Failures map to
  404 / 413 / 502.
- `allowed_paths` is declared on `DownstreamService` (`config.py:57`) but **is never enforced**
  anywhere in the code — noted in §10.
- The configured default target `mock-ai-service` does not exist in this repo; there is no
  implementation of it here.

### 7.3 Groq Cloud (outbound)

`POST {GROQ_API_BASE_URL}/chat/completions` (default `https://api.groq.com/openai/v1`),
`Authorization: Bearer {GROQ_API_KEY}`, body `{"model": …, "messages": [...], "max_tokens": …}`
(`app/clients/ai_client.py:324-334,366-371`). Model default `openai/gpt-oss-120b`. Incoming
`x-ratelimit-*` and `retry-after` headers are parsed and logged (`:155-175`).

---

## 8. How to run, configure and test

### 8.1 Environment variables

26 variables are documented in `.env.example`. The ones that actually change behaviour:

| Variable | Default | Effect |
| --- | --- | --- |
| `ENVIRONMENT` | `development` | `production` **hard-fails boot** unless `SECRET_KEY`, `MASTER_API_KEY`, `GROQ_API_KEY`, `DATABASE_URL`, `REDIS_URL` are all set to non-placeholder values (`config.py:326-356`) |
| `SECRET_KEY` | `change-me-in-production` | HS256 signing key |
| `ACCESS_TOKEN_TTL_MINUTES` / `REFRESH_TOKEN_TTL_DAYS` | 15 / 7 | Token lifetimes |
| `ADMIN_DEFAULT_EMAIL` / `ADMIN_DEFAULT_PASSWORD` | `admin@admin.com` / generated | Bootstrap admin; if the password is unset a random one is generated and **logged once** (`app/db/bootstrap.py:83-101`) |
| `MASTER_API_KEY` | unset | Any request carrying it is fully trusted (`app/core/security.py:167-169`) |
| `DATABASE_URL` | `postgresql+asyncpg://novaiax:novaiax@postgres:5432/novaiax` (dev) / required (prod) | Auto-normalised to the `asyncpg` scheme (`config.py:20-36,106-118`) |
| `REDIS_URL` | `redis://redis:6379/0` (dev) / required (prod) | |
| `RATE_LIMIT_DEFAULT` / `RATE_LIMIT_AI` | 60 / 10 | Per client IP, per minute |
| `TRUST_PROXY_HEADERS` | `false` (`true` on Render) | Gates `X-Forwarded-For` trust |
| `MAX_PAYLOAD_BYTES` | 1048576 | Proxy 413 threshold |
| `CACHE_TTL_DEFAULT` / `CACHE_TTL_AI` | 60 / 300 | |
| `GROQ_API_KEY` / `_BASE_URL` / `_MODEL` / `_TIMEOUT_SECONDS` | — / Groq / `openai/gpt-oss-120b` / 10 | |
| `GROQ_RATE_LIMIT_RPM` / `GROQ_TOKEN_BUDGET_PER_MINUTE` / `GROQ_MAX_CONCURRENCY` / `GROQ_MAX_ATTEMPTS` / `GROQ_MAX_OUTPUT_TOKENS` | 20 / 6000 / 2 / 1 / 1024 | Process-wide AI pacer (`config.py:241-247`) |
| `ALLOWED_ORIGINS` | `http://localhost:3000,http://127.0.0.1:3000` | CORS; CSV or JSON accepted |
| `DOWNSTREAM_SERVICES_JSON` | mock AI service | Reverse-proxy targets |
| `ADMIN_COOKIE_NAME` | `novaiax_admin_session` | Admin panel cookie name |

> Note: the local `.env` present in this working tree is **not** tracked by git
> (`.gitignore:20`) — `git ls-files .env` reports no match. It does, however, contain live
> `SECRET_KEY`, `MASTER_API_KEY`, `GROQ_API_KEY` and `ADMIN_DEFAULT_PASSWORD` values, and
> `MASTER_API_KEY` being set triggers a boot-time warning
> (`app/core/config.py:309-323`).

### 8.2 Run locally

```bash
cp .env.example .env                        # then edit
docker compose -f docker-compose.data.yml up -d   # optional: Postgres 17 + Redis 7
pip install -r requirements.txt
alembic upgrade head                        # REQUIRED — the app never creates tables
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```
(`README.md:96-113`; `app/main.py:75-84` states migrations must be applied before start.)

- Interactive docs: `http://localhost:8000/docs`.
- Full stack in one command: `docker compose up --build` (`docker-compose.yml`; the `api`
  service installs deps and runs `alembic upgrade head` on every boot).
- Production image: `Dockerfile` → `CMD ["/app/entrypoint.sh"]` → `alembic upgrade head` then
  `exec uvicorn app.main:app --host 0.0.0.0 --port 8000` (`entrypoint.sh:17-21`). `set -e`
  makes a failed migration abort the boot.

### 8.3 Test

```bash
pip install -e ".[dev]"
pytest            # 142 tests, all passing in this working tree
ruff check .      # passes
mypy app          # passes (81 source files)
```

- `conftest.py:31` forces `DATABASE_URL=sqlite+aiosqlite:///./test.db` via `os.environ.setdefault`,
  so tests always run on SQLite even though `.env` points at Postgres.
- Two autouse fixtures only: `_ensure_test_database` (session-scoped `Base.metadata.create_all`)
  and `_reset_redis_fallback` (function-scoped in-memory Redis reset) — `conftest.py:42-67`.
  There are **no** session/client/user fixtures and no per-test DB rollback, so state persists
  across a whole test session.
- CI (`.github/workflows/ci.yml`): job `lint` (ruff + mypy), job `test` (pytest), job `migrate`
  (`alembic upgrade head` against a throwaway `postgres:16-alpine`).

---

## 9. Weaknesses, likely bugs, duplicated code, technical debt, security risks

Ordered roughly by impact. Each item is grounded in a specific location.

### 9.1 Security

1. **Two DB sessions and two user lookups per protected request.** `AuthMiddleware` calls
   `get_current_user(request=request, api_key_service=...)` **without a session**
   (`app/middlewares/auth_middleware.py:86`), so `_load_user` takes the
   `else` branch and opens a *temporary* `SessionLocal()` (`app/api/deps.py:89-93`). The route's
   own `require_db_user` then loads the same user again on the request-scoped session. Every
   authenticated request therefore opens 2 sessions and issues 2 `SELECT`s for the same row.
2. **Master API key compared with `==`, not `secrets.compare_digest`**
   (`app/core/security.py:168`) — a timing side channel on a fully-trusted credential.
3. **No `IntegrityError` handling anywhere** (verified: zero matches in `app/`). Two concurrent
   registrations of the same e-mail both pass the pre-check at `auth_service.py:71-73`, then the
   unique index on `users.email` raises `IntegrityError` → generic **500** instead of the
   intended 409.
4. **`LoginRequest.password` has no `max_length`** (`app/schemas/auth.py:45`,
   `min_length=1` only). A ~1 MiB body (allowed by `MAX_PAYLOAD_BYTES`) forces a full
   PBKDF2-SHA256 verification on every attempt. `RegisterUserRequest.password` is correctly
   capped at 128 (`auth.py:33`).
5. **The CSRF check is effectively inert for browser CSRF.** The CSRF cookie is
   `SameSite=Lax` (`app/api/admin.py:133`) and `_validate_csrf` returns early when the cookie
   is absent (`admin.py:150-152`). A cross-site POST does not carry Lax cookies, so
   `expected is None` and validation is skipped; the real protection is the session cookie's
   own `SameSite=Lax` (`admin.py:281`). The token machinery only helps in same-site cases.
6. **Bootstrap re-asserts the admin password on every boot.** `ensure_default_admin` re-hashes
   `ADMIN_DEFAULT_PASSWORD` into the stored admin row whenever the hashes differ
   (`app/db/bootstrap.py:65-76`). `render.yaml:88-90` sets `DEFAULT_ADMIN_PASSWORD` as a
   long-lived secret, so a known admin password is permanently re-armed on each restart. The
   docstring's "logged exactly once" guarantee (`:33-35`) only covers the randomly-generated case.
7. **Public OpenAPI schema.** `/docs` and `/openapi.json` are explicitly allowlisted
   (`auth_middleware.py:46-47`), publishing the full route inventory, including which routes are
   admin-only, to unauthenticated callers. `/redoc` is *not* allowlisted — an inconsistency.
8. **First-admin election is unprotected on non-PostgreSQL.** `lock_users_table()` issues
   `LOCK TABLE` only when `dialect.name == "postgresql"`
   (`app/repositories/user_repository.py:112-116`). On SQLite (all tests, `conftest.py:31`)
   `exists_admin()` is unguarded, so two concurrent first registrations can both become ADMIN
   (`auth_service.py:74-76`). Likewise `with_for_update()` is silently ignored by aiosqlite, so
   the "concurrent saves cannot overwrite each other" guarantee in
   `UserService.update_profile`'s docstring (`user_service.py:121-122`) does not hold there.
9. **Every authenticated identity can read every system prompt** —
   `list_system_prompts`/`get_system_prompt` use `get_current_user`, not the admin guard
   (`system_prompt_router.py:23,45`). This is a deliberate, tested decision
   (`tests/test_system_prompt_read_access.py`), but it does mean `SERVICE` API keys and any
   user can read prompt text, which is the same text used as the AI system prompt.
10. **Narrow refresh-token replay window.** `refresh` rejects when
    `fromtimestamp(iat) < refresh_token_valid_after` (`auth_service.py:125`), while
    `change_password`/`promote`/`demote` set that field to `datetime.now(UTC)` *with sub-second
    precision* and `iat` is truncated to whole seconds (`security.py:68`). A token minted in the
    same wall-clock second as a password change therefore survives it. Window ≈ 1 second.
11. **No `aud`/`iss` claims and no key rotation.** A single symmetric `SECRET_KEY` signs every
    token for every user; there is no audience binding, so a token minted for one deployment is
    valid in any other sharing the key.
12. **Proxy: `allowed_paths` is unenforced** (§10.1) — if it is ever set expecting a restriction,
    it provides none.

### 9.2 Performance / database

13. **N+1 queries on two hot read paths.** `ObjectiveService.list_by_user` calls
    `day_repo.list_by_objective(obj.id)` inside a loop (`objective_service.py:143-145`) → up to
    1 + 100 = **101 queries** for a full page. Identical pattern in
    `GeneralAgentService._build_context` (`general_agent_service.py:136`) inside the
    conversational path, so it repeats on **every chat turn**.
14. **`RoadmapDayRepository.create_many` issues one `SELECT` per row** — `await
    self.session.refresh(day)` inside a `for` loop (`roadmap_day_repository.py:69-70`) — on every
    objective registration and every renewal.
15. **No index on `objectives.user_id`** (`app/models/objective.py:57`), yet every
    `GET /objectives/` filters and orders by it. Only `ix_objectives_id` exists
    (migration `:57`).
16. **`index=True` on every primary key** produces four redundant indexes
    (`ix_users_id`, `ix_objectives_id`, `ix_roadmap_days_id`, `ix_system_prompts_id`) on top of
    the PK indexes that PostgreSQL already creates.
17. **No connection-pool tuning at all.** `create_async_engine(_database_url, echo=False)`
    (`app/db/session.py:34`) — no `pool_pre_ping`, no `pool_recycle`, no `pool_size`/
    `max_overflow`, no statement timeout. Defaults are `AsyncAdaptedQueuePool` (size 5, overflow
    10). Render's free-tier Postgres drops idle connections, so a stale-connection error on the
    first request after idle is a realistic failure mode here.
18. **Blocking `KEYS` on a user-facing request path.** `flush_user_cache` calls `keys()` twice
    (`cache_flush_service.py:38,43`) and then deletes keys **one round-trip at a time**
    (`:39-40,44-45,50-51`). `RedisClient.keys`' own docstring says it is "not for hot request
    paths" (`redis_client.py:258-262`), but this is a user-invocable path. A pipelined
    `DEL k1 k2 k3` / `UNLINK` would be one call.
19. **The AI call happens before the DB write** in both `register` and `renew_roadmap`
    (`objective_service.py:105-121`, `:224-239`). A DB failure after a successful paid call
    wastes the call and discards the model output.

### 9.3 Correctness bugs

20. **`InMemoryStore` does not enforce TTLs.** `set(..., ex=...)` accepts and discards `ex`
    (`redis_client.py:79-81`) and `expire` is an explicit no-op (`:98-99`). In any environment
    where Redis is unreachable, denylist entries, cached AI responses and proxy entries
    **never expire**.
21. **The cache flush silently under-reports on Redis failure.** `RedisClient._run` swallows any
    exception from a live client and transparently re-runs against the in-memory store
    (`redis_client.py:235-243`). `flush_user_cache` has no `try/except`, so a Redis outage
    returns `{"conversations_cleared": 0, …}` and the endpoint answers `{"status": "ok"}`
    (`cache_router.py:44`) while the user's real data is untouched. A GDPR-style "delete my
    data" request silently succeeds without deleting.
22. **Proxy cache entries can become permanently unflushable.** The index set
    `proxy_cache:index:<user_id>` gets `expire(..., cache_ttl_default)` (`proxy.py:270`) while
    the `cache:` entries it points at were written *first* (`:256-260`) with the same TTL. A key
    that lands in the set and then outlives it can never be found by
    `cache_flush_service.py:49-52`.
23. **No cache invalidation when a system prompt is updated.** The AI cache key is
    `ai_response:{agent_id}:{user_id}:{sha256(msg)}` (`chat_completion_service.py:53`) — it keys
    on the prompt's **row id**, not its content. After `PUT /system-prompts/{id}` the old
    answers keep being served for up to `CACHE_TTL_AI` (300 s). No layer knows about the cache.
24. **AI responses can be cross-tenant on the shared sentinel id.** For API-key callers
    `current_user["id"]` is the literal string `"api-key"` (`app/api/deps.py:212`), and
    `ChatCompletionService.create_completion` accepts `int | str`
    (`chat_completion_service.py:40`). All API keys therefore share one cache bucket. The proxy
    path explicitly avoided this by mixing the raw key into the identity (`proxy.py:79-90`); the
    chat path did not.
25. **Unhandled `int()` on untrusted data → 500 instead of 401.** `auth_service.refresh`/
    `logout` call `int(user_id)`, `int(issued_at)`, `int(exp)` on JWT claim values with no guard
    (`:114,120,125,133,174,186`). A token with a non-numeric `sub` or `exp` raises an unhandled
    `ValueError` → 500.
26. **Unguarded `int()` on AI output → 500.** `_generate_roadmap` does
    `metas[int(item["day"])] = meta` (`objective_service.py:391`) after checking only
    `isinstance(item, dict)` and `"day" in item` (`:387`). A model reply with
    `{"day": "segunda"}` raises out of the service as a 500.
27. **Silent loss of out-of-range roadmap day metadata.** Day numbers outside `1..total` are
    stored in `metas` and rendered into the markdown (`objective_service.py:391-392`) but are
    never looked up when creating rows (`roadmap_day_service.py:83`), so they are dropped from
    `roadmap_days` with no warning.
28. **Objective assistant swallows the most important failure.** `except (KeyError, TypeError,
    ValueError): title = description = ""` (`objective_assistant_service.py:95-96`) catches an
    unparseable due date (e.g. `"31/12/2026"` from the model) and falls through to the ordinary
    "not complete" branch, saving the conversation and returning **200 OK** with the assistant's
    text. The user believes the goal was created. No log is emitted.
29. **`int()` failures from the `"api-key"` sentinel are prevented by `require_db_user`**, not by
    design of the services — any new route that forgets the guard will 500 on
    `int(current_user["id"])`. This is noted as fragile rather than as a present bug.
30. **`result_days` is dead code** in `RoadmapDayService.create_days` — declared at `:78`,
    appended to at `:94`, never read (the function returns `merged`). Evidence of an unfinished
    refactor.
31. **Docstring contradicts code in `delete_day`**: `roadmap_day_service.py:207-209` says
    deleted days are not regenerated, but `create_days:95-105` recreates any missing calendar
    date in the window, so a deleted day inside a still-valid window comes back on renewal.
32. **`day_number` is never updated for reused rows** (`roadmap_day_service.py:84-94`), so a
    re-generated window exposes `day_number` values that no longer match the window's numbering
    (ordering survives only because the repo orders by `(day_date, day_number)`).
33. **Context silently truncated in the general agent.** `list_by_user(user_id, 0, 100)` orders
    by `Objective.id ASC` (`general_agent_service.py:76`, `objective_repository.py:83`) — the
    **oldest** 100, with no indication of truncation — while the prompt instructs the model to
    answer *only* from that block (`:85-86`). A user asking about objective #101 is told
    authoritatively that it does not exist.

### 9.4 Concurrency and transactions

34. **Two transaction conventions collide.** Services open `async with session.begin():`
    (`user_service.py`, `auth_service.py:70`) while repositories call bare
    `await session.commit()` (`objective_repository.py:65,102`;
    `roadmap_day_repository.py:54,68,117,135`; `system_prompt_repository.py:41,53,59`).
    The `_release_read_transaction` shim (`app/api/deps.py:99-124`) exists purely to work around
    the resulting "A transaction is already begun on this Session" error, and its own docstring
    warns that the rollback **expires the ORM instance** so later access raises
    `MissingGreenlet`. This is a latent footgun spread across four files.
35. **Non-atomic objective + days writes.** `register` commits the objective (`:113`) and then
    the days (`:121`); if day creation fails, the objective row with its roadmap markdown is
    durable with **zero** `roadmap_days`. Same in `renew_roadmap` (`:232`, `:239`). No
    `session.begin()` exists in `ObjectiveService` at all.
36. **No row lock on roadmap renewal.** Two concurrent `renew_roadmap` calls both pass the
    7-day check (`:184`, computed from a stale in-memory `roadmap_updated_at`), both call the AI,
    and the second overwrites the first. `ObjectiveRepository` has no `for_update` anywhere.
37. **TOCTOU on duplicate roadmap days.** Duplicate prevention is read-then-write
    (`roadmap_day_service.py:71` → `:95-105`) with **no** unique constraint on
    `(objective_id, day_date)`. Two concurrent `create_days` both insert.
38. **Lost conversation updates.** Both assistants do a read-modify-write of a single Redis
    string with no lock, `WATCH`/`MULTI`, or version
    (`general_agent_service.py:92-107`, `objective_assistant_service.py:71-114`). Concurrent tabs
    silently drop a turn. Worse in the assistant: the `clear()` on success (`:112`) can wipe a
    concurrent in-flight turn.
39. **Refresh-token rotation is not atomic.** `auth_service.refresh` reads the denylist
    (`:107`) and later writes the revocation (`:134`); two concurrent refreshes with the same
    token both pass and both yield a valid pair.
40. **`session.commit()` on a caller-owned session.** `RoadmapDayService.create_days:107-108`
    commits unrelated pending state as a side effect.
41. **`UserService.list_users` counts admins in Python.** `count_admins(for_update=True)` loads
    every admin id to `len()` it (`user_repository.py:67-69`).

### 9.5 Duplicated code / technical debt

42. **`_extract_json` is implemented twice and has already drifted** —
    `objective_service.py:399-436` vs `objective_assistant_service.py:118-160` are ~85 % identical;
    the first logs on every fallback, the second only conditionally. A fix to one will not apply
    to the other.
43. **`_ensure_aware` is duplicated verbatim** — `roadmap_day_service.py:235-245` and
    `objective_service.py:271-281`.
44. **The ownership check block is repeated 4×** — `roadmap_day_service.py:146-150,182-186,223-227`
    and `objective_service.py:174-179`, each 5 identical lines. A shared
    `_assert_owner(objective_id, user_id, role)` is missing.
45. **Identical 6-key serialisation literal written 4×** in `system_prompt_service.py:26-33,44-49,53-58,64-69` (75 lines total).
46. **Auth response envelope written 3×** — `auth_service.py:85-94,136-139,200-209`.
47. **Three identical one-field response models** —
    `ChatCompletionResponse`, `GeneralAgentResponse`, `ObjectiveAssistantResponse` — and two
    identical two-line request models. No shared base.
48. **Two classes named `UserResponse` with different shapes** —
    `app/schemas/auth.py:48-54` (4 fields) and `app/schemas/user_schemas.py:8-15` (5 fields);
    `UserProfileResponse` (`user_schemas.py:18-30`) is a third copy of the 4-field shape.
49. **Three near-identical single-field command DTOs** — `PromoteUserCommand`,
    `DemoteUserCommand`, `DeleteUserCommand`, each `{target_id: int}`; and three near-identical
    `__post_init__` empty-message validators.
50. **Dead code, verified by zero call sites**: `UserRepository.update_role` and
    `delete_by_id` (`user_repository.py:75,99`); `RoadmapDayRepository.create` (`:25`);
    `require_admin` (`app/api/deps.py:218` — every admin route uses
    `require_admin_db_role`); `self.session` stored but unused in
    `ChatCompletionService.__init__` (`chat_completion_service.py:34`) and
    `SystemPromptService.__init__` (`system_prompt_service.py:18`).
    `UserRepository.delete_by_id` would also leave an uncommitted delete if ever wired up
    (`:99-110`).
51. **The `AIClient` abstraction is undercut.** `ObjectiveService` is documented as
    unit-testable with an injected client (`:59-68`) but falls back to
    `self.ai_client or GroqAIClient()` (`:352`) — a provider-specific concrete class constructed
    internally, which raises `RuntimeError` (not `HTTPException`) → 500 when unconfigured.
52. **Three unmanaged, hardcoded, Portuguese-only system prompts** —
    `general_agent_service.py:79-90`, `objective_assistant_service.py:63-69`,
    `objective_service.py:353-366`. They are not in `system_prompts` and not configurable, so an
    operator cannot change assistant behaviour without a redeploy. The assistant's prompt is
    PT-only while its JSON parser is explicitly bilingual (`:86-94,166-175`).
53. **Prompt-injection surface.** Objective titles, descriptions and day content are
    concatenated verbatim into the general agent's model context with no delimiting
    (`general_agent_service.py:129-147`); a user-authored goal named
    `ignore previous instructions…` is injected into every later turn of that conversation.
54. **Magic-string duplication of cache prefixes.** `AI_RESPONSE_PREFIX = "ai_response:"`
    (`cache_flush_service.py:23`) exists, but the writer builds the same key from a hardcoded
    literal (`chat_completion_service.py:53`). Renaming one side silently breaks the flush with
    no error.
55. **Circular-import workarounds are inconsistent** — `objective_assistant_service` uses a
    runtime local import (`:48`), `objective_service` uses function-local repository imports
    (`:139,206`) despite already importing `RoadmapDayService` at module level (`:37`), and
    `roadmap_day_service` imports `RoadmapDay` eagerly (`:17`).
56. **Every `__init__.py` in `app/` except `app/models/__init__.py` is empty** (12 files; 13
    including `tests/__init__.py`) — no package-level re-exports, so consumers always import
    concrete modules.
57. **Style inconsistencies**: `d.status.COMPLETED` (enum via instance) at
    `objective_service.py:212-214` vs a proper import at `general_agent_service.py:20,138`;
    mixed ORM and Core DML styles inside `system_prompt_repository.py`; missing blank lines
    between methods at `auth_service.py:94/95` and `:187/188`; a Portuguese `Field(description=…)`
    in an otherwise English codebase (`roadmap_day_schemas.py:34`); stale docstring saying
    `AuthResponse.user` is "a dictionary" when it is a nested model (`auth.py:61-62`).
58. **Log noise / swallowed exceptions**: `except Exception: access_payload = {}` with no logging
    (`auth_service.py:180-181`) — the caller believes the access token was revoked when it was
    not; `objective_service._extract_json` logs a `warning` on the *expected* first
    `JSONDecodeError` (`:410-413`), emitting a warning on every successful registration.
59. **`ObjectiveRepository.update(objective_id, **values)`** does an untyped `setattr` loop
    (`:90,100-101`) with no whitelist — a typo silently creates a new attribute on the ORM
    instance instead of raising.
60. **Inconsistent pagination clamping**: `UserService.list_users` clamps internally
    (`user_service.py:211-212`); `SystemPromptService.list_prompts` does not
    (`system_prompt_service.py:21-23`) and would compute a negative offset for `page=0` if
    called directly.
61. **No pool/timeout hygiene in migrations.** `migrations/env.py:42` imports
    `app.db.session` purely for `Base`, which constructs a **second, never-connected,
    never-disposed** `AsyncEngine` on every `alembic` invocation. `:40` and `:43-48` also import
    the models twice. `env.py:125` uses `asyncio.run`, so `alembic` cannot be driven from inside
    a running event loop.
62. **`alembic downgrade` leaks both PostgreSQL enum types.** The `downgrade()` body
    (migration `:76-92`) drops all tables and indexes but never `DROP TYPE user_role` /
    `roadmap_day_status`, so `alembic downgrade base` followed by `upgrade head` fails with
    "type already exists". CI only ever runs `upgrade head`, so it is not caught.
63. **`user_role` enum drift between model and migration.** The model has three members
    (`USER`, `ADMIN`, `SERVICE` — `app/models/user.py:39-41`) but the migration creates a
    two-value type (`:37`). Benign today only because `SERVICE` is never persisted, and
    Alembic autogenerate does not compare native-enum labels (`env.py:94-98`), so CI will never
    flag it.
64. **No lock file** (§2) and `requirements.txt` ships test dependencies into the runtime image
    (`requirements.txt:16-17`).
65. **`conftest.py:25` `ROOT` points one directory too high** — `parent.parent` from a
    root-level file yields the project's *parent*; the `sys.path.insert` is a no-op and tests
    only work because pytest's `prepend` import mode inserts the directory independently.
    `conftest.py:31` also uses a **CWD-relative** SQLite path, so running `pytest` from another
    directory silently creates a second, empty `test.db`.
66. **`downstream_services` is a non-cached property** re-parsing JSON on every access
    (`config.py:252-265`) — it is read on every proxied request (`proxy.py:181`).
67. **Stray root artifacts**: `accounts.db` (0 bytes) and `test.db` (380 KB) sit in the project
    root. Both are git-ignored (`.gitignore:36`), so they are untracked leftovers.
68. **`/redoc` is not in the public allowlist** while `/docs` and `/openapi` are (§9.1 item 7).

---

## 10. What is incomplete, mocked, or not wired up

1. **`DownstreamService.allowed_paths` is declared but never enforced.** It is documented in
   `app/core/config.py:49-57` as "path prefixes that are permitted to be proxied", but no code
   in `app/api/v1/proxy.py` reads it. Anyone configuring it expecting a restriction gets none.
2. **The configured downstream target does not exist here.** The default is
   `http://mock-ai-service:8001` (`.env.example:65`, `config.py:213`) and the name itself
   indicates a placeholder. There is no implementation in this repository, so
   `/api/v1/proxy/ai/...` will 502 until a real service is deployed and
   `DOWNSTREAM_SERVICES_JSON` is repointed.
3. **`InMemoryStore` is a degraded stand-in for Redis**, not a Redis implementation: no TTLs,
   no atomicity, no persistence, explicitly "not shared across processes or workers"
   (`redis_client.py:63-64`). It is a legitimate dev/CI affordance but means rate limiting,
   denylists, conversation memory and response caching are all per-process in that mode —
   incorrect in any multi-worker deployment.
4. **The admin bootstrap's concurrency guarantee is untested.** The Postgres
   `LOCK TABLE ... SHARE ROW EXCLUSIVE` path (`user_repository.py:112-116`) is never exercised,
   because every test runs on SQLite (`conftest.py:31`) and the CI `migrate` job runs Alembic
   only — never application code against Postgres.
5. **The full-stack contract is documented but not present.** `API_REFERENCE.md` describes the
   mobile app's expected request/response shapes in detail, yet there is no OpenAPI snapshot
   test, no contract test, and no generated client — drift between the docs and the code would
   not be caught by CI.
6. **`GROQ_MAX_ATTEMPTS > 1` retry logic exists but is off by default** (default `1`,
   `config.py:246`) and is therefore effectively untested in production. The comment
   (`ai_client.py:22-24`) argues retries are counterproductive on the free tier, but the code
   path remains.
7. **A rate-limit probe route exists only in tests.** Per `API_REFERENCE.md:876-880`, the test
   suite references a `GET /rate-limit-probe` route that is registered only inside the test
   module as a stand-in protected path; it does not exist in the deployed app.
8. **No observability beyond structured logs.** No metrics, no tracing/OpenTelemetry, no error
   reporting service. The Groq client does emit rich correlation-id logs
   (`ai_client.py:385-464`), which is the strongest instrumentation in the project.
9. **No email verification, no password-reset, no MFA, no session/device management.** The
   login surface is e-mail + password only (`app/schemas/auth.py:36-45`); logout is client-driven.
10. **No rate limiting on failed logins specifically.** `/api/v1/auth/login` is only covered by
    the generic 60/min per-IP bucket, so credential stuffing is bounded but not specifically
    throttled, and there is no lockout or backoff.
11. **No account self-service deletion or role self-management** — a user can change only their
    display name and password (`user_router.py:46-110`); e-mail is explicitly immutable
    (`:53-56`).
12. **The `README.md` and much of `PROJECT_STATUS.md` are in Portuguese** while `API_REFERENCE.md`,
    `DEPLOY.md`, the code comments and the docstrings are in English — an inconsistent
    documentation language mix. `PROJECT_STATUS.md` also states "128 tests passing"
    (line 3, dated 2026-09-23) against 142 today.

---

*Analysis performed read-only; no project file was modified. Verification commands run:
`ruff check .` (pass), `mypy app` (pass, 81 files), `pytest` (142 passed), plus a read-only
introspection of `app.main:app` to enumerate the 50 registered routes. The test suite was
executed against a temporary SQLite database outside the repository so the working tree was
left untouched.*
