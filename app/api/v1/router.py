"""
API v1 router — the central routing hub.

This module creates the top-level :class:`APIRouter` for version 1 of
the API and includes all sub-routers.  The router is mounted at
``/api/v1`` in :mod:`app.main`.

Architecture:
    main.py
      └── v1_router (prefix="/api/v1")
            ├── auth_router            (prefix="/auth",              tags=["auth"])
            ├── objective_router       (prefix="/objectives",        tags=["objectives"])
            ├── objective_assistant    (prefix="/objectives/assistant", tags=["objectives"])
            ├── roadmap_day_router     (prefix="/objectives",        tags=["objectives"])
            ├── general_agent_router   (prefix="/agents/general",    tags=["agents"])
            ├── user_router            (prefix="/users",             tags=["users"])
            ├── proxy_router           (prefix="/proxy",             tags=["proxy"])
            ├── system_prompt_router   (prefix="/system-prompts",    tags=["system-prompts"])
            ├── chat_router            (prefix="/ai",                tags=["ai"])
            └── cache_router           (prefix="/cache",             tags=["cache"])
"""

from fastapi import APIRouter

from app.api.v1.auth import router as auth_router
from app.api.v1.cache_router import router as cache_router
from app.api.v1.chat_router import router as chat_router
from app.api.v1.general_agent_router import router as general_agent_router
from app.api.v1.objective_assistant_router import router as objective_assistant_router
from app.api.v1.objective_router import router as objective_router
from app.api.v1.proxy import router as proxy_router
from app.api.v1.roadmap_day_router import router as roadmap_day_router
from app.api.v1.system_prompt_router import router as system_prompt_router
from app.api.v1.user_router import router as user_router

# Create the top-level v1 router.  Individual sub-routers define their
# own prefixes and tags.
router = APIRouter()

# Include each sub-router.  The sub-routers' own prefixes are appended
# to the ``/api/v1`` prefix set in ``main.py``.
router.include_router(auth_router)
router.include_router(cache_router)
router.include_router(objective_router)
router.include_router(objective_assistant_router)
router.include_router(roadmap_day_router)
router.include_router(user_router)
router.include_router(proxy_router)
router.include_router(system_prompt_router)
router.include_router(chat_router)
router.include_router(general_agent_router)
