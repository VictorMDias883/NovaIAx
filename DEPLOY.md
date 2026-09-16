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
| Web service `novaiax-gateway` | `runtime: docker` — uses the existing `Dockerfile` as-is; region **`virginia`**, plan **`free`**, health check at **`/health`**. |
| PostgreSQL `novaiax-db` | Render-managed Postgres, plan `free`. Its connection string is injected into the web service's `DATABASE_URL` via `fromDatabase` (no manual URL rewriting — the app normalises `postgresql://` → `postgresql+asyncpg://` internally). |
| Secrets | `SECRET_KEY`, `GROQ_API_KEY`, `MASTER_API_KEY`, `DEFAULT_ADMIN_PASSWORD`, `REDIS_URL`, all declared `sync: false` → Render prompts for them on first sync; never stored in the repo. |

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
`sync: false` variable.  Fill them in:

| Secret | Value |
| --- | --- |
| `SECRET_KEY` | `openssl rand -hex 32` (64-hex random) |
| `MASTER_API_KEY` | `openssl rand -hex 24` (48-hex random) |
| `GROQ_API_KEY` | your `gsk_…` key from [console.groq.com](https://console.groq.com) |
| `DEFAULT_ADMIN_PASSWORD` | a strong password (required by `_validate_production_secrets` in `app/core/config.py` even if unused) |
| `REDIS_URL` | your Upstash `rediss://…` connection string (§ 4) |

**Rotating / editing later** — Blueprint updates skip `sync: false`, so manage
these per service:

1. Dashboard → your **service** (`novaiax-gateway`) → **Environment** tab.
2. Click **Add Environment Variable** (or the value field to edit an existing
   one); tick *Secret*.
3. Save, then **Manual Deploy → Deploy latest commit** (or **Restart service**)
   to apply them.

> Keep secrets out of `render.yaml` and out of git — only non-secret config
> (ENVIRONMENT, GROQ_API_MODEL, RATE_LIMIT_*, CACHE_TTL_*, MAX_PAYLOAD_BYTES,
> TRUST_PROXY_HEADERS) is declared as plain `value:` entries there.  `DATABASE_URL`
> is injected automatically by `fromDatabase` and needs no action.

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
  is current), then `exec uvicorn app.main:app --host 0.0.0.0 --port 8000`.
- With `set -e`, a **failed migration stops the script**: the container exits
  non-zero, uvicorn never starts, and Render fails the deploy (old version keeps
  serving via zero-downtime).

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

```bash
BASE=https://novaiax-gateway.onrender.com

# Register the first user (becomes admin automatically):
curl -s -X POST "$BASE/api/v1/auth/register" \
  -H "Content-Type: application/json" \
  -d '{"full_name":"Admin","email":"admin@example.com","password":"S3cure!Passw0rd"}'

# Login and grab tokens:
curl -s -X POST "$BASE/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"S3cure!Passw0rd"}'
```

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
| First request after idle is slow / times out | Free-tier cold start (~30–60 s). Raise the Flutter client timeout to ≥ 60–90 s or upgrade to a paid plan (§ 7). |
| 401 on everything including `/health` | `/health` is exempt; if other routes 401, check `SECRET_KEY`/token configuration. |
| `SECRET_KEY must be set…Raises RuntimeError` | `ENVIRONMENT=production` + placeholder `SECRET_KEY`. Rotate it in the Env/Secrets tab and redeploy. |
| Secret added in dashboard not taking effect | `sync: false` values are only prompted on *initial* Blueprint sync; edit them directly on the service's **Environment** page, then redeploy. |
| Commit to `main` doesn't deploy | Check **Auto-Deploy** on the service (Blueprint sets `commit`), and that the Blueprint repo/branch match. |