#!/usr/bin/env python
"""Seed the agent personalization catalog (groups + sub-agents).

Idempotent: existing rows are refreshed by ``slug``, never duplicated.  Run it
on any environment that serves the personalization endpoints::

    python -m scripts.seed_agent_catalog
"""

import argparse
import asyncio
import logging
import os
import sys

# Allow ``python scripts/seed_agent_catalog.py`` as well as ``python -m``.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.seed_agent_catalog import seed_agent_catalog  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402

logger = logging.getLogger("seed_agent_catalog")


async def run() -> int:
    async with SessionLocal() as session:
        counts = await seed_agent_catalog(session)
    print(f"Catálogo do agente atualizado: {counts['groups']} grupos, {counts['sub_agents']} sub-agentes.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        return asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - top-level CLI guard
        logger.error("Falha ao popular o catálogo do agente: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
