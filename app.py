#!/usr/bin/env python3
"""Telegram Research Access Service - MTProto Telethon REST API for agents"""
import os, asyncio, logging
from datetime import datetime, timezone
from typing import Optional, Dict
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query, Header, Depends
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import User, Channel, Chat
from telethon.errors import FloodWaitError, ChannelPrivateError, UsernameNotOccupiedError, ChannelInvalidError

API_ID = int(os.getenv("TELEGRAM_API_ID", "37261813"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "4910f18f0d2a51ea977aabff59941644")
SESSION_STRING = os.getenv("TELEGRAM_SESSION", "")
API_KEY = os.getenv("ERMI_API_KEY", "ermi-research-key-change-me")
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "./media_cache"))
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
MAX_LIMIT = 500
DEFAULT_LIMIT = 50
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("telegram-research")
client = None
client_lock = asyncio.Lock()
checkpoints: Dict[str, int] = {}

def load_session() -> str:
    if SESSION_STRING: return SESSION_STRING.strip()
    p = Path("ermi.session.string")
    if p.exists(): return p.read_text().strip()
    raise RuntimeError("No TELEGRAM_SESSION env or session file")

async def get_client() -> TelegramClient:
    global client
    if client is None or not client.is_connected():
        async with client_lock:
            if client is None or not client.is_connected():
                client = TelegramClient(StringSession(load_session()), API_ID, API_HASH)
                await client.connect()
                if not await client.is_user_authorized():
                    raise RuntimeError("Session not authorized")
                log.info("Telethon client connected")
    return client

async def require_api_key(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    api_key: Optional[str] = Query(None, description="API key / password alias"),
    password: Optional[str] = Query(None, description="Password preferred for agents"),
):
    if os.getenv("ALLOW_NO_AUTH", "0") == "1":
        return "open"
    key = password or api_key or x_api_key
    if not key or key != API_KEY:
        raise HTTPException(status_code=401, detail={
            "ok": False,
            "error": "UNAUTHORIZED",
            "message": "Valid password required. Add ?password=YOUR_PASSWORD"
        })
    return key

def error_response(code: str, message: str, status: int = 400, **extra):
    return JSONResponse(status_code=status, content={"ok": False, "error": code, "message": message, **extra})

async def resolve_target(cl, target: str):
    t = target.strip()
    if t.startswith("https://t.me/"): t = t.replace("https://t.me/", "")
    if t.startswith("t.me/"): t = t[5:]
    if t.startswith("@"): t = t[1:]
    if t.lstrip("-").isdigit():
        try: return await cl.get_entity(int(t))
        except Exception:
            cid = int(t)
            if cid > 0: cid = int(f"-100{cid}")
            return await cl.get_entity(cid)
    try: return await cl.get_entity(t)
    except (UsernameNotOccupiedError, ValueError, ChannelInvalidError):
        raise ValueError(f"NOT_FOUND: {target}")
    except ChannelPrivateError:
        raise ValueError("ACCESS_DENIED: private")

def serialize_sender(sender):
    if sender is None: return None
    if isinstance(sender, User):
        return {"id": sender.id, "username": sender.username, "first_name": sender.first_name, "last_name": sender.last_name, "type": "user"}
    if isinstance(sender, (Channel, Chat)):
        return {"id": sender.id, "title": getattr(sender, "title", None), "username": getattr(sender, "username", None), "type": "channel" if isinstance(sender, Channel) else "chat"}
    return {"id": getattr(sender, "id", None), "type": "unknown"}

async def serialize_message(cl, msg, download_media=False):
    sender = None
    try: sender = await msg.get_sender()
    except Exception: pass
    permalink = None
    try:
        entity = await msg.get_chat()
        if getattr(entity, "username", None):
            permalink = f"https://t.me/{entity.username}/{msg.id}"
        else:
            cid = str(abs(msg.chat_id))
            if str(msg.chat_id).startswith("-100"): cid = str(msg.chat_id)[4:]
            permalink = f"https://t.me/c/{cid}/{msg.id}"
    except Exception: pass
    media_info = None
    if msg.media:
        media_info = {"type": type(msg.media).__name__, "available": False, "has_media": True}
        if download_media:
            try:
                fname = f"{msg.chat_id}_{msg.id}"
                path = MEDIA_DIR / fname
                dl = await cl.download_media(msg, file=str(path))
                if dl:
                    path = Path(dl)
                    media_info["available"] = True
                    media_info["local_path"] = str(path)
                    media_info["size"] = path.stat().st_size
                    media_info["download_url"] = f"/telegram/media/file?path={path.name}"
            except Exception as e:
                media_info["error"] = str(e)
    return {
        "message_id": msg.id,
        "date": msg.date.astimezone(timezone.utc).isoformat() if msg.date else None,
        "text": msg.text or msg.message or None,
        "sender": serialize_sender(sender),
        "chat_id": str(msg.chat_id),
        "media": media_info,
        "url": permalink,
        "is_forward": bool(msg.forward),
        "views": getattr(msg, "views", None),
    }

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting Telegram Research Service")
    try:
        cl = await get_client()
        me = await cl.get_me()
        log.info(f"Authorized as {me.id} @{me.username}")
    except Exception as e:
        log.error(f"Init failed: {e}")
    yield
    if client and client.is_connected():
        await client.disconnect()

