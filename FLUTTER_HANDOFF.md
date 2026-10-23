# NovaIAx — Flutter Integration Handoff

Single source of truth for the mobile team. It describes the **HTTP contract**
the Flutter app must implement against: authentication, the error envelope,
every feature area, plan gating, AI quota and the privacy/compliance flows.

- Full endpoint catalog (method, path, params, bodies, responses):
  [`API_REFERENCE.md`](API_REFERENCE.md)
- Machine-readable schema: [`openapi.json`](openapi.json)
- Privacy feature documentation: [`PRIVACY.md`](PRIVACY.md)
- Deployment / environments: [`DEPLOY.md`](DEPLOY.md)

> The backend is FastAPI. All JSON, `snake_case`, integer ids, ISO-8601 UTC
> timestamps. Nothing in this document is aspirational — it is the running
> code's behavior.

---

## 1. Environments & base URL

| Environment | Base URL |
|-------------|----------|
| Local dev   | `http://localhost:8000` |
| Production  | the Render service URL (see `DEPLOY.md`) |

The JSON API lives under `/api/v1`. The public root `/` returns
`{"name":"NovaIAx API","status":"ok","version":"0.1.0"}`. The liveness/readiness
probe is `/health` (HEAD is also supported); it returns
`{"status":"ok","db":"ok","redis":"ok"}` with HTTP 200, or 503 when a
dependency is down. Trailing-slash variants are normalized internally and do
not issue redirects.

---

## 2. Authentication

### 2.1 Credentials

Every protected user-scoped request carries a JWT:

```
Authorization: Bearer <access_token>
```

- **Access token TTL:** 15 minutes.
- **Refresh token TTL:** 7 days.
- Protected endpoints accept only access tokens; a refresh token used as a
  Bearer token is rejected with `401 authentication_required`.
- These routes are public and do not require a user token: `GET /`, `GET` or
  `HEAD /health`, `GET /api/v1/app-config`, `POST /api/v1/auth/register`,
  `/login`, `/refresh`, `/logout`, `GET /privacy`, `/terms`,
  `/account-deletion`, `POST /account-deletion/request`,
  `POST /api/v1/billing/google/rtdn`, `/docs`, `/redoc`, `/openapi.json`, and
  `/admin/login` plus `/admin/static/*`. RTDN verifies Pub/Sub credentials on
  its own; the admin panel uses its own cookie/CSRF flow.
- An invalid, expired, or malformed `Authorization` header is ignored on
  public routes. In particular, a stale access token must not block login,
  registration, refresh, or logout. Refresh and logout use the refresh token
  in the request body, not the Authorization header.

### 2.2 Endpoints

| Method | Path | Body | Response |
|--------|------|------|----------|
| `POST` | `/api/v1/auth/register` | `{full_name, email, password, birth_date}` | `201` `AuthResponse` |
| `POST` | `/api/v1/auth/login`    | `{email, password}` | `200` `AuthResponse` |
| `POST` | `/api/v1/auth/refresh`  | `{refresh_token}` | `200` `TokenResponse` |
| `POST` | `/api/v1/auth/logout`   | `{refresh_token, access_token?}` | `200` `TokenResponse` |
| `GET`  | `/api/v1/auth/me`       | — | `200` `UserResponse` |

`AuthResponse`:

```json
{
  "user": { "id": 1, "full_name": "Ana", "email": "ana@example.com", "role": "USER" },
  "access_token": "eyJ...",
  "refresh_token": "eyJ..."
}
```

`TokenResponse` (refresh/logout): `{ "access_token": "...", "refresh_token": "..." }`.

`UserResponse`: `{ "id", "full_name", "email", "role" }` where `role` is
`USER`, `ADMIN` or `SERVICE`.

**Registration requires `birth_date`** (`YYYY-MM-DD`) — the age gate
(`MINIMUM_ACCOUNT_AGE_YEARS`, default 18) rejects under-age applicants with
`422 validation_error`. Registration auto-records the `terms` and `privacy`
consents.

### 2.3 Token handling the app MUST implement

1. Store `access_token` + `refresh_token` securely (e.g. `flutter_secure_storage`).
2. Send the access token only to protected routes; do not attach it to public
  auth, app-config, health, or legal-page requests.
3. On a protected-route `401 authentication_required`, call
  `POST /api/v1/auth/refresh` once with `{ "refresh_token": "..." }`, then
  retry the original request with the new access token.
4. If refresh returns `401 authentication_required`, clear the session and
  route to login. An Authorization header is not needed for refresh.
5. On logout, call `POST /api/v1/auth/logout` with the refresh token in the
  body before clearing local tokens. An access token may optionally be
  included in the body for immediate revocation; the header is not used.
6. Never retry a `401 invalid_credentials` (wrong password). A suspended
  account may complete public login/refresh, but protected requests return
  `403 account_suspended` until the account is restored.

### 2.4 Login lockout

After **5** failed passwords for one email within **15 minutes**, further
logins for that address return `429` with `code: "rate_limit_exceeded"` until
the window expires. A successful login resets the counter. The lockout is keyed
on **email + client IP**, so a third party cannot lock a victim's address out
from a different network. Show the `detail` message; do not hard-code the text.

### 2.5 First user is admin (development only)

In development, the first user registered in an empty database becomes `ADMIN`;
everyone else becomes `USER`. **In production this bootstrap is disabled** —
`FIRST_USER_ADMIN` is forced off when `ENVIRONMENT=production`, so a public
sign-up can never self-promote. The app must never assume a freshly registered
account is an admin; admin-only endpoints (§10, §15) return `403` for normal
users.

---

## 3. The error envelope (read this twice)

**Every** error response is:

```json
{ "detail": "<mensagem em pt-BR>", "code": "<codigo_estavel>" }
```

