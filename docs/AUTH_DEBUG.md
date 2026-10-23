# Authorization Debug Audit

Audit date: 2026-10-23. This describes the checked-out code before the authorization redesign.

## Reproduction and conclusion

An in-process `fastapi.testclient.TestClient` reproduced `GET /` as `401 authentication_required`, both without `Authorization` and with `Authorization: Bearer garbage`. A successful `POST /api/v1/auth/register` returned `201` without credentials; the resulting access token reached `/api/v1/auth/me` (`200`), while an expired token was rejected there (`401 authentication_required`). Garbage Bearer headers were ignored on `/api/v1/app-config`, `/privacy`, `/terms`, `/account-deletion`, `/docs`, and `/openapi.json`. Login with a wrong password returned `401 invalid_credentials`. Invalid refresh-token bodies returned `401`; these are expected body-token validation failures, not rejection of the Authorization header. `/health` returned `503` in this environment because Redis was unavailable (the database check passed).

**Proven root cause:** `AuthMiddleware` is a global, path-based authentication gate. Its allowlist omits `/`, so the base URL gets a missing-token 401 before a handler can answer. It also makes public policy depend on regex coverage and runs a database-backed suspension check before dispatch for every non-allowlisted path. The broader claim that every named public endpoint returns 401 was not reproducible in this checkout: the allowlist below lets those paths through, including with garbage Bearer headers. `/health` can independently return 503 when a dependency is unavailable.

`/api/v1/objectives` redirects to `/api/v1/objectives/` (the registered path); with redirects disabled, the missing-slash request returned 307. Adding `X-Forwarded-Proto: https` and `X-Forwarded-For` did not produce a Location in this baseline because auth rejected both paths first. No Uvicorn/curl baseline was run.

## Middleware execution order

`app/main.py` registers CORS, security headers, logging, authentication, then rate limiting. Starlette executes the most recently registered middleware outermost:

| Order | Component | What can reject before the handler |
|---|---|---|
| 1 (outermost) | `RateLimitMiddleware` | `429 rate_limit_exceeded`; skips `/health`; Redis errors fail open. Uses client-IP buckets, or decoded access-token user ID for AI paths. |
| 2 | `AuthMiddleware` | For paths outside its public allowlist, invokes `get_current_user`; returns `401`, suspended-user `403`, or propagated `429`. Unexpected auth errors become `500`. |
| 3 | `LoggingMiddleware` | Logs request/response; no authorization. |
| 4 | `SecurityHeadersMiddleware` | Adds response headers; no authorization. |
| 5 | `CORSMiddleware` | Handles CORS/preflight. Being inside auth means an OPTIONS path not allowlisted can be rejected before CORS handles it. |
| 6 | FastAPI route/dependencies | Route code may return `401`, `403`, `404`, `429`, or `503`. |

## Current global auth allowlist

`app/middlewares/auth_middleware.py` defines `_PUBLIC_PATH_PREFIXES`; `_is_public_path(path)` uses `pattern.match(path)` for each compiled regex. This policy is path-only, not method- or route-based.

| Regex | Exact matching rule |
|---|---|
| `^/auth(?:/|$)` | `/auth` and descendants, any method. |
| `^/api/v1/auth(?:/|$)` | `/api/v1/auth` and descendants, any method. |
| `^/docs(?:/|$)` | `/docs` and descendants, any method. |
| `^/openapi(?:\.json)?(?:/|$)` | `/openapi`, `/openapi.json`, and slash descendants, any method. |
| `^/redoc(?:/|$)` | `/redoc` and descendants, any method. |
| `^/health$` | Exact `/health` only; no slash variant. |
| `^/admin(?:/|$)` | `/admin` and descendants, any method; admin has its own controls. |
| `^/privacy(?:/|$)`, `^/terms(?:/|$)`, `^/account-deletion(?:/|$)` | Each page and descendants, any method. |
| `^/api/v1/billing/google/rtdn$` | Exact path only; handler verifies Pub/Sub itself. |
| `^/api/v1/app-config$` | Exact path only. |

No trailing-slash normalization occurs before this check. Auth has two explicit prefixes (`/auth` and `/api/v1/auth`); app-config and RTDN require the full versioned path. Methods are not considered, so an unsupported method at a matching path still bypasses this gate. `/` is absent. On any other path, malformed/expired Bearer credentials are rejected by `get_current_user`.

## Other pre-handler authorization/status sources

