# NovaIAx API Reference

Reference for the backend HTTP endpoints used by the NovaIAx mobile app and
the admin tooling.  Generated from the running application's OpenAPI schema
(`openapi.json`) plus the conventions enforced across the codebase.

> This document is **read-only documentation**; it does not modify any code.
> The endpoint catalog at the end is generated from `app.openapi()`, so it is
> always in sync with the routers.  Regenerate both files with
> `python scripts/export_openapi.py` (see `README.md`).

---

## 1. Global conventions

### Base URLs

| Area           | Base                 | Format      |
|----------------|----------------------|-------------|
| JSON API (v1)  | `/api/v1`            | `application/json` |
| Health probe   | `/health`            | `application/json` |
| Public pages   | `/privacy`, `/terms`, `/account-deletion` | Server-rendered HTML |
| Admin panel    | `/admin`             | Server-rendered HTML (forms + cookies) |
| Static (admin) | `/admin/static/...`  | CSS assets  |

### Authentication

Two credential schemes exist; which one is accepted depends on the route:

1. **JWT Bearer token** (`Authorization: Bearer <access_token>`) — the
   universal scheme, obtained from `POST /api/v1/auth/register` or
   `POST /api/v1/auth/login`.
   - Access token TTL: **15 minutes**.
   - Refresh token TTL: **7 days**.
   - Only tokens with `type == "access"` are accepted; refresh tokens and
     revoked (logged-out) `jti`s are rejected with `401`.
2. **API key** (`X-API-Key: <key>`) — the master key or a registered hashed
   key (backed by Redis).  Authenticates as a `SERVICE`-role identity.

- **Service-to-service (proxy) endpoints** accept either scheme.
- **User-scoped endpoints** (anything that loads or creates data tied to a
  database user) require a **JWT**.  An API-key identity is rejected with a
  clean `403`.
- The public auth endpoints (`/auth/register`, `/login`, `/refresh`,
  `/logout`) and the billing RTDN push path
  (`POST /api/v1/billing/google/rtdn`, called by Google, not a user) do **not**
  require an auth header.

### Roles

`role` is one of `USER`, `ADMIN`, `SERVICE`.

- In development the **first registered user** in a fresh database becomes
  `ADMIN`; later registrations become `USER`.  This bootstrap is **disabled in
  production** (`FIRST_USER_ADMIN` is forced off when `ENVIRONMENT=production`).
- Admin-only capabilities are noted per endpoint.

### Error envelope (uniform `{detail, code}`)

Every error response is a JSON object with **two** fields:

```json
{ "detail": "<mensagem em pt-BR>", "code": "<stable_machine_code>" }
```

- `detail` is always **pt-BR** and is meant for display.  It may change.
- `code` is **stable** and is what the app must switch on.  It is present on
  *every* error, including plain `HTTPException`s whose code is derived from
  the status.

Generic codes (from `app/exceptions/api_errors.py`):

