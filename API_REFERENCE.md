# NovaIAx API Reference

Reference for the backend HTTP endpoints used by the NovaIAx mobile app and
admin tooling. This document is **read-only documentation** — it does not
modify any code.

---

## 1. Global conventions

### Base URLs

| Area            | Base                    | Format       |
|-----------------|-------------------------|--------------|
| JSON API (v1)   | `/api/v1`               | `application/json` |
| Admin panel     | `/admin`                | Server-rendered HTML (forms + cookies) |
| Static (admin)  | `/admin/static/...`     | CSS assets    |

### Authentication

Two credential schemes exist; which one is accepted depends on the route:

1. **JWT Bearer token** (`Authorization: Bearer <access_token>`) — the
   universal scheme, obtained from `POST /api/v1/auth/register` or
   `POST /api/v1/auth/login`.
   - Access token TTL: **15 minutes** (`access_token_ttl_minutes`).
   - Refresh token TTL: **7 days** (`refresh_token_ttl_days`).
   - Only tokens with `type == "access"` are accepted; refresh tokens and
     revoked (logged-out) `jti`s are rejected with `401`.
2. **API key** (`X-API-Key: <key>`) — master key or a registered hashed key
   (backed by Redis). Authenticates as a `SERVICE`-role identity.

- **Service-to-service (proxy) endpoints** accept either scheme.
- **User-scoped endpoints** (anything that loads or creates data tied to a
  database user — `/auth/me`, `/objectives/*`, roadmap days, the assistant,
  agents and chat) require a **JWT**. An API-key identity is rejected with a
  clean `403` `{"detail": "API-key authentication cannot be used on this endpoint"}`
  instead of crashing.

The public auth endpoints (`/api/v1/auth/register`, `/login`, `/refresh`,
`/logout`) do **not** require any auth header. The admin panel uses its own
httpOnly session cookie instead of headers.

### Roles

`role` is one of: `USER`, `ADMIN`, `SERVICE`.

- The **first registered user** in a fresh database becomes `ADMIN`; all
  later registrations become `USER`.
- `USER`-only vs `ADMIN`-only capabilities are noted per endpoint.

### Rate limiting

Sliding 60-second window, keyed by client IP, enforced by
`RateLimitMiddleware` on **every** non-`/health` request:

| Bucket | Limit | Applies to paths containing |
|--------|-------|------------------------------|
| Default | 60 req/min | everything |
| AI (strict) | 10 req/min | `/ai/`, `/agents/general`, or `/objectives/assistant` (includes `POST /api/v1/ai/chat`, `POST /api/v1/agents/general/chat`, `POST /api/v1/objectives/assistant`, and `/api/v1/proxy/ai/...`) |

On exceeding the limit:

- **429** — `{"detail": "Too Many Requests"}` with `X-RateLimit-Limit`,
  `X-RateLimit-Remaining` and `Retry-After: 60` headers.
- If Redis is unreachable the limiter falls back to permissive (does not block).

### Error response shape

All JSON errors use the same envelope: **`{"detail": "<message>"}`**.

| Status | When | Shape |
|--------|------|-------|
| 401 | Missing/invalid/revoked credentials | `{"detail": "Authentication required"}` / `{"detail": "Invalid token"}` / `{"detail": "Invalid token type"}` / `{"detail": "Token has been revoked"}` |
| 403 | Authenticated but not allowed | `{"detail": "Not authorized"}` |
| 422 | Request body fails Pydantic validation | `{"detail": "Invalid request"}` (details intentionally hidden) |
| 429 | Rate limit exceeded | `{"detail": "Too Many Requests"}` |
| 5xx | Unhandled error | `{"detail": "Internal server error"}` |

### Date/time

Datetimes are ISO-8601 strings (e.g. `"2026-09-18T12:00:00Z"`), timezone-aware.

---

## 2. Auth

Base: `/api/v1/auth` · All JSON · No auth header required (except `/me`).

### 2.1 POST `/api/v1/auth/register`

Registers a new user; the first user ever registered becomes `ADMIN`.

**Request body** (required fields marked *):