app = FastAPI(title="Telegram Research Access Service", version="1.1.0", lifespan=lifespan)

CATALOG = {
    "service": "Telegram Research Access Service",
    "source": "telegram_mtproto",
    "auth": "none when ALLOW_NO_AUTH=1",
    "how_agents_use": "Plain HTTP GET. No MCP required. Parse JSON response.",
    "base_url_hint": "Use the public Railway URL for this deployment",
    "tools": [
        {"name": "health", "method": "GET", "path": "/health", "description": "Session health and authorized user"},
        {"name": "me", "method": "GET", "path": "/telegram/me", "description": "Logged-in account profile"},
        {"name": "dialogs", "method": "GET", "path": "/telegram/dialogs?limit=30", "description": "List chats/channels/groups this account can access"},
        {"name": "history", "method": "GET", "path": "/telegram?target=@channel&limit=50", "description": "Message history; optional before/after message ids"},
        {"name": "search", "method": "GET", "path": "/telegram/search?target=@channel&q=keyword", "description": "Keyword search in a chat"},
        {"name": "message", "method": "GET", "path": "/telegram/message?target=@channel&message_id=123", "description": "Single message by id"},
        {"name": "media", "method": "GET", "path": "/telegram/media?target=@channel&message_id=123&download=true", "description": "Download media and return path/url"},
        {"name": "media_file", "method": "GET", "path": "/telegram/media/file?path=FILENAME", "description": "Fetch previously downloaded file bytes"},
        {"name": "updates", "method": "GET", "path": "/telegram/updates?target=@channel&cursor=123", "description": "Incremental messages after cursor"},
    ],
    "examples": [
        "/telegram?target=@pir2011&limit=20",
        "/telegram/search?target=@pir2011&q=registration",
        "/telegram/dialogs?limit=20",
        "/telegram/me",
    ],
    "not_included": ["delete messages", "ban users", "join arbitrary private invites without care", "export full session string"],
}

@app.get("/")
async def root(format: Optional[str] = Query(None)):
    if format == "html":
        lines = ["<h1>Telegram Research API</h1>", "<p>Plain HTTP for agents. Type name then password=?password=SECRET. No browser needed.</p>", "<ul>"]
        for t in CATALOG["tools"]:
            lines.append(f"<li><b>{t['name']}</b> — <code>{t['method']} {t['path']}</code><br/>{t['description']}</li>")
        lines.append("</ul>")
        return HTMLResponse("\n".join(lines))
    return CATALOG

@app.get("/health")
async def health():
    try:
        cl = await get_client()
        authorized = await cl.is_user_authorized()
        me = await cl.get_me() if authorized else None
        return {"ok": True, "status": "healthy", "source": "telegram_mtproto", "authorized": authorized, "user_id": me.id if me else None, "username": me.username if me else None, "timestamp": datetime.now(timezone.utc).isoformat()}
    except Exception as e:
        return JSONResponse(status_code=503, content={"ok": False, "status": "unhealthy", "error": str(e)})

@app.get("/telegram/me")
async def telegram_me(api_key: str = Depends(require_api_key)):
    try:
        cl = await get_client()
        me = await cl.get_me()
        return {"ok": True, "source": "telegram_mtproto", "user": {"id": me.id, "username": me.username, "first_name": me.first_name, "last_name": me.last_name, "phone": getattr(me, "phone", None)}}
    except Exception as e:
        return error_response("TEMPORARY_ERROR", str(e), 500)

@app.get("/telegram/dialogs")
async def telegram_dialogs(limit: int = Query(30, ge=1, le=100), api_key: str = Depends(require_api_key)):
    try:
        cl = await get_client()
        dialogs = []
        async for d in cl.iter_dialogs(limit=limit):
            dialogs.append({
                "id": d.id,
                "title": d.title or d.name,
                "username": getattr(d.entity, "username", None),
                "unread": d.unread_count,
                "is_channel": d.is_channel,
                "is_group": d.is_group,
                "is_user": d.is_user,
            })
        return {"ok": True, "source": "telegram_mtproto", "count": len(dialogs), "dialogs": dialogs}
    except Exception as e:
        return error_response("TEMPORARY_ERROR", str(e), 500)

