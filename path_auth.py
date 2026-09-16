"""Path-based API key: /k/{password}/... so agents need no headers."""
import os
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

class PathApiKeyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, api_key: str):
        super().__init__(app)
        self.api_key = api_key

    async def dispatch(self, request: Request, call_next):
        path = request.scope.get("path", "") or ""
        if path.startswith("/k/"):
            parts = path.split("/", 3)
            if len(parts) >= 3 and parts[1] == "k" and parts[2]:
                key = parts[2]
                rest = "/" + parts[3] if len(parts) > 3 and parts[3] else "/"
                allow_open = os.getenv("ALLOW_NO_AUTH", "0") == "1"
                if not allow_open and key != self.api_key:
                    return JSONResponse(
                        status_code=401,
                        content={
                            "ok": False,
                            "error": "UNAUTHORIZED",
                            "message": "Invalid password in URL. Use /k/PASSWORD/telegram?...",
                        },
                    )
                request.scope["path"] = rest
                request.scope["raw_path"] = rest.encode("utf-8")
                request.state.path_api_key_ok = True
        return await call_next(request)