| Field      | Type   | Required | Constraints |
|------------|--------|----------|-------------|
| `full_name`| string | *        | 2–120 chars |
| `email`    | string | *        | Valid email |
| `password` | string | *        | 8–128 chars; must contain ≥1 digit and ≥1 uppercase letter |

**Success — `201`**

```json
{
  "user":        { "id": 1, "full_name": "Ana", "email": "ana@x.com", "role": "USER" },
  "access_token": "<jwt>",
  "refresh_token": "<jwt>"
}
```

| Field | Type |
|-------|------|
| `user.id` | int |
| `user.full_name` | string |
| `user.email` | string |
| `user.role` | string (`"USER"`/`"ADMIN"`/`"SERVICE"`) |
| `access_token` | string (JWT, 15 min) |
| `refresh_token` | string (JWT, 7 days) |

**Errors**

| Status | JSON | When |
|--------|------|------|
| 409 | `{"detail": "User already exists"}` | Email already registered |
| 422 | `{"detail": "Invalid request"}` | Password rules/validation failed |

### 2.2 POST `/api/v1/auth/login`

Authenticates and returns a fresh token pair.

**Request body**

| Field | Type   | Required | Notes |
|-------|--------|----------|-------|
| `email` | string | * | Valid email |
| `password` | string | * | ≥1 char |

**Success — `200`** — same `AuthResponse` shape as 2.1.

**Errors**

| Status | JSON | When |
|--------|------|------|
| 401 | `{"detail": "Invalid credentials"}` | Wrong email or password |
| 422 | `{"detail": "Invalid request"}` | Schema validation |

### 2.3 POST `/api/v1/auth/refresh`

Exchanges a refresh token for a new access/refresh pair.

> **Token rotation (single-use refresh):** the presented refresh token is
> revoked — its `jti` is added to the denylist for its remaining TTL — and a
> fresh pair is issued. Reusing a previously-refreshed token returns `401`.

**Request body**

| Field | Type   | Required |
|-------|--------|----------|
| `refresh_token` | string | * |

**Success — `200`**

```json
{ "access_token": "<jwt>", "refresh_token": "<jwt>" }
```

**Errors**

| Status | JSON | When |
|--------|------|------|
| 401 | `{"detail": "Invalid refresh token"}` | Missing/non-`refresh`/expired/revoked token |
| 422 | `{"detail": "Invalid request"}` | Empty `refresh_token` |

### 2.4 POST `/api/v1/auth/logout`

Revokes the presented refresh token — and, when supplied, the access token —
by adding their `jti`s to a Redis denylist for the tokens' remaining TTL.

**Request body**

| Field | Type   | Required |
|-------|--------|----------|
| `refresh_token` | string | * |
| `access_token` | string | no (when supplied, revokes the access `jti` too) |

**Success — `200`**

```json
{ "access_token": "", "refresh_token": "" }
```

**Errors**

| Status | JSON | When |
|--------|------|------|
| 401 | `{"detail": "Invalid refresh token"}` | Token invalid/unparsable |
| 422 | `{"detail": "Invalid request"}` | Empty `refresh_token` |

### 2.5 GET `/api/v1/auth/me`

Returns the identity of the authenticated user (from the token).

**Required headers:** `Authorization: Bearer <access_token>`. An `X-API-Key`
identity is rejected with `403` (see §1 Authentication).

**Success — `200`**

```json
{ "id": 1, "full_name": "Ana", "email": "ana@x.com", "role": "USER" }
```

| Field | Type |
|-------|------|
| `id` | int |
| `full_name` | string |
| `email` | string |
| `role` | string |

**Errors**

| Status | JSON | When |
|--------|------|------|
| 401 | `{"detail": "Authentication required"}` / `{"detail": "Invalid token"}` | No credentials / bad token |
| 403 | `{"detail": "API-key authentication cannot be used on this endpoint"}` | Authenticated with `X-API-Key` instead of a JWT |
| 429 | `{"detail": "Too Many Requests"}` | Rate limit |

---

## 3. Objectives (Goals)

Base: `/api/v1/objectives` · All JSON · All require auth.

**Shared types**

`ObjectiveResponse`:

