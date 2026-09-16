# ── Runtime ───────────────────────────────────────────────────────────────────
# Single-stage build.  Multi-stage was considered but rejected: the image's
# bulk is Python C extensions (asyncpg, cryptography, uvloop) that must be
# compiled in a full Python build environment.  Splitting stages would
# duplicate that work or require copying compiled artefacts, adding complexity
# for negligible size savings (~20 MB on a ~200 MB image).
FROM python:3.12-slim AS runtime

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# ── Dependency layer (cached unless requirements.txt changes) ──────────────────
# requirements.txt mirrors pyproject.toml's [project.dependencies] and is the
# source of truth for the Docker build's dependency layer.  This layer is
# cached across application code changes — rebuilds only trigger when the
# dependency list itself changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir --no-compile -r requirements.txt

# ── Application layer ─────────────────────────────────────────────────────────
# Copy the full application tree (app/, migrations/, alembic.ini, …).
# The dependency install above is already cached, so this layer is cheap — it
# only invalidates when application source files change.
COPY . .

# Ensure the migration/start entrypoint is executable regardless of how the
# repository checkout preserved file modes.
RUN chmod +x entrypoint.sh

EXPOSE 8000

# Run database migrations (alembic upgrade head) before starting uvicorn —
# see entrypoint.sh for details.  Render builds this Dockerfile as-is and
# runs the image's CMD on every instance spin-up.
CMD ["/app/entrypoint.sh"]