| Component | Current behavior |
|---|---|
| `ADMIN_ALLOWED_IPS` | `enforce_panel_ip` is an `/admin` router dependency; an unlisted source IP gets `403`. `get_client_ip` trusts forwarded values only when `TRUST_PROXY_HEADERS` is enabled. |
| `ADMIN_PANEL_ENABLED` | `enforce_panel_enabled` applies to `/admin`; disabled panel returns `404`. It does not gate `/api/v1` or `/`. |
| Suspension | `_load_user`, reached through `get_current_user`, returns `403 account_suspended`. The global middleware makes it run on every non-allowlisted path, even before the route is selected. Allowlisted public routes skip it. Refresh independently rejects suspended accounts with 403. |
| Maintenance mode | `maintenance_mode` is configuration returned by `/api/v1/app-config`; no global maintenance gate returning 401/403 was found. |
| Rate limit and login lockout | Global rate middleware and `AuthService` login lockout return 429, not 401. Login lockout is email+IP keyed. |
| CSRF | Public account-deletion POST may return 403 when its cookie/form token is invalid. Admin login and panel mutations have separate CSRF checks and may return 403. |
| API keys | `get_current_user` accepts a valid Bearer access JWT or `X-API-Key`. Invalid keys do not authenticate. API-key identities are `SERVICE`; `require_db_user` rejects them with 403. Global middleware skips this logic only on allowlisted paths. |
| Route/ownership checks | `require_db_user`, admin-role dependencies, and service-level ownership checks can return 401/403 after dispatch reaches FastAPI. |
| Readiness | `/health` returns 503 when its DB or Redis check fails; this is availability, not authorization. |

## Registered route inventory

The following is the compact method/path table from `app.openapi()["paths"]` (226 method/path operations across 164 paths). FastAPI in the installed environment stores included routers lazily, so the nested operations do not appear as ordinary top-level `APIRoute` objects in `app.routes`. Methods are grouped only for identical paths. Access labels: Public, User (valid JWT/API key), DB user (database-backed identity), Admin, or panel-cookie auth. For each non-allowlisted path, current global enforcement is `AuthMiddleware -> get_current_user`; additional explicit route dependencies are noted by router. The listed public paths are the intended public exceptions; all other API operations are intended to be authenticated.