| Field | Type | Notes |
|-------|------|-------|
| `id` | int | |
| `title` | string | |
| `description` | string \| null | |
| `roadmap` | string \| null | AI-generated markdown roadmap for the current 7-day window |
| `roadmap_updated_at` | datetime \| null | Last roadmap generation time |
| `due_date` | datetime | |
| `user_id` | int | Owner id |
| `days` | array of `RoadmapDayResponse` | Ordered by `day_number` |

`RoadmapDayResponse`:

| Field | Type | Notes |
|-------|------|-------|
| `id` | int | |
| `objective_id` | int | |
| `day_number` | int | 1..N inside the 7-day window |
| `day_date` | datetime | Date the day refers to |
| `content` | string \| null | Day's minimum objective (AI-generated) |
| `status` | string | `"PENDING"` \| `"COMPLETED"` \| `"SKIPPED"` |
| `completed_at` | datetime \| null | |

Days are returned ordered by `day_date`, then `day_number`.

### 3.1 GET `/api/v1/objectives/`

Lists the authenticated user's objectives (ordered by ID ascending, with
their days).

**Required headers:** auth.

**Query parameters**

| Param | Type | Required | Default | Constraints |
|-------|------|----------|---------|-------------|
| `offset` | int | no | 0 | ≥0 |
| `limit` | int | no | 50 | 1–100 |

**Success — `200`** — JSON **array** of `ObjectiveResponse`:

```json
[
  {
    "id": 5,
    "title": "Learn English",
    "description": "Conversational fluency",
    "roadmap": "# Week 1\n...",
    "roadmap_updated_at": "2026-09-18T10:00:00Z",
    "due_date": "2026-10-30T00:00:00Z",
    "user_id": 1,
    "days": [
      { "id": 12, "objective_id": 5, "day_number": 1,
        "day_date": "2026-09-18T00:00:00Z", "content": "Basic vocabulary", "status": "PENDING", "completed_at": null }
    ]
  }
]
```

### 3.2 POST `/api/v1/objectives/register`

Creates an objective and immediately generates its first 7-day roadmap
(via the AI provider). Synonymous with a "create goal" action.

**Required headers:** auth.

**Request body**

| Field | Type   | Required | Constraints |
|-------|--------|----------|-------------|
| `title` | string | * | 2–255 chars |
| `description` | string | no | ≤1000 chars (`null` ok) |
| `due_date` | datetime | * | ISO-8601, must not be in the past |

**Success — `200`** — `ObjectiveResponse` (same shape as 3.1, single object).

**Errors**

| Status | JSON | When |
|--------|------|------|
| 400 | `{"detail": "due_date cannot be in the past"}` | `due_date` before now |
| 401 | `{"detail": "Authentication required"}` | No credentials |
| 504 | `{"detail": "AI provider request timed out"}` | AI generation timed out |
| 502 | `{"detail": "Failed to communicate with AI provider"}` / `{"detail": "AI provider returned an error"}` / etc. | AI provider failure |

### 3.3 POST `/api/v1/objectives/{objective_id}/roadmap/renew`

Generates the **next** 7-day roadmap window. Only callable once 7 days have
elapsed since the last generation (context carried from the previous window).

**Path params:** `objective_id` (int, ≥1).

**Required headers:** auth (owner **or** `ADMIN`).

**Request body:** none.

**Success — `200`** — `ObjectiveResponse` with a new `roadmap` and fresh `days`.

**Errors**

| Status | JSON | When |
|--------|------|------|
| 404 | `{"detail": "Objective not found"}` | Objective doesn't exist |
| 403 | `{"detail": "Not authorized"}` | Not the owner and not `ADMIN` |
| 409 | `{"detail": "Roadmap can only be renewed every 7 days. Available after <iso>"}` | Window not elapsed yet |
| 400 | `{"detail": "Objective already expired"}` | `due_date` passed |
| 502 / 504 | AI provider failure | See 3.2 |

---

## 4. Roadmap Days

Base: `/api/v1/objectives` · All JSON · Owner or `ADMIN` only.

### 4.1 GET `/api/v1/objectives/{objective_id}/roadmap/days`