- `detail` is pt-BR, for display. It can change between releases.
- `code` is **stable** and is what the app switches on. It is present on every
  error, including generic `HTTPException`s (the code is derived from the
  status).

### 3.1 Generic codes

| HTTP | `code` | App behavior |
|------|--------|--------------|
| 400 | `bad_request` | Show `detail`; likely a client bug |
| 401 | `authentication_required` | Refresh then retry (§2.3) |
| 401 | `invalid_credentials` | Login screen, show `detail` |
| 403 | `forbidden` | "Sem permissão" |
| 403 | `account_suspended` | Account is suspended; show the support contact in `detail` |
| 404 | `not_found` | Resource gone / not yours |
| 409 | `conflict` | Duplicate (e.g. memory key) |
| 413 | `payload_too_large` | Reduce body |
| 422 | `validation_error` | Fixed body (below); fix the payload |
| 429 | `rate_limit_exceeded` | Back off (read `Retry-After`) |
| 500 | `internal_error` | Generic error + retry |
| 502/503 | `service_unavailable` | Generic error + retry |

### 3.2 AI codes

| HTTP | `code` | Meaning / UI |
|------|--------|--------------|
| 429 | `ai_rate_limited` | Provider throttled; retry shortly |
| 429 | `ai_busy` | Queue saturated; retry shortly |
| 429 | `ai_quota_exceeded` | **User's** daily AI budget spent → upsell |
| 504 | `ai_provider_timeout` | Retry |
| 502 | `ai_provider_error` | Retry |
| 502 | `ai_provider_auth_failed` | Server-side misconfig; report |
| 502 | `ai_invalid_response` | Retry; do not show raw content |
| 502 | `ai_all_providers_failed` | Retry later |

### 3.3 Plan / billing codes

| HTTP | `code` | Meaning / UI |
|------|--------|--------------|
| 402 | `plan_upgrade_required` | Feature needs a higher plan → paywall |
| 402 | `plan_feature_locked` | Feature not in plan → paywall |
| 402 | `plan_limit_reached` | Plan quota exhausted → upsell |
| 403 | `subscription_inactive` | Subscription not active → manage/repurchase |
| 503 | `billing_unavailable` | Billing backend down; retry |
| 400 | `purchase_invalid` | Purchase verification failed |

### 3.4 Validation

`422` body is **fixed**:

```json
{ "detail": "Requisição inválida.", "code": "validation_error" }
```

No per-field breakdown is exposed. Treat any `422` as a client bug.

---

## 4. Global conventions

- **Pagination:** `{ "items": [...], "page": 1, "limit": 20, "total": 42 }`
  (`page` 1-based). Default `limit` is **20** unless noted; max is **100**.
  **Exception:** `GET /api/v1/objectives/` uses `offset`/`limit` (default 50)
  and returns a **bare array** for backward compatibility (§14).

  | Endpoint | Query params | Default `limit` |
  |----------|--------------|-----------------|
  | `GET /me/tasks` | `page`, `limit`, `status`, `priority`, `due_from`, `due_to` | 20 |
  | `GET /me/reminders` | `page`, `limit`, `status`, `from`, `to` | 20 |
  | `GET /me/notes` | `page`, `limit`, `tag`, `pinned` | 20 |
  | `GET /me/checklists` | `page`, `limit`, `category` | 20 |
  | `GET /me/habits` | `page`, `limit` | 20 |
  | `GET /me/focus-sessions` | `page`, `limit`, `from`, `to` | 20 |
  | `GET /me/medications` | `page`, `limit`, `active` | 20 |
  | `GET /me/medication-intakes` | `page`, `limit`, `medication_id`, `status`, `from`, `to` | 20 |
  | `GET /me/finance/expenses` | `page`, `limit`, `category`, `from`, `to` | 20 |
  | `GET /me/finance/incomes` | `page`, `limit`, `from`, `to` | 20 |
  | `GET /me/finance/bills` | `page`, `limit`, `status`, `due_from`, `due_to` | 20 |
  | `GET /me/study/plans` | `page`, `limit`, `status` | 20 |
  | `GET /me/study/decks` | `page`, `limit` | 20 |
  | `GET /me/study/decks/{id}/cards` | `page`, `limit`, `due_only` | **50** |
  | `GET /me/logs` | `page`, `limit`, `kind`, `from`, `to` | 20 |
  | `GET /community/reports` | `page`, `limit`, `category` | 20 |
  | `GET /community/drafts` | `page`, `limit` | 20 |
  | `GET /system-prompts/` (admin) | `page`, `limit` | **50** |
  | `GET /users/` (admin) | `page`, `limit` | 20 |

  **Never paginated** (fixed or bare): `GET /objectives/` (bare array),
  `GET /objectives/{id}/roadmap/days` (`{objective_id, days}`),
  `GET /me/finance/budgets` (`{month, items}`),
  `GET /me/consents` (`{consents}`), `GET /community/support`,
  `GET /community/blocks`, `GET /community/official-contacts` (`{items}`),
  `GET /agent-groups`, `GET /sub-agents`, `GET /me/memories` (bare arrays).
