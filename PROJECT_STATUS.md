# NovaIAx — Project Status Report

_Generated: 2026-09-23 · Branch `main`; working tree carries pending fixes (RATE_LIMIT_AI, role dep, per-user ai_response cache, CI) · 128 tests passing_

This is an internal engineering review for planning, not user documentation.

---

## 1. Architecture overview

NovaIAx is a **FastAPI API Gateway** (Python 3.12, async) sitting in front of downstream services, with first-party AI features backed by Groq Cloud. It centralizes authentication, RBAC, rate limiting, caching, structured logging, a reverse proxy, and a server-rendered admin panel.

**Layers** (`app/`):

```
Cliente → Middlewares (RateLimit → Auth → Logging → SecurityHeaders → CORS)
              ↓
         Router → Command (frozen dataclass) → Service → Repository → Database
```

| Layer | Where | Contents |
| --- | --- | --- |
| Entry/middleware | `app/main.py`, `app/middlewares/` | FastAPI app, lifespan (admin bootstrap), 5 middlewares in fixed order |
| Config | `app/core/config.py` | Pydantic `Settings` singleton; env-driven; production secret validation |
| API v1 | `app/api/v1/` | Routers (auth, users, objectives, agents, ai, system-prompts, proxy, cache) + DI in `app/api/deps.py` |
| Commands | `app/commands/` | Immutable request objects (`LoginCommand`, `RegisterObjectiveCommand`, …) |
| Services | `app/services/` | Business rules (objective roadmap windows, agents, auth, chat, flush) |
| Repositories | `app/repositories/` | SQLAlchemy data access (`UserRepository`, `ObjectiveRepository`, …) |
| Models | `app/models/` | `User`, `Objective`, `RoadmapDay`, `SystemPrompt` |
| Clients | `app/clients/ai_client.py` | `AIClient` ABC + `GroqAIClient` with process-wide pacer/semaphore |
| Cache | `app/cache/` | `RedisClient` (in-memory fallback), `ConversationCache`, `TokenDenylist` |
| Admin panel | `app/api/admin.py` + `app/templates/` | Jinja2 HTML panel, cookie+CSRF auth over the existing service layer |
| Exceptions | `app/exceptions/`, `app/api/admin.py` | Global JSON error handlers + admin redirect handler |

**Middlewares execute as `RateLimit → Auth → Logging → SecurityHeaders → CORS → handler`** (reverse of registration order in `app/main.py:124-145`). AuthMiddleware treats `/auth`, `/api/v1/auth`, `/docs`, `/openapi`, `/health`, `/admin` as public; everything else requires JWT or `X-API-Key`.

**Data flow highlights:**
- **Objectives/goals**: `ObjectiveService` generates a 7-day AI roadmap per window, persists one `RoadmapDay` per calendar day (each carrying a *meta básica*), and `renew_roadmap` is callable every 7 days up to `due_date` (`app/services/objective_service.py`).
- **Agents**: `ObjectiveAssistantService` collects goal data conversationally (multi-turn cache) then auto-registers the objective; `GeneralAgentService` injects the user's objectives + roadmap-day status as context per turn; `ChatCompletionService` binds an admin-managed system prompt (`/ai/chat`).
- **Proxy**: `/proxy/{service}/...` forwards to configured downstreams with per-caller Redis cache, header stripping, 1 MiB payload cap, per-service timeout (`app/api/v1/proxy.py`).
- **Admin bootstrap**: on startup, creates the default ADMIN if none exists and re-syncs the default admin's password to `ADMIN_DEFAULT_PASSWORD` when it doesn't match (`app/db/bootstrap.py`).

---

## 2. Endpoint inventory

Auth column: **public** = no credential needed · **user** = valid JWT or API key · **db-user** = real DB user (API key → 403) · **admin** = role ADMIN (API-key → 403).

### Auth — `app/api/v1/auth.py` (prefix `/api/v1/auth`)

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| POST | `/register` | public | Create user; first user becomes ADMIN; returns JWT pair |
| POST | `/login` | public | Email/password → JWT pair |
| POST | `/refresh` | public | Rotate a refresh token into a new pair (revokes the old one) |
| POST | `/logout` | public¹ | Revoke refresh token's `jti` (denylist) |
| GET | `/me` | db-user | Current user identity |

¹ `logout` is middleware-public but requires a *valid refresh token* in the body, so it is not usable without credentials.

