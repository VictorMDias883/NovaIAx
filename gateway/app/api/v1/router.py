from fastapi import APIRouter

from gateway.app.api.v1.auth import router as auth_router
from gateway.app.api.v1.auth_router import router as auth_router_v2
from gateway.app.api.v1.objective_router import router as objective_router
from gateway.app.api.v1.proxy import router as proxy_router

router = APIRouter()
router.include_router(auth_router)
router.include_router(auth_router_v2)
router.include_router(objective_router)
router.include_router(proxy_router)