- **Timestamps:** ISO-8601 UTC with `Z`, e.g. `2026-01-31T14:05:00Z`.
- **Ids:** integers.
- **Field names:** `snake_case`.
- **Rate limits:** 60 req/min per IP by default. AI paths (`/ai/`,
  `/agents/general`, `/agents/device`, `/agents/chat`,
  `/objectives/assistant`, `/objectives/register`, `/roadmap/renew`,
  `/me/weekly-plan`) are limited to **10 req/min per IP**, or **8 req/min per
  authenticated user id** once a JWT is present. Response headers:
  `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `Retry-After`.

---

## 5. Objectives & roadmap

| Method | Path | Body | Response |
|--------|------|------|----------|
| `GET` | `/api/v1/objectives/` | — | `200` **array** of `ObjectiveResponse` |
| `POST` | `/api/v1/objectives/register` | `{title, description?, due_date}` | `200` `ObjectiveResponse` |
| `POST` | `/api/v1/objectives/{id}/roadmap/renew` | — | `200` `ObjectiveResponse` |
| `GET` | `/api/v1/objectives/{id}/roadmap/days` | — | `200` `{objective_id, days[]}` |
| `PATCH` | `/api/v1/objectives/{id}/roadmap/days/{day_id}` | `{status}` | `200` `RoadmapDayResponse` |
| `DELETE` | `/api/v1/objectives/{id}/roadmap/days/{day_id}` | — | `204` |

`ObjectiveResponse`:

```json
{
  "id": 1, "title": "...", "description": "..." ,
  "roadmap": "## Roadmap\n...", "roadmap_updated_at": "2026-01-31T14:05:00Z",
  "due_date": "2026-02-28T00:00:00Z", "user_id": 1,
  "days": [ { "id": 1, "day_number": 1, "day_date": "2026-02-01",
              "content": "...", "status": "PENDING", "completed_at": null } ]
}
```

- `RoadmapDayStatus` ∈ `PENDING | COMPLETED | SKIPPED`.
- `POST /objectives/register` and `.../renew` call the AI to generate the
  7-day roadmap, so they can return the AI codes in §3.2 and count against the
  AI rate bucket. `renew` is only available every 7 days.
- `roadmap/renew` is gated per plan (`plan_upgrade_required` / `ai_quota_exceeded`).
- `DELETE .../days/{id}` removes a day; remaining days keep their original
  `day_number`.

### 5.1 Objective assistant

| Method | Path | Body | Response |
|--------|------|------|----------|
| `POST` | `/api/v1/objectives/assistant` | `{user_message}` | `200` `{assistant_message, created_objective?}` |

Multi-turn: the server keeps the conversation per user. Keep sending
`user_message` on the same endpoint; the server decides when the objective is
complete and returns `created_objective` populated. When that happens the
server clears the cached conversation, so the **next** message starts fresh.

---

## 6. Agents & chat

| Method | Path | Body | Response |
|--------|------|------|----------|
| `POST` | `/api/v1/agents/general/chat` | `{user_message}` | `200` `{assistant_message, why[], sources[]}` |
| `DELETE` | `/api/v1/agents/general/conversation` | — | `200` `{...}` |
| `POST` | `/api/v1/agents/device/command` | `{user_message}` | `200` `{reply, action?}` |
| `POST` | `/api/v1/ai/chat` | `{agent_id, user_message}` | `200` `{assistant_message, why[], sources[]}` |
| `DELETE` | `/api/v1/ai/chat/conversation` | query `agent_id` | `200` `{...}` |
| `POST` | `/api/v1/ai/feedback` | `{message_id, reason, details?}` | `201` `{id, message_id, reason, created_at}` |
| `POST` | `/api/v1/agents/chat` | `{message}` (alias `user_message`) | `200` `{message_id, reply, why[], sources[], proposed_actions[], safety}` |

- **Every** chat response carries a stable `message_id` (also on
  `general/chat`, `ai/chat` and `device/command`). The app echoes it back to
  `POST /ai/feedback`; the backend rejects a `message_id` that does not belong
  to the caller.
- `general/chat` and `ai/chat` include `why` (short reasons) and `sources`
  (memory keys / sub-agent slugs used); both are `[]` when there is no
  personalization.
- `POST /api/v1/agents/chat` is the unified orchestrator: it returns `reply`,
  `why`, `sources` and `proposed_actions` in one call.
- `device/command` may return a proposed device `action` that the app
  confirms/rejects via `/api/v1/agents/actions/...`. No proposed action ever
  sends a message or places a call on its own — dial/ compose steps are
  `PREPARES_ONLY` and always require an explicit user confirmation in the OS.
- `FeedbackReason` ∈ `offensive | wrong | unsafe | other`.
- All agent/AI calls count against the AI rate bucket and the user's daily AI
  budget (§8).

### 6.1 `POST /api/v1/agents/chat` response shape

```json
{
  "message_id": "…",
  "reply": "…",
  "why": ["…"],
  "sources": ["…"],
  "proposed_actions": [
    {
      "id": 42,
      "type": "SET_VOLUME",
      "params": { "stream": "media", "percent": 30 },
      "requires_confirmation": false,
      "summary": "Ajustar o volume de mídia para 30%",
      "executor": "device",
      "is_device": true,
      "prepares_only": false
    }
  ],
  "safety": {
    "crisis": false,
    "disclaimers": [],
    "emergency": { "active": false, "contacts": [] }
  }
}
```

- **`proposed_actions[]`** — every item has exactly:
  `id` (int), `type` (see §6.2), `params` (object, strict per type),
  `requires_confirmation` (bool), `summary` (pt-BR label), `executor`
  (`"backend" | "device"`), `is_device` (bool alias of `executor=="device"`,
  kept for compatibility) and `prepares_only` (bool).
- **`executor`** tells you who runs it: `"device"` → the app executes locally
  and reports back via `/agents/actions/{id}/result`; `"backend"` → the server
  runs it after `POST /agents/actions/{id}/confirm`.
- `requires_confirmation` and `prepares_only` are **server-side policy**,
  recomputed from the action type — the model can never weaken them.
- **`safety`** — always present:
  - `crisis` (bool): `true` on a crisis/emergency turn (the fixed reply is
    returned and the model is never called);
  - `disclaimers` (string[]): pt-BR notices for medical/financial/legal topics;
  - `emergency`: `{ "active": bool, "contacts": [{ "name", "description",
    "phone" }] }`. Contacts are populated **only** when `crisis` is `true`
    (CVV 188, SAMU 192, Bombeiros 193, Polícia 190, Ligue 180) so the app can
    render tappable call buttons; an ordinary turn is
    `{ "active": false, "contacts": [] }`.
- The three action endpoints in §6.3 return the **persisted action state**:
  `{ id, type, status, summary, requires_confirmation, executor, is_device,
  prepares_only, params, result, error, error_code, detail, created_at,
  updated_at }`.

### 6.2 Action reference (whitelist)

Only these types are ever returned as an action; anything else the model
emits is silently dropped. `params` are strict — an unknown key or an
out-of-range value rejects the whole action.

| `type` | `executor` | `params` | `requires_confirmation` | `prepares_only` |
|--------|-----------|----------|-------------------------|-----------------|
| `CREATE_TASK` | backend | `{title, notes?, due_at?, priority?, recurrence?}` | yes | no |
| `CREATE_REMINDER` | backend | `{title, body?, remind_at, recurrence?}` | yes | no |
| `CREATE_NOTE` | backend | `{title, content?, tags?[]}` | yes | no |
| `ADD_CHECKLIST_ITEM` | backend | `{checklist_id, text}` | yes | no |
| `LOG_EXPENSE` | backend | `{amount, category, description?, occurred_at?}` | yes | no |
| `LOG_MEDICATION_TAKEN` | backend | `{medication_id, taken_at?, notes?}` | yes | no |
| `START_STUDY_PLAN` | backend | `{subject, focus?}` | yes | no |
| `CREATE_FLASHCARDS` | backend | `{deck?, cards:[{front, back}]}` | yes | no |
| `SHOW_AGENDA` | backend | `{date?}` | yes | no |
| `MARK_DAY_COMPLETED` | backend | `{objective_id, day_id}` | yes | no |
| `SET_VOLUME` | device | `{stream: media\|ring\|alarm, percent: 0–100}` | no | no |
| `SET_BRIGHTNESS` | device | `{percent: 0–100}` | no | no |
| `TOGGLE_FLASHLIGHT` | device | `{on: bool}` | no | no |
| `OPEN_APP` | device | `{app_name}` | no | no |
| `OPEN_SETTINGS_PANEL` | device | `{panel: wifi\|bluetooth\|internet\|volume\|display}` | no | no |
| `DIAL` | device | `{contact_name?, phone?}` — at least one required; `phone` is normalised to digits with an optional leading `+` | yes | **yes** |
| `COMPOSE_MESSAGE` | device | `{app: sms\|whatsapp, contact_name, text}` | yes | **yes** |
| `SET_ALARM` | device | `{hour: 0–23, minute: 0–59, label?}` | no | no |
| `SET_TIMER` | device | `{seconds: 1–86400, label?}` | no | no |
| `CREATE_CALENDAR_EVENT` | device | `{title, start_iso, end_iso?}` | no | no |
| `NAVIGATE` | device | `{destination}` | no | no |
| `WEB_SEARCH` | device | `{query}` | no | no |
| `PLAY_MUSIC` | device | `{query}` | no | no |
| `WHAT_IS_PENDING_TODAY` | backend (server-resolved) | `{}` — the backend renders the pending-today facts **into `reply`** and no action row is ever proposed | no | no |

`WHAT_IS_PENDING_TODAY` stays in the model's vocabulary but is never
returned as an action: the server-rendered answer *is* the reply, so the app
only displays it (same behaviour as `POST /agents/device/command`, which
answers it with `action: null`).

### 6.3 Device action lifecycle

| Method | Path | Body |
|--------|------|------|
| `POST` | `/api/v1/agents/actions/{action_id}/confirm` | — |
| `POST` | `/api/v1/agents/actions/{action_id}/reject` | — |
| `POST` | `/api/v1/agents/actions/{action_id}/result` | `{status, error_code?, detail?}` |

`status` ∈ `executed | failed | canceled`. `error_code` / `detail` are optional
and only meaningful for `failed`; `canceled` means the user dismissed the OS
prompt without completing the action.

---

## 7. Productivity (per-user, isolated)

Every query is scoped to the caller's `user_id`. Dates are normalized to
**UTC** using the timezone in the agent profile (`GET /api/v1/me/agent` →
`timezone`, default `America/Sao_Paulo`); naive datetimes you send are
interpreted in that timezone and converted before storage.

| Area | Endpoints |
|------|-----------|
| Tasks | `GET/POST /me/tasks`, `GET/PATCH/DELETE /me/tasks/{id}` |
| Reminders | `GET/POST /me/reminders`, `PATCH/DELETE /me/reminders/{id}`, `POST /me/reminders/{id}/snooze` |
| Notes | `GET/POST /me/notes`, `GET/PATCH/DELETE /me/notes/{id}` |
| Checklists | `GET/POST /me/checklists`, `GET/PATCH/DELETE /me/checklists/{id}`, `POST/PATCH/DELETE /me/checklists/{id}/items[/{item_id}]` |
| Habits | `GET/POST /me/habits`, `GET/DELETE /me/habits/{id}`, `POST /me/habits/{id}/check-ins` |
| Focus | `GET/POST /me/focus-sessions`, `PATCH/DELETE /me/focus-sessions/{id}` |
| Summary | `GET /me/agenda`, `GET /me/today` |
| Weekly plan | `POST /me/weekly-plan`, `POST /me/weekly-plan/accept` |

Key enums:

- `TaskStatus` / `ReminderStatus` ∈ `pending | done | canceled`
- `TaskPriority` ∈ `low | medium | high`
- `ChecklistCategory` ∈ `compras | casa | bebe | viagem`

Creation bodies:

- Task: `{title, notes?, due_at?, priority?, recurrence?}` — completing a
  recurring task advances `due_at`.
- Reminder: `{title, remind_at?, ...}`; snooze via `{snooze_until}`.
- Habit: `{title}`; check-in via `{date}`.
- Focus session: `{start_at, label?, end_at?}`.

**Weekly plan** (`/me/weekly-plan`) is **PRO-gated** (feature `automation`):
lower plans get `402 plan_upgrade_required`. It returns suggestions **without
persisting**; the app persists everything by calling `/me/weekly-plan/accept`.
`WeeklyPlanResponse` = `{suggestions: [...], disclaimer: "..."}`.

### 7.1 Medication care (per-user)

| Area | Endpoints |
|------|-----------|
| Medications | `GET/POST /me/medications`, `GET/PATCH/DELETE /me/medications/{id}` |
| Intakes | `GET /me/medication-intakes`, `POST /me/medications/{id}/intakes`, `DELETE /me/medication-intakes/{id}` |
| Adherence | `GET /me/medications/adherence` |

`IntakeStatus` ∈ `pending | taken | skipped`. The adherence endpoint is a
read-only summary.

**Medication create body:** `name` (required), `start_date` (required,
`YYYY-MM-DD`), `dosage?`, `frequency?`, `instructions?`, `active?` (default
`true`), `end_date?` (`YYYY-MM-DD`, must be ≥ `start_date`) and
`schedule_times?` (list of zero-padded 24h `"HH:MM"` strings, max **12**,
stored sorted and de-duplicated). On update every field is optional; send
`end_date` less than `start_date` → `422`. On intake create the time may be
sent as `scheduled_at` **or** the alias `scheduled_for`; a naive datetime is
interpreted in the agent-profile timezone and stored as UTC. **The AI never
diagnoses and never changes a dose** — it may only surface the user's own
recorded data, and any generated text carries a `disclaimer`.

### 7.2 Personal finance (per-user)

| Area | Endpoints |
|------|-----------|
| Expenses | `GET/POST /me/finance/expenses`, `PATCH/DELETE /me/finance/expenses/{id}` |
| Incomes | `GET/POST /me/finance/incomes`, `PATCH/DELETE /me/finance/incomes/{id}` |
| Bills | `GET/POST /me/finance/bills`, `PATCH/DELETE /me/finance/bills/{id}` |
| Budget | `GET /me/finance/budgets`, `PUT /me/finance/budgets` |
| Summary | `GET /me/finance/summary` |

`BillStatus` ∈ `pending | paid | overdue`; `BillRecurrence` ∈ `none | monthly |
yearly`. Amounts are **integer cents**. **The AI never gives financial advice**
— it only aggregates the user's own records, and every AI-generated summary
carries a `disclaimer`.

### 7.3 Study (per-user)

| Area | Endpoints |
|------|-----------|
| Plans | `GET/POST /me/study/plans`, `GET/PATCH/DELETE /me/study/plans/{id}` |
| Decks | `GET/POST /me/study/decks`, `GET/PATCH/DELETE /me/study/decks/{id}` |
| Cards | `GET /me/study/decks/{id}/cards`, `POST /me/study/decks/{id}/cards`, `PATCH/DELETE /me/study/cards/{id}` |
| Reviews | `POST /me/study/cards/{id}/reviews` (SM-2 spaced repetition) |
| AI | `POST /me/study/quiz`, `POST /me/study/mind-map` |

`StudyPlanStatus` ∈ `active | completed | archived`. `quiz` and `mind-map` are
AI-backed (count against the AI bucket/budget) and include a `disclaimer`.

### 7.4 Logbook — trips, shifts & child-care (per-user)

`GET/POST /me/logs`, `GET/PATCH/DELETE /me/logs/{id}`, `GET /me/logs/report`.

`LogKind` ∈ `trip | shift | childcare` (read-only enum: the value is fixed by
the record, not user-editable). `GET /me/logs/report` returns an AI-generated
narrative over the caller's entries with a `disclaimer`.

### 7.5 Community (per-user, public-safe)

Community routes are prefixed `/api/v1/community` (not `/me`):

| Area | Endpoints |
|------|-----------|
| Reports | `GET/POST /api/v1/community/reports`, `GET/PATCH/DELETE /api/v1/community/reports/{id}` |
| Confirm / flag | `POST /api/v1/community/reports/{id}/confirm`, `POST /api/v1/community/reports/{id}/flag`, `DELETE /api/v1/community/reports/{id}/confirm` |
| Photos | `POST /api/v1/community/reports/{id}/photo` (**EXIF is stripped server-side**) |
| Drafts | `GET /api/v1/community/drafts`, `POST /api/v1/community/drafts`, `POST /api/v1/community/drafts/generate`, `PATCH/DELETE /api/v1/community/drafts/{id}` |
| Blocks | `GET/POST /api/v1/community/blocks`, `DELETE /api/v1/community/blocks/{blocked_id}` |
| Support | `GET /api/v1/community/support`, `GET /api/v1/community/official-contacts` |

`ReportStatus` ∈ `submitted | confirmed | flagged | removed`; `ReportCategory` ∈
`security | infrastructure | environment | transport | health | other`.
`GET /api/v1/community/support` is the **domestic-violence support** endpoint: it
returns guidance and emergency contacts and **never stores or publishes**
anything about the caller.

---

## 8. Plan gating & AI quota

- `GET /api/v1/me/subscription` returns `SubscriptionResponse`:
  `{plan, status, is_trial, active, subscription_inactive, has_subscription,
    product_id, expires_at, auto_renewing, limits}`.
- `plan` is a `PlanTier`: `free | trial | starter | pro | premium`.
- `limits` (`PlanLimitsResponse`): `display_name`, `price_brl`, `max_groups`,
  `max_sub_agents`, `daily_messages`, `daily_tokens`, `daily_device_actions`,
  `models`, `features`.
- AI calls charge the caller's **daily budget** keyed by plan. When spent:
  `429 ai_quota_exceeded` (distinct from `ai_rate_limited`, which is the
  provider throttling). Use the two codes for different UI.
- Missing a plan feature: `402 plan_upgrade_required` or `plan_feature_locked`.
- Plan limits live in `app/clients/plan_limits.py` (server-side source of
  truth); never hard-code them in the app — read `limits` from
  `/me/subscription`.

---

## 9. Billing (Google Play)

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `POST` | `/api/v1/billing/google/verify` | Bearer | Verify a purchase token after the Play purchase flow |
| `POST` | `/api/v1/billing/google/rtdn` | **Public** (Google Pub/Sub) | Server-to-server notifications — the app never calls this |
| `GET` | `/api/v1/me/subscription` | Bearer | Current entitlement |

Manage-subscription deep link (build in the app):

```
https://play.google.com/store/account/subscriptions?sku={product_id}&package={package_name}
```

`product_id` comes from `GET /me/subscription`; `package_name` is the app id
(`br.com.novaiax.agente`).

Purchase verification failure → `400 purchase_invalid`. Billing not configured
→ `503 billing_unavailable`.

---

## 10. Agent personalization

| Method | Path | Body |
|--------|------|------|
| `GET` | `/api/v1/agent-groups` | — (catalog) |
| `GET` | `/api/v1/sub-agents` | — (catalog) |
| `GET` | `/api/v1/me/agent` | — (profile or defaults) |
| `PUT` | `/api/v1/me/agent` | `{agent_name?, tone?, active_group_slugs?, active_sub_agent_slugs?, timezone?}` |
| `GET/POST` | `/api/v1/me/memories` | list / `{key, value}` |
| `PATCH/DELETE` | `/api/v1/me/memories/{key}` | update value/enabled / delete |
| `PUT` | `/api/v1/me/learning` | `{enabled: bool}` |

- Agent name: 2–30 chars, passes a basic filter (offensive terms / impersonating
  the platform are rejected `422`).
- User memory keys cannot use the reserved `auto_` prefix (auto-learned).
- Creating a memory with an existing key → `409 conflict`.
- When a profile exists, `general/chat` and `ai/chat` build the system prompt
  from base + name/tone + active groups' prompts + enabled memories (marked as
  untrusted data).

---

## 11. Privacy, consent & compliance (LGPD)

| Method | Path | Body |
|--------|------|------|
| `GET/PUT` | `/api/v1/me/consents` | list / `{type, version?}` |
| `DELETE` | `/api/v1/me/consents/{consent_type}` | — |
| `GET` | `/api/v1/me/export` | — |
| `DELETE` | `/api/v1/me` | `{password}` |

- Consent `type` ∈ `terms, privacy, microphone, contacts, location, learning`.
  Registration auto-records `terms` and `privacy` with the configured versions.
- Automatic memory learning requires an active `learning` consent as well as
  `learning_enabled=true` in the agent profile. Withdrawing `learning` disables
  that profile toggle and prevents further auto-learning.
- `PUT /me/consents` with an unknown `type` → `422`; `DELETE` unknown → `404`.
- `GET /me/export` returns a `DataExportResponse` (`schema_version`,
  `generated_at`, `user`, `consents`, `consent_history`, `objectives`,
  `memories`, `agent_actions`, `ai_feedback`, `chat_messages`, `conversations`,
  `promo_redemptions`, `subscription`, `purchase_events`, `productivity`, `health`
  (medications + intakes), `finance` (expenses/incomes/bills/budgets), `study`
  (plans/decks/cards/reviews), `logbook` (entries) and `community` (own
  reports/confirmations/flags, plus support content read-only), and
  `third_party_processors`) **without** `password_hash` /
  `purchase_token_hash`. It is **rate limited per user**
  (`DATA_EXPORT_MAX_PER_HOUR`, default 10/h) → `429 rate_limit_exceeded`.
- `DELETE /me` requires the current password; wrong password → `400` with a
  pt-BR `detail`. Deletion erases every module's rows scoped to the user
  (including their community reports, both directions of blocks, and the
  stored community photo **files** on disk) before the account row is removed.
- Public pages (no auth): `GET /privacy`, `GET /terms`, `GET /account-deletion`,
  `POST /account-deletion/request`.

---

## 12. Reminders & scheduled events — server storage vs. app scheduling

**The backend is a data store, not a scheduler.** It persists the *times*,
*recurrence* and *status* of scheduled items, but it runs **no cron, worker,
queue, FCM/APNs push or email**. It will never wake the device. **The app is
solely responsible for presenting reminders** (`flutter_local_notifications` or
similar).

Rules the app must follow:

- All stored times are **UTC** (ISO-8601 with `Z`). Convert to the **user's
  timezone** (from `GET /me/agent` → `timezone`, default `America/Sao_Paulo`)
  before scheduling; let the OS handle DST.
- **Pull then reconcile.** On login, on returning to the foreground, and after
  any edit, fetch the module's list and diff it against the locally scheduled
  notifications (use the server `id` as the notification id). Cancel
  notifications whose item was completed, canceled or deleted — including
  changes made on another device.
- Recompute the **next occurrence** of recurring items yourself; the server only
  stores the cadence (`none | daily | weekly | monthly`), it does not expand it.

| Module | Endpoint | Stored time anchor | Recurrence | What the app must schedule |
|--------|----------|--------------------|------------|----------------------------|
| Reminders | `/me/reminders` | `remind_at` (UTC) | `recurrence`; `snoozed_until` | One local notification at `remind_at` (user TZ); reschedule on snooze; cancel when `status` ∈ `done/canceled`. |
| Tasks | `/me/tasks` | `due_at` (UTC, nullable) | `recurrence` | Optional reminder at `due_at`; nothing when `due_at` is `null`. |
| Medications | `/me/medications` | `schedule_times` (`HH:MM[]`) + `start_date`/`end_date`, `active` | daily within window | One local notification per time, each day between `start_date` and `end_date`; stop when `active` is false or past `end_date`. |
| Medication intakes | `/me/medication-intakes` | `scheduled_at` (UTC) | — | The app logs `taken`/`skipped` via the API; the server does **not** remind. |
| Bills | `/me/finance/bills` | `due_date` (date) | `recurrence` | Local reminder (e.g. N days before `due_date`); refresh when `status` becomes `paid`. |
| Study cards | `/me/study/decks/{id}/cards` | `due_at` (UTC, spaced repetition) | computed server-side per review grade | Optional "cards due" reminder; the server recomputes `due_at` when you `POST .../reviews`. |
| Focus sessions | `/me/focus-sessions` | `start_at` (UTC) | — | In-app timer; no system notification required. |

Medication `schedule_times` are wall-clock `HH:MM` strings (24h, zero-padded),
max **12 per medication**, unique and sorted; the app interprets them in the
user's timezone together with `start_date`/`end_date`.

---

## 13. Known limitations & TODO (priority order)

1. **No server-side scheduling or push (see §12).** Notification delivery is
   100% the app's job; the server has no worker/queue/FCM.
2. **AI device actions require provider quota.** `/agents/device` calls the
   configured LLM (Groq/OpenAI); when the quota is exhausted the endpoint
   returns the AI error codes in §3.2, and the smoke test skips that step.
3. **PostgreSQL test harness caveat (dev only).** Async tests run against
   SQLite; the asyncpg pool can bind to a session-scoped loop under
   `pytest-asyncio`. End-to-end verification is done against a **live server**
   with `scripts/seed_dev.py` + `scripts/smoke_test.py`, not via pytest on PG.
4. **Recurrence is not materialized.** Recurring tasks/reminders/bills store a
   cadence only; the app expands occurrences and the server keeps a single row.
5. **`GET /objectives/` is a legacy bare array** (see §14) and the only list
   endpoint using `offset`; do not wrap it.
6. **Rate limiting is permissive when Redis is down** — the IP limiter falls
   back instead of failing closed. PostgreSQL/Redis must be healthy in prod.
7. **At most 5 proposed actions per agent turn** and **≤12 medication times per
   day** (server-side caps); the app should not assume more.
8. **Community reports are public-safe.** Photos are stored with **EXIF
   stripped**; reports may be removed by admins and are not a support channel.

---

## 14. Backward-compatibility / breaking changes

These are the deliberate deviations from the global rules; implementers must
match them exactly:

| Endpoint / item | Behavior | Why |
|------------------|----------|-----|
| `GET /api/v1/objectives/` | **Bare JSON array** (not `{items,...}`) | Shipped app expects an array |
| `GET /api/v1/objectives/{id}/roadmap/days` | `{objective_id, days}` (not paginated) | Roadmap is fetched whole |
| Error bodies | Gained a stable `code` field; `detail` text is now **pt-BR** | Additive; clients reading only `detail` still work |

No other route was renamed or had a field removed. If the backend later needs
to change one of the three above, it will be announced here and via a version
bump.

---

## 15. Admin & service endpoints (the app normally does not call these)

| Area | Endpoints | Auth |
|------|-----------|------|
| Users | `GET /api/v1/users/`, `PATCH /users/{id}/promote|demote`, `DELETE /users/{id}` | ADMIN |
| System prompts | `GET/POST /api/v1/system-prompts/`, `GET/PUT/DELETE /api/v1/system-prompts/{id}` | ADMIN |
| Cache | `POST /api/v1/cache/flush` | Bearer (flushes the caller's own cache) |
| Proxy | `/api/v1/proxy/{service}/...` | Bearer or `X-API-Key` |

The admin panel (`/admin`, server-rendered HTML) is for operators, not the app.

---

## 16. Suggested Flutter implementation checklist

1. **Dio/HTTP layer:** a single `ApiClient` that injects `Authorization`, maps
   the `{detail, code}` envelope to a typed `ApiException(code, detail,
   statusCode)`, and implements the refresh-on-401 interceptor (§2.3).
2. **Error handling:** switch on `code`, never on `detail`. Render `detail` as
   the user-facing message.
3. **Rate limiting:** handle `429` with exponential backoff honoring
   `Retry-After`; distinguish `rate_limit_exceeded` (IP) from `ai_*` (AI).
4. **Plan gating:** on `402 plan_upgrade_required` / `plan_feature_locked`
   show the paywall; on `ai_quota_exceeded` show the quota message + upsell.
5. **Timestamps:** parse with `DateTime.parse` (the `Z` suffix yields UTC) and
   convert to local for display.
6. **Consent:** read `GET /me/consents` on first launch; the app should
   surface `microphone/contacts/location/learning` toggles and write them via
   `PUT /me/consents`.
7. **Smoke test the contract:** run `python scripts/smoke_test.py <base_url>`
   against a deployment to verify the flow your app depends on before release.

---

## 17. New app-facing contract (promoções, config remota e estados de conta)

Additive endpoints and fields the app should adopt. All three calls were
verified live against the smoke deployment; none renames or removes anything
from the previous sections.

### 17.1 `POST /api/v1/me/promo-codes/redeem` (Bearer)

Redeem an operator-issued promo code. Body: `{"code": "<texto digitado>"}`
(1–40 chars, case-insensitive — the server normalises to lower case).

**201 response** — echoes what was granted, so the UI confirms in-place
without re-fetching the subscription:

```json
{
  "code": "noviax-starter30",
  "kind": "grant_plan",          // grant_plan | extend_trial | bonus_quota
  "plan": "starter",             // null for bonus_quota
  "bonus_messages": null,        // non-null only for bonus_quota
  "expires_at": "2026-11-19T17:34:56Z"   // end of the grant; null = permanent
}
```

**`expires_at` is new and matters:** it is the moment the grant stops
applying. After it, the entitlement resolver falls back to the previous
source (Play or free). Show it on the confirmation screen
("Plano Starter até 19/11/2026").

Stable failures (switch on `code`, never on `detail`):

| Status | `code` | App behavior |
|--------|--------|--------------|
| `404` | `not_found` | "Código inválido ou inexistente." — keep the input, do not retry |
| `410` | `expired` | "Este código expirou." |
| `409` | `exhausted` | "Este código atingiu o limite de usos." |
| `409` | `already_redeemed` | "Você já usou este código." — offer the plan screen |
| `429` | `rate_limit_exceeded` | Backoff honoring `Retry-After`; this endpoint has a per-user **and** per-IP budget |

On success the new plan is immediately visible in `GET /me/subscription`
(`plan`, `active`, `expires_at` updated, `source` becomes `"promo"`); a
tipping-hat refresh of the subscription screen is enough.

### 17.2 `GET /api/v1/promotions/active` (Bearer)

Informational promotion cards addressed to the caller. Returns a **list**
(may be empty — that is normal, not an error), already filtered server-side
by the caller's entitlement, time window and sorted by `priority` (descending):

```json
[
  {
    "id": 1,
    "title": "Bem-vindo ao NovaIAx",
    "message": "Resgatando um código promocional você libera recursos premium.",
    "emoji": "🚀",
    "image_url": null,           // mutually exclusive with emoji in practice
    "cta_label": "Resgatar código",
    "cta_target": "redeem",
    "cta_url": null,
    "priority": 10
  }
]
```

`cta_target` tells the navigation: `plans` → plan screen, `redeem` → promo
code screen, `url` → open `cta_url` externally (browser). Cards never change a
price or take a payment (Google Play policy) — render them as banners only.

### 17.3 `GET /api/v1/app-config` (**public** — no `Authorization`)

The one call to make at startup, before login. It is on the auth middleware's
public allow-list, cached server-side, and supports `ETag`/`If-None-Match`
(bodyless `304`) plus a short `Cache-Control`:

```json
{
  "maintenance": {"enabled": false, "message": "Estamos em manutenção. Voltamos em instantes."},
  "versions": {"min_supported": "1.0.0", "recommended": "1.0.0", "store_url": ""},
  "features": {"medications": true, "finance": true, "study": true, "logbook": true,
               "community": true, "device_actions": true, "voice": true},
  "support": {"email": "suporte@novaiax.com.br", "whatsapp_url": null},
  "announcements": [{"title": "...", "body": "...", "level": "info"}]
}
```

**Expected app behavior:**

- **Maintenance:** when `maintenance.enabled` is `true`, stop calling the API
  and show a full-screen message with `maintenance.message` (pt-BR, already
  written by the operator). Keep re-polling `app-config` (cheap: send
  `If-None-Match`) until it flips back; then restore the session silently.
  Do **not** log the user out or delete local data during maintenance.
- **Minimum version:** compare the app's version against
  `versions.min_supported` using numeric segments (`1.2.0` < `1.3`). If the
  app is **below** `min_supported`, hard-block with an update prompt that
  opens `versions.store_url` (Play Store deep link) — API calls will be
  rejected. If below `recommended` but ≥ `min_supported`, show a soft
  update banner. An empty `store_url` means the operator has not configured
  it yet — still block, but without a store link.
- **Feature flags:** a `false` flag means the module is rolled off for
  everyone — hide the tab/entry point (or show it as "em breve") instead of
  calling the endpoint and surfacing errors.
- **Support:** `support.email` is always present (the operator edits it in
  *Configurações → Aplicativo → Suporte*); `support.whatsapp_url` is `null`
  until configured (a `https://wa.me/...` link). Use it for any "fale com o
  suporte" screen — the `account_suspended` detail already includes it
  (§17.5).
