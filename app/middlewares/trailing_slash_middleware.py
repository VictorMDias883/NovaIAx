"""Retry route matching with the opposite trailing-slash form."""

from starlette.routing import Match


class TrailingSlashMiddleware:
    """Internally normalize a request path when only its slash variant matches."""

    def __init__(self, app, router) -> None:
        self.app = app
        self.router = router

    def _matches_route(self, scope: dict[str, object]) -> bool:
        for route in self.router.routes:
            match, _ = route.matches(scope)
            if match is Match.FULL:
                return True
        return False

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and not self._matches_route(scope):
            path = scope["path"]
            if path != "/":
                candidate = path[:-1] if path.endswith("/") else f"{path}/"
                raw_path = scope.get("raw_path", path.encode("utf-8"))
                candidate_raw_path = raw_path[:-1] if raw_path.endswith(b"/") else raw_path + b"/"
                if path.endswith("/"):
                    candidate_raw_path = raw_path[:-1]
                candidate_scope = {**scope, "path": candidate, "raw_path": candidate_raw_path}
                if self._matches_route(candidate_scope):
                    scope["path"] = candidate
                    scope["raw_path"] = candidate_raw_path
        await self.app(scope, receive, send)