| Method | Path | Intended access | Actual enforcement today |
|---|---|---|---|
| GET | `/` | Public | No route exists; middleware returns 401 without credentials. |
| GET | `/health` | Public | Allowlisted; readiness may return 503. |
| GET | `/docs`, `/docs/oauth2-redirect`, `/redoc`, `/openapi.json` | Public | Framework routes; allowlisted. |
| GET | `/privacy`, `/terms`, `/account-deletion` | Public | Allowlisted public-page handlers. |
| POST | `/account-deletion/request` | Public | Allowlisted; CSRF may return 403. |
| GET, POST | `/api/v1/auth/login`, `/api/v1/auth/register` | Public | Auth-prefix allowlisted; body credentials/validation apply; wrong password is 401 `invalid_credentials`, lockout is 429. |
| POST | `/api/v1/auth/refresh`, `/api/v1/auth/logout` | Public | Auth-prefix allowlisted; refresh token in the body is validated; invalid body token returns 401. |
| GET | `/api/v1/auth/me` | DB user | Global middleware plus `require_db_user`; missing/invalid token 401, service key/suspension 403. |
| GET | `/api/v1/app-config` | Public | Exact path allowlisted. |
| POST | `/api/v1/billing/google/rtdn` | Public, Pub/Sub-authenticated | Exact path allowlisted; endpoint verifies Pub/Sub and can return 401. |
| GET | `/admin/login` | Public panel login | JWT bypass; panel enabled/IP checks and login CSRF/password flow. |
| POST | `/admin/login` | Public panel login | JWT bypass; panel enabled/IP checks and login CSRF/password flow. |
| GET, POST, PATCH, DELETE | `/admin/*` registered admin routes | Admin/panel-cookie auth | JWT bypass; router-level enabled/IP checks; admin pages/mutations require cookie session, mutations generally require CSRF. `/admin/login` is the public panel exception. |
| GET | `/admin/static/*` mount | Public asset | JWT bypass; mounted static app, no panel cookie dependency. |
| GET, POST | `/api/v1/community/blocks`, `/api/v1/community/drafts`, `/api/v1/community/reports` | DB user | Global middleware plus route DB-user/ownership checks. |
| GET | `/api/v1/community/official-contacts`, `/api/v1/community/support` | DB user | Global middleware; route handler/dependency checks. |
| POST | `/api/v1/community/drafts/generate` | DB user | Global middleware plus DB-user and AI/plan checks. |
| PATCH, DELETE | `/api/v1/community/drafts/{draft_id}` | DB user | Global middleware plus DB-user/ownership checks. |
| GET, PATCH, DELETE | `/api/v1/community/reports/{report_id}` | DB user | Global middleware plus DB-user/ownership/moderation checks. |
| POST, DELETE | `/api/v1/community/reports/{report_id}/confirm` | DB user | Global middleware plus DB-user/ownership checks. |
| POST | `/api/v1/community/reports/{report_id}/flag`, `/api/v1/community/reports/{report_id}/photo` | DB user | Global middleware plus DB-user/service checks. |
| DELETE | `/api/v1/community/blocks/{blocked_id}` | DB user | Global middleware plus DB-user check. |
| GET, POST, PUT, PATCH, DELETE | `/api/v1/proxy/{service_name}` | User/service | Global middleware plus `get_current_user`; API-key and proxy policy apply. |
| GET, POST | `/api/v1/system-prompts/`, `/api/v1/system-prompts/{prompt_id}` | Admin | Global middleware plus route identity/role checks. |
| GET | `/api/v1/users/` | Admin | Global middleware plus live admin DB-role check. |
| PATCH | `/api/v1/users/me`, `/api/v1/users/me/password` | DB user | Global middleware plus DB-user dependency. |
| DELETE | `/api/v1/users/{user_id}` | Admin | Global middleware plus live admin DB-role check. |
| PATCH | `/api/v1/users/{user_id}/demote`, `/api/v1/users/{user_id}/promote` | Admin | Global middleware plus live admin DB-role check. |
| POST | `/api/v1/cache/flush` | Admin/service | Global middleware plus route authorization. |
| GET | `/api/v1/agent-groups`, `/api/v1/sub-agents` | DB user | Global middleware; route dependency/handler applies. |
| POST | `/api/v1/agents/actions/{action_id}/confirm`, `/api/v1/agents/actions/{action_id}/reject`, `/api/v1/agents/actions/{action_id}/result` | DB user | Global middleware plus identity/ownership checks. |
| POST | `/api/v1/agents/chat`, `/api/v1/agents/device/command`, `/api/v1/agents/general/chat` | DB user | Global middleware plus DB-user/entitlement checks. |
| DELETE | `/api/v1/agents/general/conversation`, `/api/v1/ai/chat/conversation` | DB user | Global middleware plus DB-user dependency. |
| POST | `/api/v1/ai/chat`, `/api/v1/ai/feedback` | DB user | Global middleware plus DB-user and quota/feedback checks. |
| DELETE | `/api/v1/me` | DB user | Global middleware plus DB-user/account-deletion flow. |
| GET | `/api/v1/me/agenda`, `/api/v1/me/export`, `/api/v1/me/finance/summary`, `/api/v1/me/medications/adherence`, `/api/v1/me/study/mind-map`, `/api/v1/me/study/quiz`, `/api/v1/me/subscription`, `/api/v1/me/today` | DB user | Global middleware plus DB-user/feature checks. |
| GET, PUT | `/api/v1/me/agent`, `/api/v1/me/consents`, `/api/v1/me/finance/budgets`, `/api/v1/me/learning` | DB user | Global middleware plus DB-user and owner scoping. |
| GET, POST | `/api/v1/me/checklists`, `/api/v1/me/finance/bills`, `/api/v1/me/finance/expenses`, `/api/v1/me/finance/incomes`, `/api/v1/me/focus-sessions`, `/api/v1/me/habits`, `/api/v1/me/logs`, `/api/v1/me/medications`, `/api/v1/me/memories`, `/api/v1/me/notes`, `/api/v1/me/reminders`, `/api/v1/me/study/decks`, `/api/v1/me/study/plans`, `/api/v1/me/tasks` | DB user | Global middleware plus DB-user and owner scoping. |
| GET, PATCH, DELETE | `/api/v1/me/checklists/{checklist_id}`, `/api/v1/me/finance/bills/{bill_id}`, `/api/v1/me/finance/expenses/{expense_id}`, `/api/v1/me/finance/incomes/{income_id}`, `/api/v1/me/focus-sessions/{session_id}`, `/api/v1/me/logs/{entry_id}`, `/api/v1/me/medications/{medication_id}`, `/api/v1/me/notes/{note_id}`, `/api/v1/me/reminders/{reminder_id}`, `/api/v1/me/study/decks/{deck_id}`, `/api/v1/me/study/plans/{plan_id}`, `/api/v1/me/tasks/{task_id}` | DB user | Global middleware plus DB-user/ownership checks. |
| GET, DELETE | `/api/v1/me/habits/{habit_id}` | DB user | Global middleware plus DB-user/ownership checks. |
| POST | `/api/v1/me/checklists/{checklist_id}/items`, `/api/v1/me/habits/{habit_id}/check-ins`, `/api/v1/me/medications/{medication_id}/intakes`, `/api/v1/me/reminders/{reminder_id}/snooze`, `/api/v1/me/study/cards/{card_id}/reviews` | DB user | Global middleware plus DB-user/ownership checks. |
| PATCH, DELETE | `/api/v1/me/checklists/{checklist_id}/items/{item_id}`, `/api/v1/me/medication-intakes/{intake_id}`, `/api/v1/me/memories/{key}`, `/api/v1/me/study/cards/{card_id}` | DB user | Global middleware plus DB-user/ownership checks. |
| GET | `/api/v1/me/medication-intakes`, `/api/v1/me/logs/report` | DB user | Global middleware plus DB-user dependency. |
| POST | `/api/v1/me/promo-codes/redeem`, `/api/v1/me/weekly-plan`, `/api/v1/me/weekly-plan/accept` | DB user | Global middleware plus DB-user checks; promo/AI limits can be 429. |
| GET | `/api/v1/objectives/` | DB user | Global middleware plus DB-user/owner checks. Slashless path currently redirects. |
| POST | `/api/v1/objectives/assistant`, `/api/v1/objectives/register`, `/api/v1/objectives/{objective_id}/roadmap/renew` | DB user | Global middleware plus DB-user/entitlement/quota checks. |
| GET | `/api/v1/objectives/{objective_id}/roadmap/days` | DB user | Global middleware plus DB-user/ownership checks. |
| PATCH, DELETE | `/api/v1/objectives/{objective_id}/roadmap/days/{day_id}` | DB user | Global middleware plus DB-user/ownership checks. |
| GET | `/api/v1/promotions/active` | User | Global middleware is the authentication gate. |
| GET, POST | `/api/v1/billing/google/verify` | DB user | Global middleware plus billing route dependencies. |

