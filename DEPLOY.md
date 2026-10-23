# Deployment — Render

Step-by-step guide for deploying NovaIAx (monolithic FastAPI gateway + backend)
to [Render](https://render.com).  The app runs as a single **web service** built
from the existing `Dockerfile` (Docker runtime), backed by **Render-managed
PostgreSQL** and an **Upstash Redis** cache.

> **Migrated from Fly.io** (Nov 2026): Fly's free tier no longer exists, so
> `fly.toml` was replaced by a `render.yaml` Blueprint.  This guide replaces the
> old Fly guide end-to-end.

> **Assumes**: the repo already implements `GET /health` (verifies DB + Redis),
> Alembic migrations, secret validation (`_validate_production_secrets`), and
> the rate limiter reads `X-Forwarded-For` behind `TRUST_PROXY_HEADERS=true`.

---

## 0. What `render.yaml` provisions

The committed [`render.yaml`](render.yaml) Blueprint creates:

| Resource | Details |
| --- | --- |
| Web service `novaiax-gateway` | `runtime: docker` — uses the existing `Dockerfile` as-is; region **`virginia`**, plan **`free`**, health check at **`/health`**. No port is hardcoded: Render injects `$PORT` and `entrypoint.sh` binds uvicorn to `${PORT:-8000}` with `--proxy-headers`. |
| PostgreSQL `novaiax-db` | Render-managed Postgres, plan `free`. Its connection string is injected into the web service's `DATABASE_URL` via `fromDatabase` (no manual URL rewriting — the app normalises `postgresql://` → `postgresql+asyncpg://` internally). |
| Secrets | `SECRET_KEY` (Render-generated via `generateValue: true`), plus `MIGRATION_DATABASE_URL`, `REDIS_URL`, `GROQ_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `AI_PROVIDERS_JSON`, `MASTER_API_KEY`, `ADMIN_BOOTSTRAP_EMAIL`, `ADMIN_BOOTSTRAP_PASSWORD` and the `GOOGLE_PLAY_*` credentials — all `sync: false` → Render prompts for them on first sync; never stored in the repo. |
| Fixed values | `ENVIRONMENT=production`, `TRUST_PROXY_HEADERS=true`, `GOOGLE_PLAY_PACKAGE_NAME=br.com.novaiax.agente`. Full list in **[Render environment](#render-environment)**. |

### Regions

Render's current regions are `oregon`, `ohio`, `virginia`, `frankfurt`,
`singapore` — **there is no São Paulo region**.  For a Brazil-centric user
base, **`virginia`** (US East) is the lowest-latency option (direct backbone
routing from Brazil to the US east coast); `oregon` would add ~2× the round-trip
time.

### Plans

- **`free`** web service: 0.1 CPU / 512 MB, **spins down after 15 min without
  traffic** and cold-starts in ~30–60 s on the next request.  See
  § [Free-tier behavior](#7-free-tier-behavior-and-the-flutter-app).
- Switch `plan: free` → `0.5c-512mb` (Starter, ~$7/mo) in `render.yaml` to
  remove spin-down entirely (available for both web service and database).

> **Note**: Render's native `preDeployCommand` is **paid-plan-only**, which
> is why migrations run via the container entrypoint instead (see § 5).

---

## 1. Prerequisites

- A [Render](https://render.com) account (`dashboard.render.com/register`).
- Your repository pushed to GitHub/GitLab (Render links to the repo that
  contains `render.yaml`).
- A [Groq API key](https://console.groq.com/) (used by the AI endpoints).
- An [Upstash Redis](https://upstash.com/) account **(Render's free tier has no
  managed Redis — only Postgres — so Redis stays on Upstash as planned)**.

---

## 2. Create the Blueprint from `render.yaml`

1. Commit and push `render.yaml` to the repo's `main` branch:
   ```bash
   git add render.yaml entrypoint.sh Dockerfile .dockerignore app/core/network.py DEPLOY.md
   git commit -m "feat(deploy): migrate from Fly.io to Render blueprints"
   git push origin main
   ```
2. In the [Render Dashboard](https://dashboard.render.com), click **New → Blueprint**.
3. Choose the Git provider and select the `NovaIAx` repository (default branch
   `main`).
4. Render reads `render.yaml` and shows a preview of the resources it will
   create:
   - Web service **novaiax-gateway** (`runtime: docker`, region `virginia`)
   - PostgreSQL **novaiax-db** (`plan: free`)
5. Click **Apply**.  Render now prompts for every `sync: false` secret (see § 3).

> Later updates to the repo do **not** re-prompt for `sync: false` secrets —
> Render ignores them on Blueprint *updates*.  Set/rotate secret values from the
> dashboard (§ 3).

Optional: validate the file locally before pushing:
```bash
# Install the Render CLI
curl -fsSL https://raw.githubusercontent.com/render-oss/cli/refs/heads/main/bin/install.sh | sh
# or: brew install render
render login

render blueprints validate render.yaml
```

---

## 3. Set secrets in the dashboard

During the **initial Blueprint sync**, Render's UI asks you for a value for each
`sync: false` variable (`SECRET_KEY` needs nothing — Render generates it with
`generateValue: true`).  Full reference in **[Render environment](#render-environment)**
(required/optional, example, source for each variable):

| Secret | Value |
| --- | --- |
| `MASTER_API_KEY` | `openssl rand -hex 24` (48-hex random) |
| `GROQ_API_KEY` | your `gsk_…` key from [console.groq.com](https://console.groq.com) |
| `REDIS_URL` | your Upstash `rediss://…` connection string (§ 4) |
| `ADMIN_BOOTSTRAP_PASSWORD` | a strong password (≥ 12 chars) — the operator account the API creates for itself |
| `MIGRATION_DATABASE_URL` | only for Supabase: the session-pooler URL (§ 10); leave empty to reuse `DATABASE_URL` |
| `GOOGLE_PLAY_SERVICE_ACCOUNT_JSON` (+ `GOOGLE_PLAY_PRODUCTS_JSON`, `GOOGLE_PLAY_RTDN_TOKEN`, `GOOGLE_PLAY_RTDN_AUDIENCE`) | optional — billing verification stays inert until configured |
| `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `AI_PROVIDERS_JSON` | optional alternatives to `GROQ_API_KEY`; at least one provider key is mandatory |

**Rotating / editing later** — Blueprint updates skip `sync: false`, so manage
these per service:

1. Dashboard → your **service** (`novaiax-gateway`) → **Environment** tab.
2. Click **Add Environment Variable** (or the value field to edit an existing
   one); tick *Secret*.
3. Save, then **Manual Deploy → Deploy latest commit** (or **Restart service**)
   to apply them.

> Keep secrets out of `render.yaml` and out of git — only non-secret config
> (ENVIRONMENT, GROQ_API_MODEL, RATE_LIMIT_*, CACHE_TTL_*, MAX_PAYLOAD_BYTES,
> TRUST_PROXY_HEADERS, GOOGLE_PLAY_PACKAGE_NAME) is declared as plain
> `value:` entries there.  `DATABASE_URL` is injected automatically by
> `fromDatabase` (Render never writes it to git either) and `SECRET_KEY` is
> created by Render with `generateValue: true`.

---

## Render environment

Every variable `render.yaml` provisions for the `novaiax-gateway` service,
cross-checked against the production validators in `app/core/config.py`
(`_validate_production_secrets`, `_validate_admin_password`).  The app boots
in `ENVIRONMENT=production` as long as the **required** rows are set —
everything marked *optional* has a working default (or simply disables the
feature).

| Variable | Required? | Example | Where to get it |
| --- | --- | --- | --- |
| `ENVIRONMENT` | **Required** (declared `value`) | `production` | Set to `production` only on Render; it turns on the hard boot-time validators. |
| `DATABASE_URL` | **Required** (auto-injected) | `postgresql://novaiax:…@hostname:5432/renderdb` | Render fills it from the managed Postgres via `fromDatabase` (§ 0). With Supabase instead, replace the entry with `sync: false` and paste the **pooled** URL (transaction pooler `6543` is fine — the app sets `statement_cache_size=0`). |
| `SECRET_KEY` | **Required** (generated) | `9f4c…` (64 hex) | Render generates it on first Blueprint sync (`generateValue: true`); rotate with `openssl rand -hex 32` on the service's **Environment** tab. |
| `MASTER_API_KEY` | **Required** | `a71f…` (64 hex) | `openssl rand -hex 32` — any request carrying it is fully trusted, so keep it out of clients. |
| `GROQ_API_KEY` | **Required** (unless another provider key is set) | `gsk_…` | <https://console.groq.com> → API Keys.  At least one of `GROQ_API_KEY` / `OPENAI_API_KEY` / `OPENROUTER_API_KEY` / `AI_PROVIDERS_JSON` must exist. |
| `REDIS_URL` | **Required** | `rediss://default:…@va1-xyz.upstash.io:6379` | Upstash console → database → *Connect* (§ 4). |
| `GOOGLE_PLAY_PACKAGE_NAME` | **Required** (declared `value`) | `br.com.novaiax.agente` | Play Console → *Setup → Advanced → App details* (the Android application id). Must stay a valid package name or the app refuses to boot in production. |
| `MIGRATION_DATABASE_URL` | Optional | `postgresql://…-virginia.pooler.supabase.com:5432/postgres` | Supabase → *Connect* → **Session pooler** (port `5432`, IPv4).  When unset, Alembic migrates `DATABASE_URL` (§ 5 / § 10). |
| `OPENAI_API_KEY` | Optional | `sk-…` | <https://platform.openai.com/api-keys>. |
| `OPENROUTER_API_KEY` | Optional | `sk-or-v1-…` | <https://openrouter.ai/keys>. |
| `AI_PROVIDERS_JSON` | Optional | `[{"name":"groq","base_url":"https://api.groq.com/openai/v1","api_key":"gsk_…","default_model":"openai/gpt-oss-120b","priority":10}]` | Your own fleet definition; replaces the individual `*_API_KEY` variables entirely (`.env.example` documents the schema). |
| `ADMIN_BOOTSTRAP_EMAIL` | Optional (recommended) | `ops@example.com` | The operator account the API creates for itself on the first boot (§ 9).  Render prompts for it (`sync: false`). |
| `ADMIN_BOOTSTRAP_PASSWORD` | Optional (recommended) | a strong password | At least 12 characters and not a well-known one (`admin`, `admin123`, `password`, `12345678`, the `.env.example` placeholders) — a weak credential is refused in production and the deploy keeps running without an operator.  Chosen by you (`openssl rand -hex 16`).  The password is applied **only** when the account is created, so both variables can be removed after the first deploy. |
| `GROQ_API_MODEL` | Optional (declared `value`) | `openai/gpt-oss-120b` | Default model when a task has no model mapping; the Groq model catalog. |
| `GROQ_API_BASE_URL` | Optional (declared `value`) | `https://api.groq.com/openai/v1` | Groq docs — only change it if Groq moves the endpoint. |
| `TRUST_PROXY_HEADERS` | Optional, **`true` on Render** (declared `value`) | `true` | Keep `true` only behind Render's edge proxy so `X-Forwarded-For` is trusted for rate limiting. |
| `RATE_LIMIT_DEFAULT` / `RATE_LIMIT_AI` | Optional (declared `value`) | `"60"` / `"10"` | Requests per minute per IP; tune to your plan. |
| `CACHE_TTL_DEFAULT` / `CACHE_TTL_AI` | Optional (declared `value`) | `"60"` / `"300"` | Redis TTL seconds for generic vs. AI responses. |
| `MAX_PAYLOAD_BYTES` | Optional (declared `value`) | `"1048576"` | Request body cap; over it the gateway answers `413`. |
| `GOOGLE_PLAY_SERVICE_ACCOUNT_JSON` | Optional | `{"type":"service_account",…}` | Play Console → *Setup → API access → Create new service account* → download JSON key.  Billing verification stays inert without it. |
| `GOOGLE_PLAY_PRODUCTS_JSON` | Optional | `{"starter":"novaiax_starter","pro":"novaiax_pro"}` | Play Console → *Monetize → Products* (plan → product id mapping). |
| `GOOGLE_PLAY_RTDN_TOKEN` | Optional | a random shared secret | Set it yourself; it must match the token configured on the Pub/Sub subscription that pushes to `/api/v1/billing/google/rtdn`. |
| `GOOGLE_PLAY_RTDN_AUDIENCE` | Optional | `https://pubsub.googleapis.com/projects/…` | Pub/Sub subscription's OIDC audience, used to verify the push token. |

Other useful variables are **not** in `render.yaml` (they have safe defaults
and can be added as `sync: false` or plain values as needed):
`ADMIN_PANEL_ENABLED` (default `true`), `ADMIN_BOOTSTRAP_NAME`
(default `Administrador`), `ADMIN_BOOTSTRAP_PROMOTE_EXISTING` (default
`false`), `ADMIN_REQUIRE_TOTP` (default `false`),
`ADMIN_TOTP_ISSUER`, `ALLOWED_ORIGINS` (CORS; unset = same-origin only),
`DB_POOL_SIZE`/`DB_MAX_OVERFLOW`/`DB_POOL_RECYCLE_SECONDS`,
`FIRST_USER_ADMIN` (forced off in production), `RUN_MIGRATIONS` (entrypoint
migration switch, default `true`) and `PORT` (injected by Render — never set
it yourself).

> `PORT` is deliberately absent from `render.yaml`: Render picks the port for
> Docker services and injects it as `$PORT`; `entrypoint.sh` binds uvicorn to
> `${PORT:-8000}` accordingly.

---

## 4. Set up Upstash Redis

NovaIAx depends on Redis for rate limiting, caching, conversation history, API
keys, and token revocation.  Render's free tier exposes **only** managed
Postgres, so keep Redis on Upstash:

1. Create an account at <https://upstash.com> and log into the console.
2. **Create database** → choose **Redis**, name it (e.g. `novaiax`), pick the
   region closest to your users (Upstash's São Paulo region if available,
   otherwise `sa-east-1`).
3. Enable **TLS** (default) — the connection string starts with `rediss://`.
4. Copy the connection URL:
   ```
   rediss://default:<password>@<region>-<something>.upstash.io:6379
   ```
5. Paste it into the `REDIS_URL` secret on the `novaiax-gateway` service
   (§ 3).  Because `REDIS_URL` is declared `sync: false` in the Blueprint,
   Render stores it as an encrypted secret — it is never committed.

> The free Upstash tier is sufficient for low-traffic stages.  If you later move
> to a paid Render plan, you can swap Upstash for [Render Key Value](https://render.com/docs/key-value)
> if desired — but that is a separate change, not required to deploy.

---

## 5. Migrations: how Alembic runs on Render

Render's platform-level **pre-deploy command** (`preDeployCommand` in
`render.yaml`) is available **only for paid services**, so while the service is
on the free plan migrations run **inside the container**:

- `Dockerfile`'s `CMD` is `/app/entrypoint.sh`.
- `entrypoint.sh` runs `alembic upgrade head` (idempotent no-op when the schema
  is current), then `exec uvicorn app.main:app --host 0.0.0.0 --port
  ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'`.  Render injects
  `PORT`; the proxy flags make uvicorn honour `X-Forwarded-*` from Render's
  edge proxy so rate limiting sees the real client IP.
- With `set -e` plus an explicit guard, a **failed migration prints
  `migration failed` and stops the script**: the container exits non-zero,
  uvicorn never starts, and Render marks the deploy failed.  (On the
  `free` plan Render restarts the instance in place, so a failed deploy leaves
  the service down until the next successful deploy; paid plans get
  zero-downtime rollouts that keep the old version serving.)
- **Skipping migrations:** set `RUN_MIGRATIONS=false` in the service
  environment (default is `true`) and the entrypoint starts uvicorn without
  running Alembic — for operators with an out-of-band migration step.
- **Which database Alembic migrates:** `migrations/env.py` prefers
  `MIGRATION_DATABASE_URL` when set and falls back to `DATABASE_URL`
  otherwise (both normalised to `postgresql+asyncpg://`).  Use the override
  when the app's pooled URL and the migration URL must differ — the Supabase
  case described in § 10.

**Tradeoff vs. a pre-deploy hook:** a real `preDeployCommand` runs on a separate
instance *before* the deploy, cleanly aborting the deploy on failure.  The
in-process entrypoint achieves the same guardrail (migration failure → container
crash → deploy marked failed) but couples migrations to the serving instance's
lifecycle — they also re-run on every **free-tier cold start** (adds a moment to
spin-up).  If you swap `&&` for `;` in `entrypoint.sh`, a failed migration would
let uvicorn still try to boot against an unmigrated schema — generally **not**
what you want.

> **Upgrade path:** move the service to a paid plan (`0.5c-512mb` or higher),
> then add to `render.yaml` and delete the migration step from `entrypoint.sh`:
> ```yaml
>     preDeployCommand: alembic upgrade head
> ```

---

## 6. Trigger a deploy & tail logs

### Manual deploy

**Dashboard:** open the service's **Deploys** page → **Manual Deploy →
Deploy latest commit**.

**CLI:**
```bash
render login
render deploys create novaiax-gateway --wait
# or by service ID: render deploys create srv-abc123
```

Pushing to `main` auto-deploys by default (Blueprint default `autoDeployTrigger:
commit`).

### Tailing logs

**Dashboard:** service → **Logs** tab (streams build, migration, and request
logs).

**CLI:**
```bash
render logs --resources <service-id> --tail
# filter during a deploy: render logs -r <service-id> --text "alembic" --tail
```

`render services` lists your services and shows their IDs; `render deploys
create novaiax-gateway` also streams deploy logs live.

Deploy pipeline per Render: **build** (Docker image) → **start command**
(`entrypoint.sh`) → live.  The migration output appears at the top of the start
phase.

---

## 7. Verify `/health` post-deploy

Once the deploy shows **Live** and the health check passes:

```bash
# Health is public (exempt from auth + rate limiting):
curl -s https://novaiax-gateway.onrender.com/health
# => {"status":"ok","db":"ok","redis":"ok"}
```

- HTTP **200** with all `ok` → DB and Redis reachable.
- HTTP **503** or `{"status":"error"}` → the failing component is reported in
  `db` / `redis` (check `REDIS_URL` and `DATABASE_URL` secrets).

Swagger UI is served at `/docs`, e.g. `https://novaiax-gateway.onrender.com/docs`.

> The actual subdomain appears in the dashboard (Render may suffix the name if
> `novaiax-gateway` is taken).  Replace it in the commands above.

---

## Free-tier behavior and the Flutter app

The **free** web-service plan has two behaviors that matter for clients:

1. **Spin-down**: after **15 minutes** with no inbound traffic the instance
   stops; the next request triggers a cold start that takes **~30–60 s** (or
   longer on a heavily loaded queue).  The first request after idle can fail or
   time out from the client's perspective.
2. **Expiring database**: the free Postgres database **expires 30 days after
   creation** (1 GB cap).  After expiry there is a 14-day grace period to upgrade
   before the DB is deleted.  Plan a migration to a paid DB plan (or free-tier
   re-provisioning) before day 30.

Implications:

- **Backend-facing**: `/health` is enough for Render's checks.  No change needed
  server-side to handle spin-up.
- **Flutter app (client-facing) — action required**: the mobile client's HTTP
  timeouts must tolerate the cold-start window.  If the Flutter client uses a
  default `http`/`dio` timeout (< 10–15 s), the **first request after spin-down
  will time out**.  Ensure the timeout is ≥ 60–90 s, or add a retry-on-timeout
  with backoff, or pin the app open / warm the service.  **This is a client
  change to schedule on the Flutter side** — tracked as a separate item, not
  fixed by this repo.
- **Options to avoid the delay** (pick one when traffic justifies it):
  - Upgrade the web service to `0.5c-512mb` (Starter, ~$7/mo) — no spin-down.
  - Keep a lightweight uptime pinger hitting `/health` every ≤ 10 min (consumes
    free monthly instance hours — 750 h ≈ 31 days always-on).

---

## 8. First real users / smoke test

> **Production has no auto-admin.**  The "first registered user becomes admin"
> behavior only applies outside production.  On a production deploy, create the
> operator through the panel provisioning SQL (§ 9) before using `/admin`.

```bash
BASE=https://novaiax-gateway.onrender.com

# Register the first user (becomes admin automatically outside production):
curl -s -X POST "$BASE/api/v1/auth/register" \
  -H "Content-Type: application/json" \
  -d '{"full_name":"Admin","email":"admin@example.com","password":"S3cure!Passw0rd","birth_date":"1990-01-01"}'

# Login and grab tokens:
curl -s -X POST "$BASE/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"S3cure!Passw0rd"}'
```

---

## 9. Admin console (`/admin`)

The operator panel at `/admin` is **separate** from the mobile API auth: it has
its own login, its own opaque session cookie (only a SHA-256 hash is stored),
idle (default 60 min) and absolute (default 8 h) timeouts, CSRF on every
mutation, an append-only audit log, and an optional TOTP second factor.

### Provisioning admins (production)

The API creates the operator account **by itself**, on every boot, from two
environment variables (`app/db/bootstrap.py`):

| Variable | Example | Purpose |
| --- | --- | --- |
| `ADMIN_BOOTSTRAP_EMAIL` | `ops@example.com` | Login of the operator account. |
| `ADMIN_BOOTSTRAP_PASSWORD` | `openssl rand -hex 16` | Password — applied **only** when the account is created. |
| `ADMIN_BOOTSTRAP_NAME` | `Administrador` | Display name of the account. |
| `ADMIN_BOOTSTRAP_PROMOTE_EXISTING` | `false` | Promote an existing (non-admin) account with the same e-mail.  Off by default. |

**First deploy**

1. Set `ADMIN_BOOTSTRAP_EMAIL` and `ADMIN_BOOTSTRAP_PASSWORD` on the
   `novaiax-gateway` service (both are `sync: false` in the Blueprint, so
   Render asks for them on the first sync).
2. Deploy.  The API creates the ADMIN account right after the migrations
   (the insert is idempotent: `ON CONFLICT (email) DO NOTHING`, so several
   workers booting at once still create exactly one account).
3. Log in at **`/admin/login`** with those credentials.  With
   `ADMIN_REQUIRE_TOTP=true` the account must enrol TOTP on the first login.
4. **Remove both variables** from the service's Environment tab.  The password
   is never re-applied — an existing account keeps its own password forever —
   so leaving them behind only keeps a secret in the dashboard.

The old `ADMIN_DEFAULT_EMAIL` / `ADMIN_DEFAULT_PASSWORD` names still work as
deprecated aliases (they log one warning on startup); rename them.

In production a malformed e-mail or a weak password (fewer than 12 characters
or one of `admin`, `admin123`, `password`, `12345678`, the `.env.example`
placeholders) is **refused**: the API logs the reason and keeps serving
traffic without creating the account.  When no admin exists at all and no
bootstrap variable is set, the API logs one warning that the panel has no
operator.

There is **no admin registration endpoint**.  To promote an account that
already exists, generate the SQL locally (the script only prints, it never
connects):

```bash
python scripts/make_admin_sql.py --name "Ops" --email ops@example.com
# prompt: ADMIN_PASSWORD env or interactive getpass
```

Paste the emitted `UPDATE`/`INSERT … ON CONFLICT (email) DO NOTHING` into the
Supabase SQL editor (or `psql`): the `UPDATE` only changes the role and the
`INSERT` only applies the password when the account is created, so an
existing password is never overwritten.  Every non-admin (or unknown e-mail)
attempting the panel login gets a generic `401`, so an attacker cannot
enumerate admins.

### Relevant environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `ADMIN_BOOTSTRAP_EMAIL` | *(unset)* | E-mail of the operator account the API creates for itself on the first boot (§ 9). |
| `ADMIN_BOOTSTRAP_PASSWORD` | *(unset)* | Password of that account — applied **only** when the account is created (≥ 12 characters in production). |
| `ADMIN_BOOTSTRAP_NAME` | `Administrador` | Display name of the operator account. |
| `ADMIN_BOOTSTRAP_PROMOTE_EXISTING` | `false` | Promote an existing (non-admin) account with the same e-mail to ADMIN. |
| `ADMIN_PANEL_ENABLED` | `true` | When `false` every `/admin` route returns `404`. |
| `ADMIN_PANEL_COOKIE_NAME` | `novaiax_admin_panel` | Panel session cookie name (path `/admin`). |
| `ADMIN_REQUIRE_TOTP` | `false` | Force TOTP; admins without a secret are redirected to enroll. |
| `ADMIN_TOTP_ISSUER` | `NovaIAx Admin` | Label shown in the authenticator app. |
| `ADMIN_ALLOWED_IPS` | *(unset)* | Comma-separated allowlist; outside it `/admin` returns `403`. Behind a proxy set `TRUST_PROXY_HEADERS=true`. |
| `ADMIN_SESSION_IDLE_MINUTES` | `60` | Inactivity timeout. |
| `ADMIN_SESSION_ABSOLUTE_HOURS` | `8` | Hard session lifetime. |

The panel serves **aggregate counters only** (users, prompts, open reports,
messages today, active subscriptions by plan) — never chats, notes, health,
finance, study or logbook content.

### Agent console (`/admin/agents`)

The **Agentes** tab manages the prompt catalog without code: audience groups,
sub-agents and custom-prompt agents.  Each change is audited, and every prompt
save appends an immutable row to `prompt_versions` (viewable, diffable and
rollback-able from the “Versões” screen).  The public catalog endpoints
(`/api/v1/agent-groups`, `/api/v1/sub-agents`, `/api/v1/system-prompts/`) list
**only active** entries, in `sort_order`, and carry `ETag`/`Cache-Control` so
clients revalidate cheaply; activation changes invalidate the relevant AI
caches automatically.

Every production AI request gets the platform-owned **safety preamble**
prepended by the router, after the admin prompt is loaded — an admin prompt
cannot remove or override it.  A deny-list linter blocks saves that try
(`prompt_safety`), and the “Testar agente” sandbox runs an unsaved draft
through the router (quota-exempt, per-admin rate-limited).

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGENT_PROMPT_MAX_CHARS` | `5000` | Hard cap for any admin-authored prompt fragment. |
| `AGENT_PREVIEW_RATE_LIMIT` | `10` | “Testar agente” attempts per admin per window (`0` disables). |
| `AGENT_PREVIEW_RATE_WINDOW_SECONDS` | `60` | Sandbox rate-limit window length. |

---

## 10. Supabase notes (Postgres / Data API)

If the database is a Supabase project (instead of Render-managed Postgres):

- **Connection for Alembic → `MIGRATION_DATABASE_URL`:** use the Supavisor
  **session mode** URL — pooler host
  (`<project>-<region>.pooler.supabase.com`), **port `5432`**, IPv4
  compatible:
  ```
  postgresql://<user>:<pass>@<project>-<region>.pooler.supabase.com:5432/postgres
  ```
  Set it as `MIGRATION_DATABASE_URL` (§ "Render environment"), *not* as
  `DATABASE_URL`, so Alembic and the app can point at different endpoints.
  `migrations/env.py` prefers `MIGRATION_DATABASE_URL` and falls back to
  `DATABASE_URL` when it is unset.  **Never** use:
  - the **transaction pooler** (`6543`) — PgBouncer multiplexes sessions and
    cannot hold the prepared statements/locks Alembic needs;
  - the **direct host** (`db.<ref>.supabase.co`) when it is IPv6-only and the
    build/runtime environment is IPv4 — connections simply time out.
- **Connection for the app → `DATABASE_URL`:** the async engine tolerates the
  transaction pooler (`6543`) — `app/db/session.py` sets
  `statement_cache_size=0` for it to avoid the
  `prepared statement … does not exist` PgBouncer error (that setting is
  applied automatically whenever the URL's port is `6543`).  The session
  pooler (port `5432`) also works if you prefer it, in which case
  `MIGRATION_DATABASE_URL` can be left unset and migrations reuse the same
  URL.
- **Row Level Security**: the admin-console migration enables RLS (no policies)
  on **every** table in the `public` schema.  The backend connects as the table
  **owner**, which bypasses RLS, so the API keeps working; the Supabase
  **Data API (PostgREST) anon/authenticated roles** see **no rows**.  This is
  intentional — the app is the only data path.  If you ever expose tables via
  the Data API, add explicit policies first.
- Do **not** enable `FORCE ROW LEVEL SECURITY` unless you also add policies for
  the owner — that would lock the backend out of its own tables.

---

## Redeploys / future deploys

```bash
git pull && git push     # merge into main → auto-deploy
# or manually: render deploys create novaiax-gateway --wait
# watch: render logs --resources <service-id> --tail
```

Migrations re-run through `entrypoint.sh` on each deploy (idempotent no-op if
nothing changed).

> If you ever delete the Blueprint resources and re-sync, you must re-enter all
> `sync: false` secrets (§ 3) — and the free Postgres 30-day clock restarts.

---

## Common issues

| Symptom | Fix |
| --- | --- |
| `/health` returns `redis: error` | Wrong/missing `REDIS_URL`; verify the Upstash `rediss://…` string in the service's **Environment** tab, then redeploy/restart. |
| `/health` returns `db: error` | `DATABASE_URL` missing or the DB expired/upgraded. Re-check the `fromDatabase` link and the Postgres resource status. |
| Deploy fails at start (`entrypoint` exits non-zero) | `alembic upgrade head` failed — read the deploy logs (`render logs -r <id> --text alembic --tail`) and check migration filenames vs. models. |
| Deploy crashes with *The SQLAlchemy asyncio module requires that the Python greenlet library is installed*, then Render reports **“No open ports detected”** | The image lacks `greenlet`/`sqlalchemy[asyncio]` — `requirements.txt` must list both (they are runtime deps in `requirements.txt` **and** `pyproject.toml`). uvicorn never starts because the entrypoint exits on the failed migration, so the service has no listening port. |
| First request after idle is slow / times out | Free-tier cold start (~30–60 s). Raise the Flutter client timeout to ≥ 60–90 s or upgrade to a paid plan (§ 7). |
| 401 on everything including `/health` | `/health` is exempt; if other routes 401, check `SECRET_KEY`/token configuration. |
| `SECRET_KEY must be set…` or `SECRET_KEY must contain at least 32 bytes…` | `ENVIRONMENT=production` requires a non-placeholder `SECRET_KEY` of at least 32 bytes. Set it in the service's **Environment** tab (for example, generate with `openssl rand -hex 32`) and redeploy. Blueprint `generateValue` does not replace an existing service value. |
| `MASTER_API_KEY must contain at least 32 bytes…` | Generate a key with `openssl rand -hex 32`, set it in the service's **Environment** tab, and redeploy. |
| Secret added in dashboard not taking effect | `sync: false` values are only prompted on *initial* Blueprint sync; edit them directly on the service's **Environment** page, then redeploy. |
| Commit to `main` doesn't deploy | Check **Auto-Deploy** on the service (Blueprint sets `commit`), and that the Blueprint repo/branch match. |