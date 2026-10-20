#!/bin/sh
# NovaIAx container entrypoint.
#
# Runs on every container start (first boot, redeploys, and free-tier
# cold starts) and performs two jobs:
#
#   1. Apply pending database migrations (idempotent — a no-op when the
#      schema is already up to date).  Skippable with RUN_MIGRATIONS=false
#      for operators who run migrations out-of-band (pre-deploy hook, CI).
#   2. Start the uvicorn HTTP server on ``${PORT:-8000}`` — Render injects
#      ``PORT`` and expects the service to bind exactly to it.  The proxy
#      flags make uvicorn honour ``X-Forwarded-*`` from Render's edge proxy
#      (``--forwarded-allow-ips='*'`` is safe here because the container is
#      only reachable through that trusted proxy).
#
# ``set -e`` plus the explicit ``||`` guard below make a failed migration
# stop the script: a loud "migration failed" line is printed, the container
# exits non-zero, Render never starts serving traffic from this instance,
# and the deploy is marked failed.  This mirrors the guardrail a
# platform-level pre-deploy hook would provide, at the cost of running
# migrations inside the same instance that serves traffic (see DEPLOY.md
# for the tradeoff).
set -e

PORT="${PORT:-8000}"

# Normalise RUN_MIGRATIONS: default is "true"; accept 1/yes/on as true too.
RUN_MIGRATIONS="$(printf '%s' "${RUN_MIGRATIONS:-true}" | tr '[:upper:]' '[:lower:]')"
case "$RUN_MIGRATIONS" in
  1|true|yes|on) RUN_MIGRATIONS=true ;;
  *) RUN_MIGRATIONS=false ;;
esac

if [ "$RUN_MIGRATIONS" = "true" ]; then
  echo "[entrypoint] Applying database migrations (alembic upgrade head)..."
  if ! alembic upgrade head; then
    echo "[entrypoint] migration failed: 'alembic upgrade head' exited non-zero." >&2
    echo "[entrypoint] migration failed: refusing to start uvicorn; the container will exit so Render marks the deploy as failed." >&2
    exit 1
  fi
else
  echo "[entrypoint] RUN_MIGRATIONS=$RUN_MIGRATIONS — skipping 'alembic upgrade head'."
fi

echo "[entrypoint] Starting uvicorn on 0.0.0.0:${PORT} (proxy headers on)..."
exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT}" \
  --proxy-headers \
  --forwarded-allow-ips='*'