| HTTP | `code` | Meaning |
|------|--------|---------|
| 400  | `bad_request` | Malformed request that is not a validation error |
| 401  | `authentication_required` | Missing/expired token |
| 401  | `invalid_credentials` | Wrong email or password |
| 403  | `forbidden` | Authenticated but not allowed |
| 404  | `not_found` | Resource does not exist (or is not the caller's) |
| 409  | `conflict` | Unique-constraint / state conflict |
| 413  | `payload_too_large` | Body over the limit |
| 422  | `validation_error` | Request failed schema validation |
| 429  | `rate_limit_exceeded` | Per-IP rate limit hit |
| 500  | `internal_error` | Unhandled server error |
| 502/503 | `service_unavailable` | Dependency unavailable |

AI-provider codes (from `app/exceptions/ai_errors.py`):

| HTTP | `code` | Meaning |
|------|--------|---------|
| 429  | `ai_rate_limited` | Provider rate limited the request |
| 429  | `ai_busy` | Concurrency queue saturated |
| 429  | `ai_quota_exceeded` | The **user's** daily AI budget is spent |
| 504  | `ai_provider_timeout` | Provider did not answer in time |
| 502  | `ai_provider_error` | Provider returned an error |
| 502  | `ai_provider_auth_failed` | Provider rejected the API key |
| 502  | `ai_invalid_response` | Provider answer could not be parsed |
| 502  | `ai_all_providers_failed` | No provider could serve the request |

Plan/billing codes (from `app/exceptions/plan_errors.py`):

| HTTP | `code` | Meaning |
|------|--------|---------|
| 402  | `plan_upgrade_required` | Feature needs a higher plan |
| 402  | `plan_feature_locked` | Feature not included in the plan |
| 402  | `plan_limit_reached` | Plan quota exhausted |
| 403  | `subscription_inactive` | Subscription is not active |
| 503  | `billing_unavailable` | Billing backend not configured |
| 400  | `purchase_invalid` | Purchase verification failed |

### Validation errors

Schema validation failures (`422`) use a **fixed** body:

```json
{ "detail": "Requisição inválida.", "code": "validation_error" }
```

The per-field breakdown is intentionally not exposed; the app should treat any
`422` as a client bug and fix its payload, not parse field errors.

### Pagination

List endpoints that paginate return the same envelope:

```json
{ "items": [ ... ], "page": 1, "limit": 20, "total": 42 }
```

`page` is 1-based; `limit` is capped per endpoint (usually 100).  Endpoints
noted below as returning a bare list/object are the deliberate exceptions kept
for **backward compatibility** with the shipped mobile app.

#### Per-endpoint pagination

Paths are relative to `/api/v1`.  "Envelope" is the shape returned; all
paginated endpoints accept `page` (`ge=1`) and `limit` unless noted.  Filter
params are listed for completeness — they combine with pagination.

| Endpoint | Query params | Default `limit` | Max `limit` | Envelope |
|----------|--------------|-----------------|-------------|----------|
| `GET /objectives/` | `offset`, `limit` | 50 | 100 | **bare array** (legacy) |
| `GET /objectives/{id}/roadmap/days` | — | — | — | `{objective_id, days}` |
| `GET /me/tasks` | `page`, `limit`, `status`, `priority`, `due_from`, `due_to` | 20 | 100 | `{items, page, limit, total}` |
| `GET /me/reminders` | `page`, `limit`, `status`, `from`, `to` | 20 | 100 | `{items, page, limit, total}` |
| `GET /me/notes` | `page`, `limit`, `tag`, `pinned` | 20 | 100 | `{items, page, limit, total}` |
| `GET /me/checklists` | `page`, `limit`, `category` | 20 | 100 | `{items, page, limit, total}` |
| `GET /me/habits` | `page`, `limit` | 20 | 100 | `{items, page, limit, total}` |
| `GET /me/focus-sessions` | `page`, `limit`, `from`, `to` | 20 | 100 | `{items, page, limit, total}` |
| `GET /me/medications` | `page`, `limit`, `active` | 20 | 100 | `{items, page, limit, total}` |
| `GET /me/medication-intakes` | `page`, `limit`, `medication_id`, `status`, `from`, `to` | 20 | 100 | `{items, page, limit, total}` |
| `GET /me/finance/expenses` | `page`, `limit`, `category`, `from`, `to` | 20 | 100 | `{items, page, limit, total}` |
| `GET /me/finance/incomes` | `page`, `limit`, `from`, `to` | 20 | 100 | `{items, page, limit, total}` |
| `GET /me/finance/bills` | `page`, `limit`, `status`, `due_from`, `due_to` | 20 | 100 | `{items, page, limit, total}` |
| `GET /me/finance/budgets` | `month` | — | — | `{month, items}` (one month, unpaginated) |
| `GET /me/study/plans` | `page`, `limit`, `status` | 20 | 100 | `{items, page, limit, total}` |
| `GET /me/study/decks` | `page`, `limit` | 20 | 100 | `{items, page, limit, total}` |
| `GET /me/study/decks/{id}/cards` | `page`, `limit`, `due_only` | 50 | 100 | `{items, page, limit, total}` |
| `GET /me/logs` | `page`, `limit`, `kind`, `from`, `to` | 20 | 100 | `{items, page, limit, total}` |
| `GET /community/reports` | `page`, `limit`, `category` | 20 | 100 | `{items, page, limit, total}` |
| `GET /community/drafts` | `page`, `limit` | 20 | 100 | `{items, page, limit, total}` |
| `GET /community/official-contacts` | `category` | — | — | `{items}` (unpaginated) |
| `GET /community/blocks` | — | — | — | `{items}` (unpaginated) |
| `GET /community/support` | — | — | — | `{items}` (static, unpaginated) |
| `GET /me/consents` | — | — | — | `{consents}` (full log, unpaginated) |
| `GET /agent-groups` | — | — | — | **bare array** |
| `GET /sub-agents` | — | — | — | **bare array** |
| `GET /me/memories` | — | — | — | **bare array** |
| `GET /system-prompts/` | `page`, `limit` | 50 | 100 | `{items, page, limit, total}` |
| `GET /users/` (admin) | `page`, `limit` | 20 | 100 | `{items, page, limit, total}` |

`GET /objectives/` is the **only** list endpoint that uses `offset` instead of
`page` and returns a bare array; treat it specially (§2).  The four bare-array
endpoints (`/objectives/`, `/agent-groups`, `/sub-agents`, `/me/memories`) are
frozen for the shipped app and must not be wrapped in an envelope.

### Timestamps, ids, naming

- Timestamps are **ISO-8601 UTC with a `Z` suffix** (for example
  `2026-01-31T14:05:00Z`).
- All ids are **integers**.
- JSON field names are **`snake_case`**.
- Booleans are real JSON booleans (never `"true"`/`1`).

### Rate limiting

Sliding 60-second window enforced by `RateLimitMiddleware` on every
non-`/health` request:

| Bucket | Limit | Key | Applies to |
|--------|-------|-----|-----------|
| Default | 60 req/min | client IP | everything |
| AI (strict) | 10 req/min | client IP | paths containing `/ai/`, `/agents/general`, `/agents/device`, `/agents/chat`, `/objectives/assistant`, `/objectives/register`, `/roadmap/renew`, `/me/weekly-plan` |
| AI (per user) | 8 req/min | authenticated user id | the same AI paths once a valid JWT is present |
| Auth lockout | 5 failed logins / 15 min | email + client IP | `POST /api/v1/auth/login` |

The per-user AI bucket (`RATE_LIMIT_AI_USER`) is applied instead of the per-IP
one whenever the request carries a valid access token, so one user cannot
exhaust the shared IP allowance.

On exceeding the limit: **429**
`{"detail": "<pt-BR>", "code": "rate_limit_exceeded"}` with
`X-RateLimit-Limit`, `X-RateLimit-Remaining` and `Retry-After` headers.  If
Redis is unreachable the IP limiter falls back to permissive.

---

## 2. Backward-compatibility notes (mobile contract)

| Endpoint | Behavior | Why |
|----------|----------|-----|
| `GET /api/v1/objectives/` | Returns a **bare array**, not `{items,...}` | Shipped app expects an array; documented deviation from the pagination rule |
| `GET /api/v1/objectives/{id}/roadmap/days` | Returns `{objective_id, days}` | Roadmap is fetched as a whole, not paginated |
| All error bodies | Gained `code`; `detail` text changed to pt-BR | Additive: clients reading only `detail` still work |

---

## 3. Endpoint catalog

Grouped by OpenAPI tag.  Every path is relative to the server root; the v1
JSON API is under `/api/v1`.  This whole section is **generated** from
`app.openapi()` by `python scripts/export_openapi.py` — do not edit it by hand.

<!-- BEGIN:endpoint-catalog -->
### `admin`

| Method | Path | Summary | Params | Body | Success |
|--------|------|---------|--------|------|---------|
| `GET` | `/admin/` | Dashboard | — | — | 200 `JSON` |
| `GET` | `/admin/agents` | Agents Page | `tab` (query, string, optional) | — | 200 `JSON` |
| `POST` | `/admin/agents/test` | Agent Preview | — | — | 200 `JSON` |
| `POST` | `/admin/agents/{entity}` | Agent Create | `entity` (path, string, required) | — | 200 `JSON` |
| `GET` | `/admin/agents/{entity}/new` | Agent New Page | `entity` (path, string, required) | — | 200 `JSON` |
| `POST` | `/admin/agents/{entity}/{item_id}` | Agent Update | `entity` (path, string, required), `item_id` (path, integer, required) | — | 200 `JSON` |
| `POST` | `/admin/agents/{entity}/{item_id}/duplicate` | Agent Duplicate | `entity` (path, string, required), `item_id` (path, integer, required) | — | 200 `JSON` |
| `GET` | `/admin/agents/{entity}/{item_id}/edit` | Agent Edit Page | `entity` (path, string, required), `item_id` (path, integer, required) | — | 200 `JSON` |
| `POST` | `/admin/agents/{entity}/{item_id}/rollback` | Agent Rollback | `entity` (path, string, required), `item_id` (path, integer, required) | — | 200 `JSON` |
| `POST` | `/admin/agents/{entity}/{item_id}/toggle` | Agent Toggle | `entity` (path, string, required), `item_id` (path, integer, required) | — | 200 `JSON` |
| `GET` | `/admin/agents/{entity}/{item_id}/versions` | Agent Versions Page | `entity` (path, string, required), `item_id` (path, integer, required) | — | 200 `JSON` |
| `GET` | `/admin/audit` | Audit Page | `page` (query, integer, optional), `action` (query, string, optional), `entity` (query, string, optional), `admin_id` (query, integer, optional) | — | 200 `JSON` |
| `GET` | `/admin/login` | Login Page | — | — | 200 `JSON` |
| `POST` | `/admin/login` | Login Submit | — | — | 200 `JSON` |
| `POST` | `/admin/logout` | Logout | — | — | 200 `JSON` |
| `GET` | `/admin/promotions` | Promotions Page | `tab` (query, string, optional) | — | 200 `JSON` |
| `POST` | `/admin/promotions` | Create Promotion | — | — | 200 `JSON` |
| `POST` | `/admin/promotions/codes` | Create Promo Code | — | — | 200 `JSON` |
| `GET` | `/admin/promotions/codes/export` | Export Promo Codes | — | — | 200 `JSON` |
| `POST` | `/admin/promotions/codes/generate` | Generate Promo Codes | — | — | 200 `JSON` |
| `GET` | `/admin/promotions/codes/{code_id}` | Promo Code Detail Page | `code_id` (path, integer, required) | — | 200 `JSON` |
| `POST` | `/admin/promotions/codes/{code_id}` | Update Promo Code | `code_id` (path, integer, required) | — | 200 `JSON` |
| `POST` | `/admin/promotions/codes/{code_id}/toggle` | Toggle Promo Code | `code_id` (path, integer, required) | — | 200 `JSON` |
| `GET` | `/admin/promotions/grants` | Grants Page | — | — | 200 `JSON` |
| `POST` | `/admin/promotions/grants` | Grant Plan To User | — | — | 200 `JSON` |
| `GET` | `/admin/promotions/new` | Promotion New Page | — | — | 200 `JSON` |
| `POST` | `/admin/promotions/{promotion_id}` | Update Promotion | `promotion_id` (path, integer, required) | — | 200 `JSON` |
| `POST` | `/admin/promotions/{promotion_id}/delete` | Delete Promotion | `promotion_id` (path, integer, required) | — | 200 `JSON` |
| `GET` | `/admin/promotions/{promotion_id}/edit` | Promotion Edit Page | `promotion_id` (path, integer, required) | — | 200 `JSON` |
| `POST` | `/admin/promotions/{promotion_id}/toggle` | Toggle Promotion | `promotion_id` (path, integer, required) | — | 200 `JSON` |
| `GET` | `/admin/reports` | Reports Page | `page` (query, integer, optional), `status` (query, string, optional) | — | 200 `JSON` |
| `POST` | `/admin/reports/{report_id}/status` | Moderate Report | `report_id` (path, integer, required) | — | 200 `JSON` |
| `GET` | `/admin/security/totp` | Totp Page | — | — | 200 `JSON` |
| `POST` | `/admin/security/totp` | Totp Enroll | — | — | 200 `JSON` |
| `POST` | `/admin/security/totp/disable` | Totp Disable | — | — | 200 `JSON` |
| `GET` | `/admin/settings` | Settings Page | `tab` (query, string, optional) | — | 200 `JSON` |
| `POST` | `/admin/settings/ai/{name}` | Update Ai Provider | `name` (path, string, required) | — | 200 `JSON` |
| `POST` | `/admin/settings/app` | Update App Config | — | — | 200 `JSON` |
| `POST` | `/admin/settings/contacts` | Create Official Contact | — | — | 200 `JSON` |
| `POST` | `/admin/settings/contacts/{contact_id}` | Update Official Contact | `contact_id` (path, integer, required) | — | 200 `JSON` |
| `POST` | `/admin/settings/contacts/{contact_id}/delete` | Delete Official Contact | `contact_id` (path, integer, required) | — | 200 `JSON` |
| `POST` | `/admin/settings/limits/{plan}` | Update Plan Limits | `plan` (path, string, required) | — | 200 `JSON` |
| `POST` | `/admin/settings/limits/{plan}/clear` | Clear Plan Limits | `plan` (path, string, required) | — | 200 `JSON` |
| `GET` | `/admin/system-prompts` | Prompts Page | `page` (query, integer, optional) | — | 200 `JSON` |
| `POST` | `/admin/system-prompts` | Create Prompt | — | — | 200 `JSON` |
| `POST` | `/admin/system-prompts/{prompt_id}` | Update Prompt | `prompt_id` (path, integer, required) | — | 200 `JSON` |
| `POST` | `/admin/system-prompts/{prompt_id}/delete` | Delete Prompt | `prompt_id` (path, integer, required) | — | 200 `JSON` |
| `GET` | `/admin/system-prompts/{prompt_id}/edit` | Edit Prompt Page | `prompt_id` (path, integer, required) | — | 200 `JSON` |
| `GET` | `/admin/users` | Users Page | `page` (query, integer, optional), `q` (query, string, optional) | — | 200 `JSON` |
| `GET` | `/admin/users/{user_id}` | User Detail Page | `user_id` (path, integer, required) | — | 200 `JSON` |
| `POST` | `/admin/users/{user_id}/ban` | Ban User Post | `user_id` (path, integer, required) | — | 200 `JSON` |
| `POST` | `/admin/users/{user_id}/delete` | Delete User | `user_id` (path, integer, required) | — | 200 `JSON` |
| `POST` | `/admin/users/{user_id}/demote` | Demote User | `user_id` (path, integer, required) | — | 200 `JSON` |
| `POST` | `/admin/users/{user_id}/force-logout` | Force Logout User | `user_id` (path, integer, required) | — | 200 `JSON` |
| `POST` | `/admin/users/{user_id}/grant` | Grant User Plan | `user_id` (path, integer, required) | — | 200 `JSON` |
| `POST` | `/admin/users/{user_id}/promote` | Promote User | `user_id` (path, integer, required) | — | 200 `JSON` |
| `POST` | `/admin/users/{user_id}/suspend` | Suspend User | `user_id` (path, integer, required) | — | 200 `JSON` |
| `POST` | `/admin/users/{user_id}/unban` | Unban User Post | `user_id` (path, integer, required) | — | 200 `JSON` |
| `POST` | `/admin/users/{user_id}/unsuspend` | Unsuspend User | `user_id` (path, integer, required) | — | 200 `JSON` |

### `agent-personalization`

| Method | Path | Summary | Params | Body | Success |
|--------|------|---------|--------|------|---------|
| `GET` | `/api/v1/agent-groups` | List Agent Groups | — | — | 200 `array[AgentGroupResponse]` |
| `GET` | `/api/v1/me/agent` | Get My Agent | — | — | 200 `UserAgentResponse` |
| `PUT` | `/api/v1/me/agent` | Update My Agent | — | `UserAgentUpdate` | 200 `UserAgentResponse` |
| `PUT` | `/api/v1/me/learning` | Set My Learning | — | `LearningUpdate` | 200 `LearningResponse` |
| `GET` | `/api/v1/me/memories` | List My Memories | — | — | 200 `array[MemoryResponse]` |
| `POST` | `/api/v1/me/memories` | Create My Memory | — | `MemoryCreate` | 201 `MemoryResponse` |
| `DELETE` | `/api/v1/me/memories/{key}` | Delete My Memory | `key` (path, string, required) | — | 204 `(no content)` |
| `PATCH` | `/api/v1/me/memories/{key}` | Update My Memory | `key` (path, string, required) | `MemoryUpdate` | 200 `MemoryResponse` |
| `GET` | `/api/v1/sub-agents` | List Sub Agents | — | — | 200 `array[SubAgentResponse]` |

### `agents`

| Method | Path | Summary | Params | Body | Success |
|--------|------|---------|--------|------|---------|
| `POST` | `/api/v1/agents/actions/{action_id}/confirm` | Confirm Action | `action_id` (path, integer, required) | JSON | 200 `AgentActionResponse` |
| `POST` | `/api/v1/agents/actions/{action_id}/reject` | Reject Action | `action_id` (path, integer, required) | JSON | 200 `AgentActionResponse` |
| `POST` | `/api/v1/agents/actions/{action_id}/result` | Report Action Result | `action_id` (path, integer, required) | `AgentActionReportResultRequest` | 200 `AgentActionResponse` |
| `POST` | `/api/v1/agents/chat` | Agent Chat | — | `AgentChatRequest` | 200 `AgentChatResponse` |
| `POST` | `/api/v1/agents/device/command` | Device Command | — | `DeviceCommandRequest` | 200 `DeviceCommandResponse` |
| `POST` | `/api/v1/agents/general/chat` | General Agent Chat | — | `GeneralAgentRequest` | 200 `GeneralAgentResponse` |
| `DELETE` | `/api/v1/agents/general/conversation` | Clear General Agent Conversation | — | — | 200 `GeneralAgentConversationClearResponse` |

### `ai`

| Method | Path | Summary | Params | Body | Success |
|--------|------|---------|--------|------|---------|
| `POST` | `/api/v1/ai/chat` | Chat Completion | — | `ChatCompletionRequest` | 200 `ChatCompletionResponse` |
| `DELETE` | `/api/v1/ai/chat/conversation` | Clear Chat Conversation | `agent_id` (query, integer, required) | — | 200 `ConversationClearResponse` |
| `POST` | `/api/v1/ai/feedback` | Submit Feedback | — | `AIFeedbackRequest` | 201 `AIFeedbackResponse` |

### `app-config`

| Method | Path | Summary | Params | Body | Success |
|--------|------|---------|--------|------|---------|
| `GET` | `/api/v1/app-config` | Get App Config | — | — | 200 `JSON` |

### `auth`

| Method | Path | Summary | Params | Body | Success |
|--------|------|---------|--------|------|---------|
| `POST` | `/api/v1/auth/login` | Login | — | `LoginRequest` | 200 `AuthResponse` |
| `POST` | `/api/v1/auth/logout` | Logout | — | `LogoutRequest` | 200 `TokenResponse` |
| `GET` | `/api/v1/auth/me` | Me | — | — | 200 `app__schemas__auth__UserResponse` |
| `POST` | `/api/v1/auth/refresh` | Refresh Tokens | — | `RefreshRequest` | 200 `TokenResponse` |
| `POST` | `/api/v1/auth/register` | Register User | — | `RegisterUserRequest` | 201 `AuthResponse` |

### `billing`

| Method | Path | Summary | Params | Body | Success |
|--------|------|---------|--------|------|---------|
| `POST` | `/api/v1/billing/google/rtdn` | Google Rtdn | — | — | 200 `RtdnResponse` |
| `POST` | `/api/v1/billing/google/verify` | Verify Google Purchase | — | `GoogleVerifyRequest` | 200 `SubscriptionResponse` |
| `GET` | `/api/v1/me/subscription` | Get My Subscription | — | — | 200 `SubscriptionResponse` |

### `cache`

| Method | Path | Summary | Params | Body | Success |
|--------|------|---------|--------|------|---------|
| `POST` | `/api/v1/cache/flush` | Flush Cache | `include_conversations` (query, boolean, optional) | — | 200 `CacheFlushResponse` |

### `community`

| Method | Path | Summary | Params | Body | Success |
|--------|------|---------|--------|------|---------|
| `GET` | `/api/v1/community/blocks` | List Blocks | — | — | 200 `BlockListResponse` |
| `POST` | `/api/v1/community/blocks` | Block User | — | `BlockCreate` | 201 `BlockResponse` |
| `DELETE` | `/api/v1/community/blocks/{blocked_id}` | Unblock User | `blocked_id` (path, integer, required) | — | 204 `(no content)` |
| `GET` | `/api/v1/community/drafts` | List Drafts | `page` (query, integer, optional), `limit` (query, integer, optional) | — | 200 `DraftListResponse` |
| `POST` | `/api/v1/community/drafts` | Create Draft | — | `DraftCreate` | 201 `DraftResponse` |
| `POST` | `/api/v1/community/drafts/generate` | Generate Draft | — | `DraftGenerateRequest` | 200 `DraftResponse` |
| `DELETE` | `/api/v1/community/drafts/{draft_id}` | Delete Draft | `draft_id` (path, integer, required) | — | 204 `(no content)` |
| `PATCH` | `/api/v1/community/drafts/{draft_id}` | Update Draft | `draft_id` (path, integer, required) | `DraftUpdate` | 200 `DraftResponse` |
| `GET` | `/api/v1/community/official-contacts` | List Official Contacts | `category` (query, string, optional) | — | 200 `OfficialContactListResponse` |
| `GET` | `/api/v1/community/reports` | List Reports | `page` (query, integer, optional), `limit` (query, integer, optional), `category` (query, string, optional) | — | 200 `ReportListResponse` |
| `POST` | `/api/v1/community/reports` | Create Report | — | `ReportCreate` | 201 `ReportResponse` |
| `DELETE` | `/api/v1/community/reports/{report_id}` | Delete Report | `report_id` (path, integer, required) | — | 204 `(no content)` |
| `GET` | `/api/v1/community/reports/{report_id}` | Get Report | `report_id` (path, integer, required) | — | 200 `ReportResponse` |
| `PATCH` | `/api/v1/community/reports/{report_id}` | Update Report | `report_id` (path, integer, required) | `ReportUpdate` | 200 `ReportResponse` |
| `DELETE` | `/api/v1/community/reports/{report_id}/confirm` | Unconfirm Report | `report_id` (path, integer, required) | — | 200 `ConfirmationResponse` |
| `POST` | `/api/v1/community/reports/{report_id}/confirm` | Confirm Report | `report_id` (path, integer, required) | — | 200 `ConfirmationResponse` |
| `POST` | `/api/v1/community/reports/{report_id}/flag` | Flag Report | `report_id` (path, integer, required) | `FlagCreate` | 200 `FlagResponse` |
| `POST` | `/api/v1/community/reports/{report_id}/photo` | Upload Report Photo | `report_id` (path, integer, required) | — | 200 `ReportResponse` |
| `GET` | `/api/v1/community/support` | Support Resources | — | — | 200 `SupportResourceListResponse` |

### `finance`

| Method | Path | Summary | Params | Body | Success |
|--------|------|---------|--------|------|---------|
| `GET` | `/api/v1/me/finance/bills` | List Bills | `page` (query, integer, optional), `limit` (query, integer, optional), `status` (query, string, optional), `due_from` (query, string, optional), `due_to` (query, string, optional) | — | 200 `BillListResponse` |
| `POST` | `/api/v1/me/finance/bills` | Create Bill | — | `BillCreate` | 201 `BillResponse` |
| `DELETE` | `/api/v1/me/finance/bills/{bill_id}` | Delete Bill | `bill_id` (path, integer, required) | — | 204 `(no content)` |
| `PATCH` | `/api/v1/me/finance/bills/{bill_id}` | Update Bill | `bill_id` (path, integer, required) | `BillUpdate` | 200 `BillResponse` |
| `GET` | `/api/v1/me/finance/budgets` | List Budgets | `month` (query, string, required) | — | 200 `BudgetListResponse` |
| `PUT` | `/api/v1/me/finance/budgets` | Set Budget | `month` (query, string, required) | `BudgetSet` | 200 `BudgetResponse` |
| `DELETE` | `/api/v1/me/finance/budgets/{budget_id}` | Delete Budget | `budget_id` (path, integer, required) | — | 204 `(no content)` |
| `GET` | `/api/v1/me/finance/expenses` | List Expenses | `page` (query, integer, optional), `limit` (query, integer, optional), `category` (query, string, optional), `from` (query, string, optional), `to` (query, string, optional) | — | 200 `ExpenseListResponse` |
| `POST` | `/api/v1/me/finance/expenses` | Create Expense | — | `ExpenseCreate` | 201 `ExpenseResponse` |
| `DELETE` | `/api/v1/me/finance/expenses/{expense_id}` | Delete Expense | `expense_id` (path, integer, required) | — | 204 `(no content)` |
| `PATCH` | `/api/v1/me/finance/expenses/{expense_id}` | Update Expense | `expense_id` (path, integer, required) | `ExpenseUpdate` | 200 `ExpenseResponse` |
| `GET` | `/api/v1/me/finance/incomes` | List Incomes | `page` (query, integer, optional), `limit` (query, integer, optional), `from` (query, string, optional), `to` (query, string, optional) | — | 200 `IncomeListResponse` |
| `POST` | `/api/v1/me/finance/incomes` | Create Income | — | `IncomeCreate` | 201 `IncomeResponse` |
| `DELETE` | `/api/v1/me/finance/incomes/{income_id}` | Delete Income | `income_id` (path, integer, required) | — | 204 `(no content)` |
| `PATCH` | `/api/v1/me/finance/incomes/{income_id}` | Update Income | `income_id` (path, integer, required) | `IncomeUpdate` | 200 `IncomeResponse` |
| `GET` | `/api/v1/me/finance/summary` | Finance Summary | `month` (query, string, required) | — | 200 `FinanceSummaryResponse` |

### `logbook`

| Method | Path | Summary | Params | Body | Success |
|--------|------|---------|--------|------|---------|
| `GET` | `/api/v1/me/logs` | List Entries | `page` (query, integer, optional), `limit` (query, integer, optional), `kind` (query, string, optional), `from` (query, string, optional), `to` (query, string, optional) | — | 200 `LogEntryListResponse` |
| `POST` | `/api/v1/me/logs` | Create Entry | — | `LogEntryCreate` | 201 `LogEntryResponse` |
| `GET` | `/api/v1/me/logs/report` | Log Report | `from` (query, string, optional), `to` (query, string, optional) | — | 200 `LogReportResponse` |
| `DELETE` | `/api/v1/me/logs/{entry_id}` | Delete Entry | `entry_id` (path, integer, required) | — | 204 `(no content)` |
| `GET` | `/api/v1/me/logs/{entry_id}` | Get Entry | `entry_id` (path, integer, required) | — | 200 `LogEntryResponse` |
| `PATCH` | `/api/v1/me/logs/{entry_id}` | Update Entry | `entry_id` (path, integer, required) | `LogEntryUpdate` | 200 `LogEntryResponse` |

### `medication`

| Method | Path | Summary | Params | Body | Success |
|--------|------|---------|--------|------|---------|
| `GET` | `/api/v1/me/medication-intakes` | List Intakes | `page` (query, integer, optional), `limit` (query, integer, optional), `medication_id` (query, integer, optional), `status` (query, string, optional), `from` (query, string, optional), `to` (query, string, optional) | — | 200 `IntakeListResponse` |
| `DELETE` | `/api/v1/me/medication-intakes/{intake_id}` | Delete Intake | `intake_id` (path, integer, required) | — | 204 `(no content)` |
| `GET` | `/api/v1/me/medications` | List Medications | `page` (query, integer, optional), `limit` (query, integer, optional), `active` (query, boolean, optional) | — | 200 `MedicationListResponse` |
| `POST` | `/api/v1/me/medications` | Create Medication | — | `MedicationCreate` | 201 `MedicationResponse` |
| `GET` | `/api/v1/me/medications/adherence` | Medication Adherence | `days` (query, integer, optional) | — | 200 `AdherenceResponse` |
| `DELETE` | `/api/v1/me/medications/{medication_id}` | Delete Medication | `medication_id` (path, integer, required) | — | 204 `(no content)` |
| `GET` | `/api/v1/me/medications/{medication_id}` | Get Medication | `medication_id` (path, integer, required) | — | 200 `MedicationResponse` |
| `PATCH` | `/api/v1/me/medications/{medication_id}` | Update Medication | `medication_id` (path, integer, required) | `MedicationUpdate` | 200 `MedicationResponse` |
| `POST` | `/api/v1/me/medications/{medication_id}/intakes` | Log Intake | `medication_id` (path, integer, required) | `IntakeCreate` | 201 `IntakeResponse` |

### `objectives`

| Method | Path | Summary | Params | Body | Success |
|--------|------|---------|--------|------|---------|
| `GET` | `/api/v1/objectives/` | List Objectives | `offset` (query, integer, optional), `limit` (query, integer, optional) | — | 200 `array[ObjectiveResponse]` |
| `POST` | `/api/v1/objectives/assistant` | Objective Assistant | — | `ObjectiveAssistantRequest` | 200 `ObjectiveAssistantResponse` |
| `POST` | `/api/v1/objectives/register` | Register Objective | — | `RegisterObjectiveRequest` | 200 `ObjectiveResponse` |
| `GET` | `/api/v1/objectives/{objective_id}/roadmap/days` | List Roadmap Days | `objective_id` (path, integer, required) | — | 200 `RoadmapDayListResponse` |
| `DELETE` | `/api/v1/objectives/{objective_id}/roadmap/days/{day_id}` | Delete Roadmap Day | `objective_id` (path, integer, required), `day_id` (path, integer, required) | — | 204 `(no content)` |
| `PATCH` | `/api/v1/objectives/{objective_id}/roadmap/days/{day_id}` | Update Roadmap Day Status | `objective_id` (path, integer, required), `day_id` (path, integer, required) | `RoadmapDayStatusUpdate` | 200 `RoadmapDayResponse` |
| `POST` | `/api/v1/objectives/{objective_id}/roadmap/renew` | Renew Objective Roadmap | `objective_id` (path, integer, required) | — | 200 `ObjectiveResponse` |

### `other`

| Method | Path | Summary | Params | Body | Success |
|--------|------|---------|--------|------|---------|
| `GET` | `/health` | Health | — | — | 200 `JSON` |

### `privacy`

| Method | Path | Summary | Params | Body | Success |
|--------|------|---------|--------|------|---------|
| `DELETE` | `/api/v1/me` | Delete My Account | — | `AccountDeletionRequest` | 200 `AccountDeletionResponse` |
| `GET` | `/api/v1/me/consents` | List Consents | — | — | 200 `ConsentListResponse` |
| `PUT` | `/api/v1/me/consents` | Update Consent | — | `ConsentUpdateRequest` | 200 `ConsentRecordSchema` |
| `DELETE` | `/api/v1/me/consents/{consent_type}` | Withdraw Consent | `consent_type` (path, string, required) | — | 200 `ConsentWithdrawalResponse` |
| `GET` | `/api/v1/me/export` | Export My Data | — | — | 200 `DataExportResponse` |

### `productivity`

| Method | Path | Summary | Params | Body | Success |
|--------|------|---------|--------|------|---------|
| `GET` | `/api/v1/me/agenda` | Get Agenda | `from` (query, string, optional), `to` (query, string, optional) | — | 200 `AgendaResponse` |
| `GET` | `/api/v1/me/checklists` | List Checklists | `page` (query, integer, optional), `limit` (query, integer, optional), `category` (query, string, optional) | — | 200 `ChecklistListResponse` |
| `POST` | `/api/v1/me/checklists` | Create Checklist | — | `ChecklistCreate` | 201 `ChecklistResponse` |
| `DELETE` | `/api/v1/me/checklists/{checklist_id}` | Delete Checklist | `checklist_id` (path, integer, required) | — | 204 `(no content)` |
| `GET` | `/api/v1/me/checklists/{checklist_id}` | Get Checklist | `checklist_id` (path, integer, required) | — | 200 `ChecklistResponse` |
| `PATCH` | `/api/v1/me/checklists/{checklist_id}` | Update Checklist | `checklist_id` (path, integer, required) | `ChecklistUpdate` | 200 `ChecklistResponse` |
| `POST` | `/api/v1/me/checklists/{checklist_id}/items` | Add Checklist Item | `checklist_id` (path, integer, required) | `ChecklistItemCreate` | 201 `ChecklistItemResponse` |
| `DELETE` | `/api/v1/me/checklists/{checklist_id}/items/{item_id}` | Delete Checklist Item | `checklist_id` (path, integer, required), `item_id` (path, integer, required) | — | 204 `(no content)` |
| `PATCH` | `/api/v1/me/checklists/{checklist_id}/items/{item_id}` | Update Checklist Item | `checklist_id` (path, integer, required), `item_id` (path, integer, required) | `ChecklistItemUpdate` | 200 `ChecklistItemResponse` |
| `GET` | `/api/v1/me/focus-sessions` | List Focus Sessions | `page` (query, integer, optional), `limit` (query, integer, optional), `from` (query, string, optional), `to` (query, string, optional) | — | 200 `FocusSessionListResponse` |
| `POST` | `/api/v1/me/focus-sessions` | Create Focus Session | — | `FocusSessionCreate` | 201 `FocusSessionResponse` |
| `DELETE` | `/api/v1/me/focus-sessions/{session_id}` | Delete Focus Session | `session_id` (path, integer, required) | — | 204 `(no content)` |
| `PATCH` | `/api/v1/me/focus-sessions/{session_id}` | Update Focus Session | `session_id` (path, integer, required) | `FocusSessionUpdate` | 200 `FocusSessionResponse` |
| `GET` | `/api/v1/me/habits` | List Habits | `page` (query, integer, optional), `limit` (query, integer, optional) | — | 200 `HabitListResponse` |
| `POST` | `/api/v1/me/habits` | Create Habit | — | `HabitCreate` | 201 `HabitResponse` |
| `DELETE` | `/api/v1/me/habits/{habit_id}` | Delete Habit | `habit_id` (path, integer, required) | — | 204 `(no content)` |
| `GET` | `/api/v1/me/habits/{habit_id}` | Get Habit | `habit_id` (path, integer, required) | — | 200 `HabitResponse` |
| `POST` | `/api/v1/me/habits/{habit_id}/check-ins` | Check In Habit | `habit_id` (path, integer, required) | JSON | 200 `HabitResponse` |
| `GET` | `/api/v1/me/notes` | List Notes | `page` (query, integer, optional), `limit` (query, integer, optional), `tag` (query, string, optional), `pinned` (query, boolean, optional) | — | 200 `NoteListResponse` |
| `POST` | `/api/v1/me/notes` | Create Note | — | `NoteCreate` | 201 `NoteResponse` |
| `DELETE` | `/api/v1/me/notes/{note_id}` | Delete Note | `note_id` (path, integer, required) | — | 204 `(no content)` |
| `GET` | `/api/v1/me/notes/{note_id}` | Get Note | `note_id` (path, integer, required) | — | 200 `NoteResponse` |
| `PATCH` | `/api/v1/me/notes/{note_id}` | Update Note | `note_id` (path, integer, required) | `NoteUpdate` | 200 `NoteResponse` |
| `GET` | `/api/v1/me/reminders` | List Reminders | `page` (query, integer, optional), `limit` (query, integer, optional), `status` (query, string, optional), `from` (query, string, optional), `to` (query, string, optional) | — | 200 `ReminderListResponse` |
| `POST` | `/api/v1/me/reminders` | Create Reminder | — | `ReminderCreate` | 201 `ReminderResponse` |
| `DELETE` | `/api/v1/me/reminders/{reminder_id}` | Delete Reminder | `reminder_id` (path, integer, required) | — | 204 `(no content)` |
| `PATCH` | `/api/v1/me/reminders/{reminder_id}` | Update Reminder | `reminder_id` (path, integer, required) | `ReminderUpdate` | 200 `ReminderResponse` |
| `POST` | `/api/v1/me/reminders/{reminder_id}/snooze` | Snooze Reminder | `reminder_id` (path, integer, required) | `ReminderSnooze` | 200 `ReminderResponse` |
| `GET` | `/api/v1/me/tasks` | List Tasks | `page` (query, integer, optional), `limit` (query, integer, optional), `status` (query, string, optional), `priority` (query, string, optional), `due_from` (query, string, optional), `due_to` (query, string, optional) | — | 200 `TaskListResponse` |
| `POST` | `/api/v1/me/tasks` | Create Task | — | `TaskCreate` | 201 `TaskResponse` |
| `DELETE` | `/api/v1/me/tasks/{task_id}` | Delete Task | `task_id` (path, integer, required) | — | 204 `(no content)` |
| `GET` | `/api/v1/me/tasks/{task_id}` | Get Task | `task_id` (path, integer, required) | — | 200 `TaskResponse` |
| `PATCH` | `/api/v1/me/tasks/{task_id}` | Update Task | `task_id` (path, integer, required) | `TaskUpdate` | 200 `TaskResponse` |
| `GET` | `/api/v1/me/today` | Get Today | — | — | 200 `TodayResponse` |
| `POST` | `/api/v1/me/weekly-plan` | Generate Weekly Plan | — | `WeeklyPlanRequest` | 200 `WeeklyPlanResponse` |
| `POST` | `/api/v1/me/weekly-plan/accept` | Accept Weekly Plan | — | — | 200 `WeeklyPlanAcceptResponse` |

### `promotions`

| Method | Path | Summary | Params | Body | Success |
|--------|------|---------|--------|------|---------|
| `POST` | `/api/v1/me/promo-codes/redeem` | Redeem Promo Code | — | `PromoRedeemRequest` | 201 `PromoRedeemResponse` |
| `GET` | `/api/v1/promotions/active` | List Active Promotions | — | — | 200 `array[PromotionResponse]` |

### `proxy`

| Method | Path | Summary | Params | Body | Success |
|--------|------|---------|--------|------|---------|
| `DELETE` | `/api/v1/proxy/{service_name}` | Proxy Request | `service_name` (path, string, required) | — | 200 `JSON` |
| `GET` | `/api/v1/proxy/{service_name}` | Proxy Request | `service_name` (path, string, required) | — | 200 `JSON` |
| `PATCH` | `/api/v1/proxy/{service_name}` | Proxy Request | `service_name` (path, string, required) | — | 200 `JSON` |
| `POST` | `/api/v1/proxy/{service_name}` | Proxy Request | `service_name` (path, string, required) | — | 200 `JSON` |
| `PUT` | `/api/v1/proxy/{service_name}` | Proxy Request | `service_name` (path, string, required) | — | 200 `JSON` |

### `public`

| Method | Path | Summary | Params | Body | Success |
|--------|------|---------|--------|------|---------|
| `GET` | `/account-deletion` | Account Deletion Page | `error` (query, string, optional), `message` (query, string, optional) | — | 200 `JSON` |
| `POST` | `/account-deletion/request` | Account Deletion Request | — | — | 200 `JSON` |
| `GET` | `/privacy` | Privacy Page | — | — | 200 `JSON` |
| `GET` | `/terms` | Terms Page | — | — | 200 `JSON` |

### `study`

| Method | Path | Summary | Params | Body | Success |
|--------|------|---------|--------|------|---------|
| `DELETE` | `/api/v1/me/study/cards/{card_id}` | Delete Card | `card_id` (path, integer, required) | — | 204 `(no content)` |
| `PATCH` | `/api/v1/me/study/cards/{card_id}` | Update Card | `card_id` (path, integer, required) | `CardUpdate` | 200 `CardResponse` |
| `POST` | `/api/v1/me/study/cards/{card_id}/reviews` | Review Card | `card_id` (path, integer, required) | `ReviewCreate` | 200 `CardResponse` |
| `GET` | `/api/v1/me/study/decks` | List Decks | `page` (query, integer, optional), `limit` (query, integer, optional) | — | 200 `DeckListResponse` |
| `POST` | `/api/v1/me/study/decks` | Create Deck | — | `DeckCreate` | 201 `DeckResponse` |
| `DELETE` | `/api/v1/me/study/decks/{deck_id}` | Delete Deck | `deck_id` (path, integer, required) | — | 204 `(no content)` |
| `GET` | `/api/v1/me/study/decks/{deck_id}` | Get Deck | `deck_id` (path, integer, required) | — | 200 `DeckResponse` |
| `PATCH` | `/api/v1/me/study/decks/{deck_id}` | Update Deck | `deck_id` (path, integer, required) | `DeckUpdate` | 200 `DeckResponse` |
| `GET` | `/api/v1/me/study/decks/{deck_id}/cards` | List Cards | `deck_id` (path, integer, required), `page` (query, integer, optional), `limit` (query, integer, optional), `due_only` (query, boolean, optional) | — | 200 `CardListResponse` |
| `POST` | `/api/v1/me/study/decks/{deck_id}/cards` | Add Card | `deck_id` (path, integer, required) | `CardCreate` | 201 `CardResponse` |
| `POST` | `/api/v1/me/study/mind-map` | Generate Mind Map | — | `MindMapRequest` | 200 `MindMapResponse` |
| `GET` | `/api/v1/me/study/plans` | List Plans | `page` (query, integer, optional), `limit` (query, integer, optional), `status` (query, string, optional) | — | 200 `StudyPlanListResponse` |
| `POST` | `/api/v1/me/study/plans` | Create Plan | — | `StudyPlanCreate` | 201 `StudyPlanResponse` |
| `DELETE` | `/api/v1/me/study/plans/{plan_id}` | Delete Plan | `plan_id` (path, integer, required) | — | 204 `(no content)` |
| `GET` | `/api/v1/me/study/plans/{plan_id}` | Get Plan | `plan_id` (path, integer, required) | — | 200 `StudyPlanResponse` |
| `PATCH` | `/api/v1/me/study/plans/{plan_id}` | Update Plan | `plan_id` (path, integer, required) | `StudyPlanUpdate` | 200 `StudyPlanResponse` |
| `POST` | `/api/v1/me/study/quiz` | Generate Quiz | — | `QuizRequest` | 200 `QuizResponse` |

### `system-prompts`

| Method | Path | Summary | Params | Body | Success |
|--------|------|---------|--------|------|---------|
| `GET` | `/api/v1/system-prompts/` | List System Prompts | `page` (query, integer, optional), `limit` (query, integer, optional) | — | 200 `JSON` |
| `POST` | `/api/v1/system-prompts/` | Create System Prompt | — | `SystemPromptRequest` | 201 `SystemPromptResponse` |
| `DELETE` | `/api/v1/system-prompts/{prompt_id}` | Delete System Prompt | `prompt_id` (path, integer, required) | — | 204 `(no content)` |
| `GET` | `/api/v1/system-prompts/{prompt_id}` | Get System Prompt | `prompt_id` (path, integer, required) | — | 200 `JSON` |
| `PUT` | `/api/v1/system-prompts/{prompt_id}` | Update System Prompt | `prompt_id` (path, integer, required) | `SystemPromptRequest` | 200 `SystemPromptResponse` |

### `users`

| Method | Path | Summary | Params | Body | Success |
|--------|------|---------|--------|------|---------|
| `GET` | `/api/v1/users/` | List Users | `page` (query, integer, optional), `limit` (query, integer, optional) | — | 200 `UserListResponse` |
| `PATCH` | `/api/v1/users/me` | Update My Profile | — | `UserProfileUpdate` | 200 `UserProfileResponse` |
| `PATCH` | `/api/v1/users/me/password` | Change My Password | — | `PasswordChangeRequest` | 200 `ActionResponse` |
| `DELETE` | `/api/v1/users/{user_id}` | Delete User | `user_id` (path, integer, required) | — | 200 `ActionResponse` |
| `PATCH` | `/api/v1/users/{user_id}/demote` | Demote User | `user_id` (path, integer, required) | — | 200 `ActionResponse` |
| `PATCH` | `/api/v1/users/{user_id}/promote` | Promote User | `user_id` (path, integer, required) | — | 200 `ActionResponse` |
<!-- END:endpoint-catalog -->

---

## 4. AI request budget (per-plan daily quota)

AI endpoints charge the caller's daily budget keyed by `plan_tier` (`free`, `trial`, `starter`, `pro`, `premium`).  The limits live in `app/clients/plan_limits.py`.  When the budget is spent the response is `429 {"detail": "<pt-BR>", "code": "ai_quota_exceeded"}`.  A `plan_upgrade_required` (402) means the endpoint itself is not available on the caller's plan.