Lists all roadmap days of an objective.

**Path params:** `objective_id` (int, ≥1). **Auth:** owner or `ADMIN`.

**Success — `200`**

```json
{
  "objective_id": 5,
  "days": [
    { "id": 12, "objective_id": 5, "day_number": 1,
      "day_date": "2026-09-18T00:00:00Z", "content": "Basic vocabulary", "status": "PENDING", "completed_at": null }
  ]
}
```

| Field | Type |
|-------|------|
| `objective_id` | int |
| `days` | array of `RoadmapDayResponse` |

**Errors**

| Status | JSON | When |
|--------|------|------|
| 404 | `{"detail": "Objective not found"}` | Objective missing |
| 403 | `{"detail": "Not authorized"}` | Not owner / not `ADMIN` |
| 401 | `{"detail": "Authentication required"}` | No credentials |

### 4.2 PATCH `/api/v1/objectives/{objective_id}/roadmap/days/{day_id}`

Updates a roadmap day's status (e.g. mark a day as fulfilled / skipped / pending).

**Path params:** `objective_id` (int, ≥1), `day_id` (int, ≥1).

**Auth:** owner or `ADMIN`.

**Request body**

| Field | Type   | Required | Values |
|-------|--------|----------|--------|
| `status` | string | * | `"PENDING"` \| `"COMPLETED"` \| `"SKIPPED"` |

**Success — `200`** — single `RoadmapDayResponse`:

```json
{ "id": 12, "objective_id": 5, "day_number": 1,
  "day_date": "2026-09-18T00:00:00Z", "content": "Basic vocabulary", "status": "COMPLETED", "completed_at": "2026-09-18T12:05:00Z" }
```

**Errors**

| Status | JSON | When |
|--------|------|------|
| 404 | `{"detail": "Objective not found"}` / `{"detail": "Roadmap day not found"}` | Objective or day missing |
| 403 | `{"detail": "Not authorized"}` | Not owner / not `ADMIN` |
| 422 | `{"detail": "Invalid request"}` | Invalid status value |

---

## 5. Objective Assistant (guided goal creation)

Base: `/api/v1/objectives/assistant` · JSON · Auth required.

### 5.1 POST `/api/v1/objectives/assistant`

Multi-turn conversational assistant that collects goal details and eventually
persists the objective. Conversation history is kept server-side per user in a
cache; it is cleared automatically once the objective is created.

**Required headers:** auth. Subject to the **AI (strict)** rate limit
(10 req/min).

**Request body**

| Field | Type   | Required | Constraints |
|-------|--------|----------|-------------|
| `user_message` | string | * | 1–5000 chars |

**Success — `200`**

```json
{ "assistant_message": "Ok, para que data você precisa concluir?" }
```

| Field | Type |
|-------|------|
| `assistant_message` | string |

**Errors**

| Status | JSON | When |
|--------|------|------|
| 400 | `{"detail": "due_date cannot be in the past"}` | AI proposed a past due date |
| 422 | `{"detail": "Invalid request"}` or `{"detail": "user_message must be a non-empty string"}` | Validation |
| 502 / 504 | AI provider failure | See section 3.2 |

---

## 6. Chat / Agents

### 6.1 POST `/api/v1/ai/chat` — generic chat completion

Single-turn chat completion against a configured "agent" (a row in the
`SystemPrompt` table referenced by `agent_id`). Responses are cached for
5 minutes (`cache_ttl_ai`).

**Required headers:** auth. **AI rate limit** (10 req/min).

**Request body**

| Field | Type   | Required | Constraints |
|-------|--------|----------|-------------|
| `agent_id` | int | * | >0; must exist in `SystemPrompt` |
| `user_message` | string | * | 1–5000 chars |

**Success — `200`**

```json
{ "assistant_message": "Resposta do assistente..." }
```

| Field | Type |
|-------|------|
| `assistant_message` | string |

**Errors**

| Status | JSON | When |
|--------|------|------|
| 404 | `{"detail": "Agent not found"}` | `agent_id` doesn't match a system prompt |
| 422 | `{"detail": "Invalid request"}` | Schema validation |
| 502 / 504 | AI provider failure | See section 3.2 |

