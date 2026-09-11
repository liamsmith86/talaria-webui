"""Configured external paths; forwarded headers never decide routing or trust."""

from starlette.responses import RedirectResponse


class PublicPath:
    def __init__(self, app, prefix):
        self.app, self.prefix = app, prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.prefix:
            path = scope["path"]
            if path == self.prefix:
                query = scope.get("query_string", b"").decode("latin-1")
                target = self.prefix + "/" + ("?" + query if query else "")
                return await RedirectResponse(target, status_code=308)(scope, receive, send)
            # Accept both preserving and stripping reverse proxies. Internal
            # health probes can still use /health without the external prefix.
            scope = dict(scope, root_path=self.prefix)
            if not path.startswith(self.prefix + "/"):
                scope["path"] = self.prefix + path
                scope["raw_path"] = self.prefix.encode() + scope.get("raw_path", path.encode())
        return await self.app(scope, receive, send)