The API route families not called out individually above are also part of the expanded route inventory. Their exact operation paths and methods are listed here, grouped by router; all are protected by the global middleware today, with explicit DB-user checks on their router handlers where present:

| Router/access | Registered paths and methods | Actual enforcement today |
|---|---|---|
| Agent personalization (DB user) | `GET /api/v1/agent-groups`, `GET /api/v1/sub-agents`, `GET /api/v1/agent-groups`, `GET /api/v1/sub-agents` | Global middleware; route-level DB-user checks. |
| Finance (DB user) | `GET,POST /api/v1/me/finance/expenses`; `PATCH,DELETE /api/v1/me/finance/expenses/{expense_id}`; `GET,POST /api/v1/me/finance/incomes`; `PATCH,DELETE /api/v1/me/finance/incomes/{income_id}`; `GET,POST /api/v1/me/finance/bills`; `PATCH,DELETE /api/v1/me/finance/bills/{bill_id}`; `GET,PUT /api/v1/me/finance/budgets`; `DELETE /api/v1/me/finance/budgets/{budget_id}`; `GET /api/v1/me/finance/summary` | Global middleware plus DB-user/owner dependencies. |
| Productivity (DB user) | `GET,POST /api/v1/me/tasks`; `GET,PATCH,DELETE /api/v1/me/tasks/{task_id}`; equivalent collection/item routes for `/me/reminders`, `/me/notes`, `/me/checklists`, `/me/habits`, `/me/focus-sessions`; `POST /api/v1/me/reminders/{reminder_id}/snooze`; `POST /api/v1/me/checklists/{checklist_id}/items`; `PATCH,DELETE /api/v1/me/checklists/{checklist_id}/items/{item_id}`; `POST /api/v1/me/habits/{habit_id}/check-ins`; `GET /api/v1/me/agenda`, `/me/today`; `POST /api/v1/me/weekly-plan`, `/me/weekly-plan/accept` | Global middleware plus DB-user/owner dependencies. |
| Medication (DB user) | `GET,POST /api/v1/me/medications`; `GET,PATCH,DELETE /api/v1/me/medications/{medication_id}`; `GET /api/v1/me/medications/adherence`; `POST /api/v1/me/medications/{medication_id}/intakes`; `GET /api/v1/me/medication-intakes`; `DELETE /api/v1/me/medication-intakes/{intake_id}` | Global middleware plus DB-user/owner dependencies. |
| Study (DB user) | `GET,POST /api/v1/me/study/plans`; `GET,PATCH,DELETE /api/v1/me/study/plans/{plan_id}`; `GET,POST /api/v1/me/study/decks`; `GET,PATCH,DELETE /api/v1/me/study/decks/{deck_id}`; `GET,POST /api/v1/me/study/decks/{deck_id}/cards`; `PATCH,DELETE /api/v1/me/study/cards/{card_id}`; `POST /api/v1/me/study/cards/{card_id}/reviews`; `POST /api/v1/me/study/mind-map`, `/me/study/quiz` | Global middleware plus DB-user/plan dependencies. |
| Logbook/privacy/promo (DB user) | `GET,POST /api/v1/me/logs`; `GET,PATCH,DELETE /api/v1/me/logs/{entry_id}`; `GET /api/v1/me/logs/report`; `GET,POST /api/v1/me/memories`; `PATCH,DELETE /api/v1/me/memories/{key}`; `GET /api/v1/me/export`; `GET,PUT /api/v1/me/consents`; `DELETE /api/v1/me/consents/{consent_type}`; `POST /api/v1/me/promo-codes/redeem` | Global middleware plus DB-user/owner dependencies; promo limits can return 429. |
| Agent orchestration/feedback (DB user) | `POST /api/v1/agents/chat`; `POST /api/v1/agents/actions/{action_id}/confirm`, `/reject`, `/result`; `POST /api/v1/ai/feedback`; `POST /api/v1/agents/device/command`; `POST /api/v1/agents/general/chat`; `DELETE /api/v1/agents/general/conversation`; `POST /api/v1/ai/chat`; `DELETE /api/v1/ai/chat/conversation` | Global middleware plus DB-user/ownership/entitlement checks. |
| Other | `GET /api/v1/app-config`; `POST /api/v1/auth/register`, `/login`, `/refresh`, `/logout`; `GET /api/v1/auth/me`; `POST /api/v1/billing/google/rtdn`, `/verify`; `POST /api/v1/cache/flush`; `GET /api/v1/promotions/active`; `GET,POST,PUT,PATCH,DELETE /api/v1/proxy/{service_name}`; `GET,POST /api/v1/system-prompts/`; `GET,PUT,DELETE /api/v1/system-prompts/{prompt_id}`; `GET /api/v1/users/`; `PATCH /api/v1/users/me`, `/users/me/password`; `DELETE /api/v1/users/{user_id}`; `PATCH /api/v1/users/{user_id}/promote`, `/demote` | Public allowlist exceptions bypass JWT; all remaining paths are globally authenticated, plus the route-specific checks described above. |