- **Announcements:** render as dismissible banners, `level` as severity
  (`info` and up); max 20 items, server-capped.

### 17.4 Subscription fields: `source` (and promo `expires_at`)

`GET /api/v1/me/subscription` (and the `/billing/google/verify` response) now
carries `source`, telling **where the effective plan comes from**:

| `source` | Meaning |
|----------|---------|
| `free` | No entitlement — default tier |
| `trial` | New-user trial |
| `play` | A verified Google Play subscription |
| `promo` | A promo code redemption or an admin manual grant |

Always render the plan from `plan` (the effective tier); use `source` only to
*explain* the origin ("Plano ativo por promoção" vs. "assinatura ativa no
Google Play"). The subscription-level `expires_at` and the redeem-level
`expires_at` (§17.1) are the same clock for a promo grant — one countdown, one
display component.

### 17.5 `account_suspended` — new error code

A suspended account now gets **`403`** with `{"detail": "...", "code":
"account_suspended"}` on **every** authenticated call (and its refresh tokens
are rejected). The `detail` is built server-side from the configured support
contact — e.g. *"Conta suspensa. Entre em contato com o suporte em
suporte@novaiax.com.br ou pelo WhatsApp https://wa.me/... ."* (only the
configured channels appear; the WhatsApp part is omitted when
`support.whatsapp_url` is `null`). Expected app behavior:

1. On `403 account_suspended`: drop the session, discard the token pair and
   show a locked screen — "Sua conta foi suspensa." — rendering the support
   contact from the `support` block of `GET /app-config` (§17.3), which is
   the same source the `detail` text already uses.
2. Do **not** attempt the refresh-on-401 flow (§2.3): refresh is refused too,
   and retrying only burns the rate limit.
3. Do **not** offer re-login until the user has contacted support; an
   unsuspended account simply logs in again normally.
