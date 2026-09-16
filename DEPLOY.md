# Deployment — Fly.io

Step-by-step guide for deploying NovaIAx (monolithic FastAPI gateway + backend)
to Fly.io.  The app runs as a single Fly app with a Fly Postgres database and an
Upstash Redis cache.

> **Assumes**: Prerequisites 1–5 are applied (rate limiter reads `Fly-Client-IP`,
> `/health` verifies DB + Redis, Alembic migrations exist, security/secret
> validation is in place, JWT/API-key roles are wired up).

---

## 1. Prerequisites

- A [Fly.io](https://fly.io) account and the Fly CLI installed:
  ```bash
  curl -L https://fly.io/install.sh | sh
  # or: brew install flyctl
  fly auth login
  ```
- A [Groq API key](https://console.groq.com/) (used by the AI endpoints).
- An [Upstash Redis](https://upstash.com/) account (or any Redis provider).

---

## 2. Create the Fly app

`fly.toml` already exists in the repo, so you attach it to a new app on your
account rather than using `fly launch` (which would generate a conflicting
config):

```bash
fly apps create novaiax-gateway --machines
# or, if the name is taken, pick a unique one:
# fly apps create novaiax-gateway-<yourname> --machines
# …and then set the matching ``app = "…"`` in fly.toml.
```

If you prefer an interactive run that asks you for a name and region:

```bash
fly launch --name novaiax-gateway --region gru --org personal --no-deploy --copy-config
```

`--copy-config` keeps the existing `fly.toml`; `--no-deploy` prevents an
immediate deploy before secrets/database are configured.

> **Region note**: `primary_region = "gru"` (São Paulo) is set in `fly.toml`
> because the user base is Brazil-centric.

---

## 3. Provision the database (Fly Postgres)

Create a small PostgreSQL cluster and attach it to the app:

```bash
fly postgres create --name novaiax-db --region gru --initial-cluster-size 1 --vm-size shared-cpu-1x --volume-size 1
```

- `shared-cpu-1x` is the cheapest VM; `--volume-size 1` (GB) keeps storage costs
  near zero for an early-stage app.
- If you didn't scope to a region at creation, you can set it later with
  `fly regions set gru`.

Attach the database to your app (this writes `DATABASE_URL` into the app's
secrets automatically):

```bash
fly postgres attach novaiax-db --app novaiax-gateway
```

Verify the connection string got injected:

```bash
fly secrets list
```

The attached URL will look like `postgresql://novaiax:<password>@novaiax-db.flycast:5432/...`.
The app expects an **asyncpg** URL, so the secret will need to be rewritten as
`postgresql+asyncpg://…` (see the secrets section below).

---

## 4. Provision Redis (Upstash)

NovaIAx depends on Redis for rate limiting, caching, conversation history, API
keys, and token revocation.  Fly has no managed Redis, so use Upstash:

1. Create an account at <https://upstash.com> and log into the console.
2. **Create database** → choose **Redis**, pick a name (e.g. `novaiax`), and
   select the region closest to your users (Upstash São Paulo region if
   available, otherwise `sa-east-1`).
3. Enable **TLS** (default) — the connection string will start with `rediss://`.
4. Copy the **REST / connection URL** from the dashboard:
   ```
   rediss://default:<password>@<region>-<something>.upstash.io:6379
   ```
   This goes into the `REDIS_URL` secret below.

> Alternative: self-host Redis on another Fly app (`fly launch` + `redis:7-alpine`
> + a volume) or use any provider offering a URL.  Upstash's free tier is
> sufficient for low-traffic stages.

---

## 5. Set secrets

Set **all** secrets with `fly secrets set`.  Values marked `<…>` must be
replaced with your own; generating good ones is shown where relevant.

```bash
# ---- Authentication / security ---------------------------------------------
fly secrets set "SECRET_KEY=$(openssl rand -hex 32)"

fly secrets set "MASTER_API_KEY=$(openssl rand -hex 24)"

# Password for the legacy default admin flow (used by admins created via
# scripts/tests only; real users sign up through /auth/register).
fly secrets set "DEFAULT_ADMIN_PASSWORD=change-this-to-a-strong-password"

# ---- Groq Cloud AI ----------------------------------------------------------
fly secrets set "GROQ_API_KEY=<your-groq-api-key>"

# ---- Database (attached above, but must be in asyncpg form) -----------------
# Get the raw URL Fly attached:
fly secrets list
# It will look like:  postgresql://novaiax:XXXXX@novaiax-db.flycast:5432/novaiax
# Rewrite the scheme to asyncpg:
fly secrets set "DATABASE_URL=postgresql+asyncpg://novaiax:<XXXXX>@novaiax-db.flycast:5432/novaiax"

# ---- Redis ------------------------------------------------------------------
fly secrets set "REDIS_URL=rediss://default:<upstash-password>@<region>-<something>.upstash.io:6379"

# ---- CORS (optional; defaults to localhost only) ----------------------------
# Comma-separated origins that may call the API from browsers.
fly secrets set "ALLOWED_ORIGINS=https://app.yourdomain.com,https://admin.yourdomain.com"
```

**Complete secret list** (reference for audits/rotation):

| Secret                    | Example                                          |
| ------------------------- | ------------------------------------------------ |
| `SECRET_KEY`              | 64-hex random (`openssl rand -hex 32`)           |
| `MASTER_API_KEY`          | 48-hex random (`openssl rand -hex 24`)           |
| `DEFAULT_ADMIN_PASSWORD`  | strong password                                  |
| `GROQ_API_KEY`            | `gsk_…`                                          |
| `DATABASE_URL`            | `postgresql+asyncpg://user:pass@app.flycast:5432/novaiax` |
| `REDIS_URL`               | `rediss://default:pass@host.upstash.io:6379`     |
| `ALLOWED_ORIGINS`         | `https://app.example.com`                        |

> Non-secret env vars (`ENVIRONMENT`, `GROQ_API_MODEL`, `RATE_LIMIT_*`,
> `CACHE_TTL_*`, `MAX_PAYLOAD_BYTES`, `TRUST_PROXY_HEADERS`) live in the
> `[env]` section of `fly.toml` already — no need to set them manually.

> `DEFAULT_ADMIN_PASSWORD` is only referenced by the legacy flow; the app
> boots fine without it, but it is required by `_validate_production_secrets`
> in `app/core/config.py` — do not skip it.

---

## 6. Deploy

```bash
fly deploy
```

What happens automatically (from `fly.toml`):

1. `release_command = "alembic upgrade head"` runs on a throwaway machine —
   applies migrations to Fly Postgres before any new machine serves traffic.
2. The `api` machine boots `uvicorn app.main:app` on port 8000.
3. Fly waits (`grace_period = "10s"`) then probes `GET /health` every `15s`.
   If DB **and** Redis are reachable, Fly marks the machine ready and starts
   routing traffic.  Otherwise the machine stays unhealthy until the checks pass.
4. With `min_machines_running = 0` + `auto_stop_machines`/`auto_start_machines`,
   the machine scales to zero when idle and cold-starts on the next request
   (5–15 s delay on first hit).  Set `min_machines_running = 1` in `fly.toml`
   to keep one machine always warm — costs a bit more, removes cold starts.

---

## 7. Verify the deployment

Tail the logs:

```bash
fly logs
# Recent log lines will include startup messages, the release_command
# migration output, and per-request structured JSON logs.
```

Check the health endpoint directly:

```bash
# Health is public (exempt from auth + rate limiting):
curl -s https://novaiax-gateway.fly.dev/health
# => {"status":"ok","db":"ok","redis":"ok"}
```

A non-200 or `{"status":"error"}` means one dependency is down — the endpoint
reports which one in `db` / `redis`.

Inspect machine status:

```bash
fly status
# Look for "running", 1 desired, 1 healthy.
```

Put the hostname in real traffic from `gru`:

```bash
curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" https://novaiax-gateway.fly.dev/health
curl -s https://novaiax-gateway.fly.dev/docs           # Swagger UI
```

---

## 8. First real users / smoke test

```bash
# Register the first user (becomes admin automatically):
curl -s -X POST https://novaiax-gateway.fly.dev/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"full_name":"Admin","email":"admin@example.com","password":"S3cure!Passw0rd"}'

# Login and grab tokens:
curl -s -X POST https://novaiax-gateway.fly.dev/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"S3cure!Passw0rd"}'
```

---

## Redeploys / future deploys

```bash
git pull         # get latest code
fly deploy       # release_command runs migrations again (idempotent no-op)
fly logs         # watch it come up
```

## Common issues

| Symptom                                        | Fix                                                                 |
| ---------------------------------------------- | ------------------------------------------------------------------- |
| `/health` returns `redis: error`               | Wrong/missing `REDIS_URL`; verify the Upstash `rediss://…` string.  |
| `/health` returns `db: error`                  | `DATABASE_URL` not in `asyncpg` form, or the DB is asleep. Restart it. |
| App boots then shuts down                      | `release_command` failed — read the release logs: `fly deploy` output (or `fly logs`). Check migration filenames vs. models. |
| Cold start feels slow                          | Acceptable for early stage.  Or set `min_machines_running = 1`.     |
| 401 on everything including `/health`          | `/health` is exempt; if other routes 401, set `SECRET_KEY`/tokens correctly. |
| `SECRET_KEY must be set…Raises RuntimeError`   | `ENVIRONMENT=production` + a placeholder/empty `SECRET_KEY`. Rotate it. |