### 6.2 POST `/api/v1/agents/general/chat` — general agent

Conversational assistant with memory (cached per user) that consults the
user's objectives + roadmap day statuses and reports progress.

**Required headers:** auth. **AI rate limit** (10 req/min).

**Request body**

| Field | Type   | Required | Constraints |
|-------|--------|----------|-------------|
| `user_message` | string | * | 1–5000 chars |

**Success — `200`**

```json
{ "assistant_message": "Você completou 3 de 7 dias esta semana." }
```

**Errors**

| Status | JSON | When |
|--------|------|------|
| 422 | `{"detail": "Invalid request"}` or `{"detail": "user_message must be a non-empty string"}` | Validation |
| 502 / 504 | AI provider failure | See section 3.2 |

### 6.3 DELETE `/api/v1/agents/general/conversation`

Clears the authenticated user's general-agent conversation history (fresh
chat on next message).

**Required headers:** auth.

**Success — `200`**

```json
{ "message": "Conversa do assistente geral apagada com sucesso." }
```

**Errors**

| Status | JSON | When |
|--------|------|------|
| 401 | `{"detail": "Authentication required"}` | No credentials |

---

## 7. Proxy (infrastructure passthrough)

Base: `/api/v1/proxy` · Forwards to configured downstream microservices.

### 7.1 GET/POST/PUT/PATCH/DELETE `/api/v1/proxy/{service_name:path}`

Any HTTP method; forwards the request (same body/query/method) to the
downstream service named by the first path segment. Downstream headers are
echoed back; `Authorization`/`Cookie`/`X-API-Key` are stripped and
`X-Forwarded-For` + `X-Gateway-User` are injected. GET/HEAD responses are
cached **per caller** (`cache_ttl_default` = 60 s; hits flagged via
`X-Cache: HIT`), so a cached response never leaks between users.

> Typical mobile usage: none — the app talks to the first-class endpoints
> above. This exists for gateway-to-microservice calls (e.g. an `ai` service
> behind `/proxy/ai/...`).

**Required headers:** auth. **AI rate limit** if path contains `/ai/`.

**Path params:** `service_name` (first segment names the service, rest is the
downstream path).

**Errors**

| Status | JSON | When |
|--------|------|------|
| 404 | `{"detail": "Service not found"}` | First segment not a configured service |
| 413 | `{"detail": "Payload too large"}` | Body > 1 MiB (`max_payload_bytes`) |
| 502 | `{"detail": "Bad gateway response"}` | Downstream unreachable/error |
| 401 | `{"detail": "Authentication required"}` | No credentials |

---

## 8. Admin

Two surfaces:

1. **JSON admin API** (`/api/v1/users`, `/api/v1/system-prompts`) — for
   programmatic administration; requires an `ADMIN`-role user.
2. **Server-rendered panel** (`/admin/...`) — HTML forms + **httpOnly
   cookie** session (cookie: `novaiax_admin_session`); no `Authorization`
   header. Non-admins are redirected to `/admin/login`.

### 8.1 JSON admin API — Users

All require an `ADMIN`-role token (403 `{"detail": "Not authorized"}` for
non-admins).

#### GET `/api/v1/users/` — list users

**Query params:** `page` (int, ≥1, default 1), `limit` (int, 1–100, default 20).

**Success — `200`**

```json
{
  "users": [
    { "id": 1, "full_name": "Ana", "email": "ana@x.com", "role": "USER", "created_at": "2026-09-18T10:00:00Z" }
  ],
  "page": 1,
  "limit": 20,
  "total": 1
}
```

#### PATCH `/api/v1/users/{user_id}/promote` — make a user `ADMIN`

**Success — `200`**: `{"message": "User promoted to administrator."}`
(errors: 404 `{"detail": "User not found"}`).

#### PATCH `/api/v1/users/{user_id}/demote` — downgrade an admin

**Success — `200`**: `{"message": "User demoted to regular user."}`
(errors: 404 user not found; **400** `{"detail": "Cannot demote the last administrator."}`).

#### DELETE `/api/v1/users/{user_id}` — delete a user