### Agents / AI / System Prompts

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| POST | `/api/v1/ai/chat` | user | Chat completion bound to an admin-managed system prompt; **per-user** cached 300s |
| POST | `/api/v1/agents/general/chat` | db-user | Conversational agent w/ live objectives + roadmap-day status context |
| DELETE | `/api/v1/agents/general/conversation` | db-user | Clear caller's general-agent history |
| POST | `/api/v1/objectives/assistant` | db-user | Goal-collection assistant; registers objective when complete |
| GET | `/api/v1/system-prompts/` | user | List system prompts (paginated) — **opened to users** |
| GET | `/api/v1/system-prompts/{id}` | user | Get one system prompt by id — **opened to users** |
| POST | `/api/v1/system-prompts/` | admin | Create prompt |
| PUT | `/api/v1/system-prompts/{id}` | admin | Update prompt |
| DELETE | `/api/v1/system-prompts/{id}` | admin | Delete prompt |

### Objectives / Goals — `app/api/v1/objective_router.py`, `roadmap_day_router.py`

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/api/v1/objectives/` | db-user | List caller's objectives with roadmap days (offset/limit) |
| POST | `/api/v1/objectives/register` | db-user | Create objective + AI 7-day roadmap window |
| POST | `/api/v1/objectives/{id}/roadmap/renew` | db-user | Generate next 7-day window (owner/admin; 409 in-window) |
| GET | `/api/v1/objectives/{id}/roadmap/days` | db-user | List roadmap days (owner/admin) |
| PATCH | `/api/v1/objectives/{id}/roadmap/days/{day_id}` | db-user | Mark day COMPLETED/PENDING/SKIPPED (owner/admin) |
| DELETE | `/api/v1/objectives/{id}/roadmap/days/{day_id}` | db-user | Remove a day from the roadmap (owner/admin) |

### Users (admin API) — `app/api/v1/user_router.py`

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| PATCH | `/api/v1/users/{id}/promote` | admin (DB-fresh) | Promote user to ADMIN |
| PATCH | `/api/v1/users/{id}/demote` | admin (DB-fresh) | Demote admin to USER |
| DELETE | `/api/v1/users/{id}` | admin (DB-fresh) | Delete user (+ cascade objectives) |
| GET | `/api/v1/users/` | admin (DB-fresh) | Paginated user list |

### Cache / Proxy / Health

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| POST | `/api/v1/cache/flush` | db-user | Flush caller's conversation + proxy-cache entries |
| GET/POST/PUT/PATCH/DELETE | `/api/v1/proxy/{service}/...` | user | Reverse proxy w/ cache, header strip, 413, 502 |
| GET | `/health` | public | Liveness/readiness; real DB+Redis check; 503 on failure |

### Admin panel (HTML) — `app/api/admin.py` (prefix `/admin`, cookie + CSRF)

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/admin/login` | public | Login form |
| POST | `/admin/login` | public¹ | Authenticate, require ADMIN, set session cookie |
| POST | `/admin/logout` | admin | Clear session cookie |
| GET | `/admin/` | admin | Dashboard (users/prompts/admins counts) |
| GET | `/admin/users` | admin | Paginated user table |
| POST | `/admin/users/{id}/promote` `/demote` `/delete` | admin | Role/user actions |
| GET | `/admin/system-prompts` | admin | Prompt table |
| POST | `/admin/system-prompts` | admin | Create prompt |
| GET | `/admin/system-prompts/{id}/edit` | admin | Edit form |
| POST | `/admin/system-prompts/{id}` | admin | Update prompt |
| POST | `/admin/system-prompts/{id}/delete` | admin | Delete prompt |

¹ Form login requires real admin credentials; non-admins get 403.

---

## 3. What works vs. partial vs. not started

### Fully working (implemented + tested)
- **Auth & tokens**: register/login/refresh/logout/me, JWT (15 min access / 7 day refresh), PBKDF2 hashing, refresh-token rotation + denylist revocation, `refresh_token_valid_after` invalidation on role change, first-user-is-admin (advisory lock).
- **RBAC**: USER/ADMIN/SERVICE (API-key in-memory); admin deps re-check the DB role.
- **Objectives**: register with due-date validation, AI roadmap generation, 7-day window renewal (409 in-window/400 expired), roadmap-day status marking, roadmap-day removal (owner/admin), cross-user 404/403 isolation.
- **Agents**: general agent (live context), objective assistant (multi-turn → auto-register, clears history on success), `/ai/chat` (prompt-scoped, cached).
- **System prompts**: CRUD; **GET list/get-by-id now available to any authenticated user** (the Flutter read path).
- **Admin panel**: full HTML CRUD (users + prompts) with cookie session + conditional CSRF.
- **Cache flush**: per-user (`/api/v1/cache/flush`) clearing conversations + proxy entries without global state.
- **Proxy**: routing, per-caller cache (SHA-256 key + index sets), sensitive header stripping, 413/502, binary-safe cache serialization.
- **Redis fallback**: transparent in-memory store (dev/CI); re-tries real Redis with 30s cooldown.
- **Groq traffic control**: process-wide RPM/TPM pacer, concurrency semaphore, no silent 429 retries, `Retry-After` propagated, correlation-id logging.
- **Health**: real `SELECT 1` + Redis ping; 200/503.
- **Admin bootstrap**: now honors `.env` credentials and re-syncs a mismatched default-admin password.

