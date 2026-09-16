#!/usr/bin/env python3
"""Bootstrap: load known-good app body from sibling module once present."""
# Temporary bootstrap until full body is restored
import urllib.request
from pathlib import Path

BODY = Path(__file__).with_name("_body.py")
if not BODY.exists():
    url = "https://raw.githubusercontent.com/ermiyas48/telegram-research-mcp/936553f6e4eb0ed983c1cd2089278dc49b565aef/app.py"
    BODY.write_text(urllib.request.urlopen(url, timeout=30).read().decode())

# Path password support
_src = BODY.read_text()
if "PathApiKeyMiddleware" not in _src:
    _src = _src.replace(
        "from fastapi import FastAPI, HTTPException, Query, Header, Depends\n",
        "from fastapi import FastAPI, HTTPException, Query, Header, Depends, Request\nfrom path_auth import PathApiKeyMiddleware\n",
    )
    _src = _src.replace(
        "async def require_api_key(x_api_key: Optional[str] = Header(None, alias=\"X-API-Key\"), api_key: Optional[str] = Query(None)):\n    if os.getenv(\"ALLOW_NO_AUTH\", \"0\") == \"1\":\n        return \"open\"\n    key = x_api_key or api_key\n",
        "async def require_api_key(request: Request, x_api_key: Optional[str] = Header(None, alias=\"X-API-Key\"), api_key: Optional[str] = Query(None)):\n    if os.getenv(\"ALLOW_NO_AUTH\", \"0\") == \"1\":\n        return \"open\"\n    if getattr(request.state, \"path_api_key_ok\", False):\n        return \"path\"\n    key = x_api_key or api_key\n",
    )
    _src = _src.replace(
        'app = FastAPI(title="Telegram Research Access Service", version="1.1.0", lifespan=lifespan)',
        'app = FastAPI(title="Telegram Research Access Service", version="1.2.0", lifespan=lifespan)\napp.add_middleware(PathApiKeyMiddleware, api_key=API_KEY)',
    )
    BODY.write_text(_src)

exec(compile(BODY.read_text(), str(BODY), "exec"), globals())
