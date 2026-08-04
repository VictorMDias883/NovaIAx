"""
API v1 router — the central routing hub.

This module creates the top-level :class:`APIRouter` for version 1 of
the API and includes all sub-routers (auth, objectives, proxy).  The
router is mounted at ``/api/v1`` in :mod:`app.main`.

Architecture:
    main.py
      └── v1_router (prefix="/api/v1")
            ├── auth_router       (prefix="/auth",       tags=["auth"])
            ├── auth_router_v2    (prefix="/auth",       tags=["auth"])
            ├── objective_router  (prefix="/objectives", tags=["objectives"])
            └── proxy_router      (prefix="/proxy",      tags=["proxy"])

Note: ``auth_router`` and ``auth_router_v2`` both use the ``/auth``
prefix, which means their routes coexist.  This allows the gateway to
support both the in-memory auth flow (v1) and the database-backed auth
flow (v2) simultaneously.
"""

from fastapi import APIRouter

from app.api.v1.auth import router as auth_router
from app.api.v1.auth_router import router as auth_router_v2
from app.api.v1.objective_router import router as objective_router
from app.api.v1.proxy import router as proxy_router

# Create the top-level v1 router.  Individual sub-routers define their
# own prefixes and tags.
router = APIRouter()

# Include each sub-router.  The sub-routers' own prefixes are appended
# to the ``/api/v1`` prefix set in ``main.py``.
router.include_router(auth_router)
router.include_router(auth_router_v2)
router.include_router(objective_router)
router.include_router(proxy_router)