### Partially working / degraded paths
- **AI roadmap parse-failure fallback**: when Groq returns text without valid JSON, the objective is still created but `days` have empty content and the roadmap is raw AI text — graceful but degraded (`objective_service.py:_extract_json`).
- **429 handling is strict**: `GROQ_MAX_ATTEMPTS=1` by default means a quota hit surfaces to the user as 429 — intentional (each attempt counts toward the org quota) but user-visible.
- **Token estimation is heuristic**: `chars/4` (not a real tokenizer) drives the pacer budget (`ai_client.py:_estimate_payload_tokens`) — approximate by design.
- **Rate limiting**: sliding window per-IP works, but the **check + record is non-atomic** (can marginally overshoot under concurrency), and the org-quota guard is the *in-process* pacer (per-worker, not cross-worker).
- **In-memory Redis fallback does not enforce TTLs**: `InMemoryStore.expire` is a no-op and `set(..., ex=...)` ignores expiry (`redis_client.py`) — harmless for dev, but a behavioral gap vs real Redis.
- **`ai_response` cache:** now **user-scoped** (`ai_response:{agent_id}:{user_id}:{digest}`, `chat_completion_service.py`) — identical messages from different users no longer share a hit, and `/cache/flush` clears a user's AI responses too.

### Not started / absent
- **No coverage measurement** (pytest config has no `--cov`; no `coverage` dependency).
- **No `/redoc` public route** — middleware returns 401 on the default FastAPI route (`auth_middleware.py:_PUBLIC_PATH_PREFIXES`).
- **No user self-service** (password change/reset), no webhooks, no metrics/telemetry export, no Prometheus/OTel.
- **No bundled downstream service**: config default references `mock-ai-service` but no such container ships in `docker-compose.yml`.

---

## 4. Known open issues & status

| Area | Status | Details |
| --- | --- | --- |
| **Admin bootstrap ignored `.env` password** | ✅ Fixed | Root cause was env-var name mismatch (`DEFAULT_ADMIN_PASSWORD` in `.env` vs `ADMIN_DEFAULT_PASSWORD` field). Config now honors both; bootstrap re-syncs a mismatched default-admin password on startup. Committed in `36078e6`; 8 tests. |
| **System-prompts GET required ADMIN** | ✅ Fixed | `require_admin` → `get_current_user` on list + by-id; writes unchanged. Committed in `7df886b`; 4 tests (`tests/test_system_prompt_read_access.py`). |
| **Cache flush** | ✅ Fixed | Per-user flush (`/cache/flush`): conversations + user-scoped `ai_response` entries (key-pattern scan) + proxy index sets. Committed in `ffaf782`; service imports a constant from `app.api.v1.proxy` (`cache_flush_service.py`) — layer smell. |
| **Groq free-tier 429 exposure** | 🟡 Partially mitigiated | Pacer (20 RPM / 6K TPM) + semaphore (2) + no silent retries. Might revisit: real tokenizer, cross-worker budget sharing, per-route budget. |
| **`RATE_LIMIT_AI` value drift** | ✅ Fixed | Aligned to **10** everywhere: `config.py:195`, `.env`, `.env.example`, `render.yaml`, `README.md`, `API_REFERENCE.md`, middleware docstring. |
| **Role dep inconsistency** | ✅ Fixed | `system_prompt_router` POST/PUT/DELETE switched `require_admin` → `require_admin_db_role` (fresh DB role, matching `user_router`); a demoted/deleted admin loses write access immediately. Regression tests added. |
| **Roadmap-day deletion** | ✅ Fixed | `DELETE /api/v1/objectives/{id}/roadmap/days/{day_id}` (204) — owner/admin; hard delete, remaining days keep `day_number`. 6 regression tests. |
| **Rate limiter check+record race** | 🟡 Open | Non-atomic `zcard` → `zadd` in `rate_limit_middleware.py:106-126`; can overshoot slightly under concurrency. |
| **Proxy caches 5xx** | 🟡 Open | `proxy.py:255-270` caches any GET/HEAD response including 5xx; `_cache_key` is `async def` with no `await` (cosmetic). |
| **CSP vs admin templates** | 🟡 Open | `default-src 'self'` + inline `onsubmit`/`onclick` in `app/templates/admin/users.html` & `prompts.html` → browser `confirm()` on delete may not fire. Panel tests use raw POST, so this isn't caught. |
| **`/redoc` → 401** | 🟡 Open | Not in the public-path allowlist; add it or remove the default route. |
| **Redis fallback TTL semantics** | 🟡 Open | In-memory store ignores `ex`/`expire`. |
| **Doc drift** | 🟡 Open | API_REFERENCE duplicate "9" sections, stale rate-limit text ("10"), stale legacy env mentions; `auth_service.py` docstrings reference a nonexistent in-memory `AuthService`. |
| **Deploy hygiene** | 🟡 Open | Production Docker image installs pytest/pytest-asyncio (`requirements.txt` mixes test deps); stray `accounts.db` (0 bytes) in repo root; `.env` `DATABASE_URL` points at LAN IP while compose uses `postgres` hostname (dev-only, but confusing). |
| **Dead/misused config** | 🟡 Open | `config.api_key_header` unused (header hardcoded `x-api-key` in `deps.py`/`proxy.py`); `settings.host/port` unused. |