**Success — `200`**: `{"message": "User deleted."}`
(errors: 404 user not found; **400** `{"detail": "Cannot delete the last administrator."}`).

### 8.2 JSON admin API — System Prompts

These are the "agents" referenced by `agent_id` in `POST /api/v1/ai/chat`.
All require an `ADMIN`-role token.

#### GET `/api/v1/system-prompts/` — list prompts

**Query params:** `page` (int ≥1, default 1), `limit` (int 1–100, default 50).

**Success — `200`**

```json
{
  "prompts": [
    { "id": 3, "tipo": "general", "system_prompt": "You are a helpful assistant.", "created_at": "2026-09-18T10:00:00Z" }
  ],
  "page": 1, "limit": 50, "total": 1
}
```

#### POST `/api/v1/system-prompts/` — create (201)

Body: `tipo` string 2–100 (required) · `system_prompt` string 1–5000 (required).

**Success — `201`**: single prompt object (see above).

#### GET `/api/v1/system-prompts/{prompt_id}` — fetch one

**Success — `200`**: prompt object. **404**: `{"detail": "System prompt not found"}`.

#### PUT `/api/v1/system-prompts/{prompt_id}` — update

Body: same as create. **Success — `200`**: prompt object.
**404**: `{"detail": "System prompt not found"}`.

#### DELETE `/api/v1/system-prompts/{prompt_id}` — delete

**Success — `204` No Content.** **404**: `{"detail": "System prompt not found"}`.

### 8.3 Server-rendered admin panel (`/admin`)

Authenticated via the `novaiax_admin_session` httpOnly cookie (access JWT).
The web UI is browser-driven HTML; the mobile app does **not** use these.

| Method | Path | Purpose | Auth |
|--------|------|---------|------|
| GET  | `/admin/login` | Login form | public |
| POST | `/admin/login` | Authenticate (ADMIN only); sets cookie; `303 → /admin/` | public |
| POST | `/admin/logout` | Clears cookie; `303 → /admin/login` | cookie |
| GET  | `/admin/` | Dashboard (counts of users/admins/prompts) | cookie; redirect `303 → /admin/login` if invalid |
| GET  | `/admin/users` | User table (paginated) | cookie |
| POST | `/admin/users/{id}/promote` / `/demote` / `/delete` | Role ops / delete | cookie |
| GET  | `/admin/system-prompts` | Prompt list + create form | cookie |
| POST | `/admin/system-prompts` | Create prompt | cookie |
| GET  | `/admin/system-prompts/{id}/edit` | Edit form | cookie |
| POST | `/admin/system-prompts/{id}` | Update prompt | cookie |
| POST | `/admin/system-prompts/{id}/delete` | Delete prompt | cookie |
| GET  | `/admin/static/admin.css` | Stylesheet | public |

All actions reuse the same service layer as the JSON API (no duplicated
business logic). Login failures render the form with an error; a non-admin
submitting the login form receives `403` with "Only administrators can access
the panel.".

**CSRF:** `GET /admin/login` mints a `novaiax_csrf` cookie (path `/admin`,
httpOnly, `SameSite=Lax`). Every panel POST form carries a hidden `csrf_token`
that must match that cookie — a mismatch returns `403`
`{"detail": "CSRF token mismatch"}`. Clients that never received the cookie
(plain scripting or `curl`) are accepted without a token.

---

## 9. NOT documented (internal / debug / non-app)

These endpoints exist but are not called by the mobile app, so they are
excluded from reference detail above:

- **GET `/health`** — liveness/readiness probe (DB + Redis checks). Public and
  rate-limit-exempt. `200` `{"status":"ok","db":"ok","redis":"ok"}` or `503`
  `{"status":"degraded","db":"error","redis":"error"}`. Used by the host
  orchestrator (Render), not the app.
- **`/docs`** and **`/openapi.json`** — Swagger UI and OpenAPI schema,
  exposed for development.
- **`/admin/static/*`** — static CSS assets (see 8.3).
- The test suite references a `GET /rate-limit-probe` route, but that route is
  registered **only inside tests** (as a stand-in protected path) and does not
  exist in the deployed application.