## Baseline test scope

In-process checks covered root, health, app config, auth register/login/refresh/logout, privacy pages, docs/schema, objectives slash variants, no-token and garbage-token headers, a successful registered-user token, an expired JWT, wrong-password login, and forwarded headers. Redis unavailability prevented a healthy `/health` response. This was the pre-change baseline.

## Post-fix verification (2026-10-23)

Authentication is now enforced through FastAPI dependencies attached when protected v1 routers are included. Auth, app-config, billing (with DB-user dependencies on its user routes and its own Pub/Sub verifier for RTDN), framework docs, and public compliance pages do not inherit a user-auth dependency. Admin login and static assets stay on the panel's separate cookie/IP/CSRF controls. There is no path-based API auth allowlist.

The live OpenAPI registry contains 227 method/path operations. `tests/test_auth_hardening.py::test_every_registered_operation_is_classified_and_auth_guarded` enumerates all of them, explicitly classifies the public API operations, exempts the admin cookie-auth surface, and asserts missing/garbage-token 401s on protected operations. The same suite checks public slash variants, protected slash variants, CORS preflight, forwarded-proto Location headers, invalid/expired tokens, suspension behavior, and distinct trusted-forwarded-IP rate-limit buckets.

The in-process and local-Uvicorn diagnostic completed 52 checks with zero auth failures. `/health` returned 503 because Redis is not available in this environment; the diagnostic reports that as reachable-but-degraded, not as an auth failure. Direct curl confirmed `GET /` returns the root JSON and a protected slashless objectives request returns 401 without a Location header behind `X-Forwarded-Proto: https`.