@app.get("/telegram")
async def get_history(target: str = Query(...), limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT), offset_id: int = Query(0), min_id: int = Query(0), max_id: int = Query(0), before: Optional[int] = Query(None), after: Optional[int] = Query(None), reverse: bool = Query(False), download_media: bool = Query(False), api_key: str = Depends(require_api_key)):
    try:
        cl = await get_client()
        entity = await resolve_target(cl, target)
        if before is not None: max_id = before
        if after is not None: min_id = after
        kwargs = {"limit": limit, "reverse": reverse}
        if offset_id: kwargs["offset_id"] = offset_id
        if min_id: kwargs["min_id"] = min_id
        if max_id: kwargs["max_id"] = max_id
        messages = []
        async for msg in cl.iter_messages(entity, **kwargs):
            messages.append(await serialize_message(cl, msg, download_media))
        return {"ok": True, "source": "telegram_mtproto", "target": target, "chat_id": str(getattr(entity, "id", None)), "title": getattr(entity, "title", None) or getattr(entity, "username", None), "count": len(messages), "messages": messages, "has_more": len(messages) == limit, "next_cursor": str(messages[-1]["message_id"]) if messages else None}
    except ValueError as e:
        msg = str(e)
        code = "ACCESS_DENIED" if "ACCESS_DENIED" in msg else "NOT_FOUND" if "NOT_FOUND" in msg else "RESOLVE_ERROR"
        return error_response(code, msg, 403 if code == "ACCESS_DENIED" else 404 if code == "NOT_FOUND" else 400)
    except FloodWaitError as e:
        return error_response("FLOOD_WAIT", f"wait {e.seconds}s", 429, retry_after=e.seconds)
    except Exception as e:
        log.exception("history failed")
        return error_response("TEMPORARY_ERROR", f"{type(e).__name__}: {e}", 500)

@app.get("/telegram/search")
async def search_messages(target: str = Query(...), q: str = Query(..., min_length=1, max_length=200), limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT), download_media: bool = Query(False), api_key: str = Depends(require_api_key)):
    try:
        cl = await get_client()
        entity = await resolve_target(cl, target)
        messages = []
        async for msg in cl.iter_messages(entity, search=q, limit=limit):
            messages.append(await serialize_message(cl, msg, download_media))
        return {"ok": True, "source": "telegram_mtproto", "target": target, "chat_id": str(getattr(entity, "id", None)), "title": getattr(entity, "title", None), "query": q, "count": len(messages), "messages": messages, "has_more": len(messages) == limit}
    except ValueError as e:
        return error_response("RESOLVE_ERROR", str(e), 400)
    except FloodWaitError as e:
        return error_response("FLOOD_WAIT", f"wait {e.seconds}s", 429, retry_after=e.seconds)
    except Exception as e:
        return error_response("TEMPORARY_ERROR", str(e), 500)

@app.get("/telegram/message")
async def get_message(target: str = Query(...), message_id: int = Query(..., ge=1), download_media: bool = Query(False), api_key: str = Depends(require_api_key)):
    try:
        cl = await get_client()
        entity = await resolve_target(cl, target)
        msgs = await cl.get_messages(entity, ids=message_id)
        msg = msgs[0] if isinstance(msgs, list) else msgs
        if not msg: return error_response("NOT_FOUND", f"Message {message_id} not found", 404)
        return {"ok": True, "source": "telegram_mtproto", "target": target, "message": await serialize_message(cl, msg, download_media)}
    except Exception as e:
        return error_response("TEMPORARY_ERROR", str(e), 500)

@app.get("/telegram/media")
async def get_media_meta(target: str = Query(...), message_id: int = Query(..., ge=1), download: bool = Query(True), api_key: str = Depends(require_api_key)):
    try:
        cl = await get_client()
        entity = await resolve_target(cl, target)
        msgs = await cl.get_messages(entity, ids=message_id)
        msg = msgs[0] if isinstance(msgs, list) else msgs
        if not msg or not msg.media: return error_response("MEDIA_UNAVAILABLE", "No media", 404)
        data = await serialize_message(cl, msg, download_media=download)
        return {"ok": True, "source": "telegram_mtproto", "target": target, "message_id": message_id, "media": data.get("media")}
    except Exception as e:
        return error_response("TEMPORARY_ERROR", str(e), 500)

@app.get("/telegram/media/file")
async def download_media_file(path: str = Query(...), api_key: str = Depends(require_api_key)):
    safe = Path(path).name
    full = MEDIA_DIR / safe
    if not full.exists() or not str(full.resolve()).startswith(str(MEDIA_DIR.resolve())):
        return error_response("NOT_FOUND", "File not found", 404)
    return FileResponse(full)

@app.get("/telegram/updates")
async def get_updates(target: str = Query(...), cursor: Optional[int] = Query(None), limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT), download_media: bool = Query(False), api_key: str = Depends(require_api_key)):
    try:
        cl = await get_client()
        entity = await resolve_target(cl, target)
        chat_id = str(getattr(entity, "id", None))
        last_id = cursor if cursor is not None else checkpoints.get(chat_id, 0)
        messages = []
        async for msg in cl.iter_messages(entity, min_id=last_id, limit=limit, reverse=True):
            messages.append(await serialize_message(cl, msg, download_media))
        new_cursor = last_id
        if messages:
            new_cursor = max(m["message_id"] for m in messages)
            checkpoints[chat_id] = new_cursor
        return {"ok": True, "source": "telegram_mtproto", "target": target, "chat_id": chat_id, "cursor": last_id, "next_cursor": new_cursor, "count": len(messages), "messages": messages, "has_more": len(messages) == limit}
    except Exception as e:
        return error_response("TEMPORARY_ERROR", str(e), 500)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