---

## 5. Test coverage (128 tests, pytest, `asyncio_mode=auto`)

Suite runs on **SQLite + in-memory Redis fallback** through the real ASGI app (`TestClient`, full middleware stack). `conftest.py` creates tables once and resets all memory stores between tests.

| File | Count | Covers |
| --- | --- | --- |
| `test_roadmap_flow_audit.py` | 13 | Malformed AI JSON, duplicate days, ordering across windows |
| `test_groq_resilience.py` | 12 | Pacer RPM/TPM, 429 (no-retry / optional retry), backoff, token trim |
| `test_admin_panel.py` | 10 | Panel login/session/CSRF/cookie, promote/demote/delete, prompt CRUD |
| `test_roadmap_day.py` | 14 | Day creation/status transitions/**deletion** |
| `test_admin_bootstrap.py` | 8 | Default-admin create, idempotency, **password re-sync**, legacy env |
| `test_objective_service.py` | 8 | Register/renew/7-day window/ownership |
| `test_roadmap_navigation_http.py` | 10 | E2E register→list→days, cross-user 404/403, day deletion |
| `test_general_agent.py` | 7 | Context assembly, history, clear route |
| `test_objective_assistant.py` | 7 | Multi-turn collection, JSON parse, auto-register |
| `test_specific_bug_fixes.py` | 7 | API-key 403, admin-login redirect loop, per-caller binary cache |
| `test_auth_and_rate_limit.py` | 6 | Login/register, refresh + logout revocation, 429, CORS CSV, SERVICE role |
| `test_health.py` | 6 | `/health` DB+Redis up/down permutations |
| `test_network.py` | 6 | Client-IP / X-Forwarded-For trust |
| `test_objective_assistant_english_keys.py` | 1 | Assistant JSON with English keys |
| `test_renew_and_duplicates.py` | 1 | Renewal summary + duplicate-day prevention |
| `test_cache_flush.py` | 4 | User-scoped flush (conversations + ai_response + proxy) |
| `test_chat_completion_cache.py` | 2 | `/ai/chat` cache is per-user; no cross-user hits |
| `test_system_prompt_read_access.py` | 4 | Non-admin reads OK, unauthenticated 401, writes still 403 |

**Zero / weak coverage (gaps):**
- **Role-change revocation**: demoting/promoting updates `refresh_token_valid_after` (bumped in `UserService`) — no direct test that pre-change refresh tokens stop working.
- **CSRF mismatch path**: `_validate_csrf` raising 403 when a CSRF cookie exists but token differs (`admin.py:153`) — untested.
- **Proxy 413** (payload too large) and **proxy 502** (downstream unreachable) — untested through the real stack.
- **`renew_roadmap` 400 "Objective already expired"** path — 409 is tested; the expired branch is not.
- **Rate-limit AI bucket** (the strict `/ai/` bucket) — only the default bucket is exercised via `/rate-limit-probe`.
- **Groq client network-error branches** (timeout→504, 401→502, invalid JSON→502) are not integration-tested (service-layer mocks used).
- **In-memory fallback TTL/cooldown behavior** — partially, via helpers; no test that a recovering Redis is re-picked.

---

## 6. Deployment status

**Live target: Render** (blueprint `render.yaml`), free tier:
- Web service `novaiax-gateway` (Docker runtime, region `virginia`, `plan: free`, health check `/health`).
- Managed Postgres `novaiax-db` (free), `DATABASE_URL` auto-injected (normalized to `postgresql+asyncpg` in `config.py`).
- **Redis is external (Upstash)** — Render free has no managed Redis; `REDIS_URL` is a manual secret.
- Migrations run **in-container** via `entrypoint.sh` (`alembic upgrade head` then uvicorn), because Render's `preDeployCommand` is paid-only.
- Secrets (set once in dashboard, `sync: false`): `SECRET_KEY`, `GROQ_API_KEY`, `MASTER_API_KEY`, `DEFAULT_ADMIN_PASSWORD` (legacy name; `ADMIN_DEFAULT_PASSWORD` also accepted), `REDIS_URL`. Non-secrets committed in `render.yaml`.
- Production boot **hard-fails** if `SECRET_KEY` is a placeholder or `MASTER_API_KEY`/`GROQ_API_KEY`/`DATABASE_URL`/`REDIS_URL` are missing (`config.py:_validate_production_secrets`).

**Fragile points:**
1. **Free-plan spin-down**: 15 min idle → cold start ~30-60 s; first request after idle is slow. `preDeployCommand` needed to fix properly (paid).
2. **Free Postgres expires after 30 days** — will need a paid DB or migration.
3. **Migrations run on every boot** inside the serving container — adds cold-start latency; a failed migration fails the deploy (correct but slow feedback).
4. **`GROQ_RATE_LIMIT_RPM` / `GROQ_TOKEN_BUDGET_PER_MINUTE` / `GROQ_MAX_CONCURRENCY` not set in `render.yaml`** → production silently uses defaults (20/6000/2). Defaults are fine for a single worker, but there is no override if workers are added.
5. **`RATE_LIMIT_AI` now consistent at 10** everywhere (code default, `.env`, `.env.example`, `render.yaml`, docs).
6. **CI is in place** (GitHub Actions, `.github/workflows/ci.yml`): `ruff check`, `mypy`, `pytest`, and `alembic upgrade head` against ephemeral Postgres on every push/PR.

---

## 7. Suggested next steps (priority order)

1. **CI added** (done — `.github/workflows/ci.yml`, on push/PR).
2. **`RATE_LIMIT_AI` reconciled to 10** (done). Next: pin `GROQ_RATE_LIMIT_RPM`/`GROQ_TOKEN_BUDGET_PER_MINUTE`/`GROQ_MAX_CONCURRENCY` explicitly in `render.yaml` so prep happens before multi-worker deploys.
3. **Security-consistency sweep**:
   - Make `system_prompt_router` writes use `require_admin_db_role` (fresh-role check like `user_router`).
   - Add `/redoc` to public paths (or disable the route).
   - Fix the CSP/inline-handler bug so admin delete `confirm()` actually fires (move to `addEventListener` or relax CSP for scripts).
4. **Harden the rate limiter** — atomic check+record (Lua script or `WATCH`/`MULTI`) to remove the overshoot race.
5. **Proxy polish** — don't cache 5xx; drop the misleading `async def _cache_key`; document/deal with 413/502 error paths (and add the two missing tests).
6. **`ai_response` cache scoped per user** (done) — `/ai/chat` keys now embed `user_id` and the per-user flush clears them; README/API_REFERENCE updated.
7. **Deploy hygiene** — split dev/test deps out of the production image (`pyproject` extras + `--no-deps` or separate file), remove stray `accounts.db`, align `.env`/compose `DATABASE_URL`, pick one admin-password env name everywhere.
8. **Close test gaps** — role-change refresh revocation, CSRF 403, proxy 413/502, renew-expired 400, AI rate-limit bucket.
9. **Plan the Render upgrade** — before real traffic: paid plan (kill spin-down), paid/expiry-safe DB, and consider moving migrations to a build-time step.

---

### Sources

- Routers: `app/api/v1/{auth,user_router,objective_router,roadmap_day_router,objective_assistant_router,general_agent_router,chat_router,system_prompt_router,proxy,cache_router,router}.py`, `app/api/admin.py`
- Services: `app/services/*.py` · Clients: `app/clients/ai_client.py` · Cache: `app/cache/*.py`
- Middlewares: `app/middlewares/*.py` · Config: `app/core/config.py` · Bootstrap: `app/db/bootstrap.py`
- Deploy: `render.yaml`, `entrypoint.sh`, `Dockerfile`, `docker-compose*.yml`, `DEPLOY.md`
- Tests: `tests/` (18 files, 128 tests) · Lint/type: `pyproject.toml` (ruff, mypy)