#!/bin/sh
# NovaIAx container entrypoint.
#
# Runs on every container start (first boot, redeploys, and free-tier
# cold starts) and performs two jobs:
#
#   1. Apply pending database migrations (idempotent — a no-op when the
#      schema is already up to date).
#   2. Start the uvicorn HTTP server.
#
# ``set -e`` makes a failed migration stop the script: the container exits
# non-zero, Render never starts serving traffic from this instance, and the
# deploy is marked failed.  This mirrors the guardrail a platform-level
# pre-deploy hook would provide, at the cost of running migrations inside
# the same instance that serves traffic (see DEPLOY.md for the tradeoff).
set -e

echo "[entrypoint] Applying database migrations (alembic upgrade head)..."
alembic upgrade head

echo "[entrypoint] Starting uvicorn..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000