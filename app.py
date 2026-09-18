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

# Bot API delivery + control (env only)
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CONTROL_BOT_ENABLED = os.getenv("CONTROL_BOT_ENABLED", "1") == "1"
CONTROL_BOT_ALLOWED_IDS = {int(x.strip()) for x in os.getenv("CONTROL_BOT_ALLOWED_IDS", "6725547584").split(",") if x.strip().isdigit()}
DEFAULT_DELIVER_CHAT_ID = int(os.getenv("DEFAULT_DELIVER_CHAT_ID", "6725547584"))
BOT_API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""
_control_bot_task = None
_control_bot_offset = 0

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

async def require_api_key(x_api_key: Optional[str] = Header(None, alias="X-API-Key"), api_key: Optional[str] = Query(None), password: Optional[str] = Query(None)):
    if os.getenv("ALLOW_NO_AUTH", "0") == "1":
        return "open"
    key = password or api_key or x_api_key
    if not key or key != API_KEY:
        raise HTTPException(status_code=401, detail={"ok": False, "error": "UNAUTHORIZED"})
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

@app.on_event("startup")
async def _start_control_bot():
    global _control_bot_task
    if CONTROL_BOT_ENABLED and BOT_TOKEN:
        _control_bot_task = asyncio.create_task(_control_bot_loop())
        log.info("Control bot task scheduled")

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
        lines = ["<h1>Telegram Research API</h1>", "<p>Plain HTTP for agents. No MCP.</p>", "<ul>"]
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



# === ERMI extended: dialogs with last_message, find_media, send ===
from pydantic import BaseModel, Field
from typing import Any

@app.get("/telegram/me")
@app.get("/me")
async def get_me_v2(api_key: str = Depends(require_api_key)):
    try:
        cl = await get_client()
        me = await cl.get_me()
        return {"ok": True, "source": "telegram_mtproto", "user": {"id": me.id, "username": me.username, "first_name": me.first_name, "last_name": me.last_name, "phone": getattr(me, "phone", None)}}
    except Exception as e:
        return error_response("TEMPORARY_ERROR", str(e), 500)

@app.get("/telegram/dialogs")
async def list_dialogs_v2(limit: int = Query(40, ge=1, le=200), api_key: str = Depends(require_api_key)):
    try:
        cl = await get_client()
        dialogs = []
        async for d in cl.iter_dialogs(limit=limit):
            ent = d.entity
            last = d.message
            last_preview = None
            if last:
                media_type = None
                if last.media:
                    from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
                    if isinstance(last.media, MessageMediaPhoto):
                        media_type = "photo"
                    elif isinstance(last.media, MessageMediaDocument) and last.document:
                        mt = last.document.mime_type or ""
                        media_type = "video" if mt.startswith("video/") else ("image" if mt.startswith("image/") else ("audio" if mt.startswith("audio/") else "document"))
                    else:
                        media_type = type(last.media).__name__
                last_preview = {"message_id": last.id, "date": last.date.isoformat() if last.date else None, "text": ((last.text or last.message or "")[:200] or None), "has_media": bool(last.media), "media_type": media_type}
            username = getattr(ent, "username", None)
            title = getattr(ent, "title", None) or " ".join(filter(None, [getattr(ent, "first_name", None), getattr(ent, "last_name", None)])) or username or str(ent.id)
            from telethon.tl.types import Channel, Chat, User
            dialogs.append({"id": ent.id, "title": title, "username": username, "unread": d.unread_count, "is_channel": isinstance(ent, Channel) and not getattr(ent, "megagroup", False), "is_group": isinstance(ent, Chat) or (isinstance(ent, Channel) and getattr(ent, "megagroup", False)), "is_user": isinstance(ent, User), "is_bot": bool(getattr(ent, "bot", False)), "target": ("@" + username) if username else str(ent.id), "last_message": last_preview})
        return {"ok": True, "source": "telegram_mtproto", "count": len(dialogs), "dialogs": dialogs, "hint": "Use target= dialog.target or dialog.id for history/search/media. last_message shows recent text/photo."}
    except Exception as e:
        return error_response("TEMPORARY_ERROR", str(e), 500)

@app.get("/telegram/find_media")
async def find_media_v2(target: str = Query(...), limit: int = Query(30, ge=1, le=100), media_type: str = Query(None), q: str = Query(None), download: bool = Query(False), api_key: str = Depends(require_api_key)):
    try:
        cl = await get_client()
        entity = await resolve_target(cl, target)
        want = (media_type or "").lower().strip() or None
        results = []
        kwargs = {"limit": min(max(limit * 4, 50), 200)}
        if q:
            kwargs["search"] = q
        async for msg in cl.iter_messages(entity, **kwargs):
            if not msg.media:
                continue
            info = await serialize_media(cl, msg, download=False) if "serialize_media" in dir() else None
            mtype = (info or {}).get("type") if info else None
            if not mtype:
                from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
                if isinstance(msg.media, MessageMediaPhoto):
                    mtype = "photo"
                elif isinstance(msg.media, MessageMediaDocument) and msg.document:
                    mt = msg.document.mime_type or ""
                    mtype = "video" if mt.startswith("video/") else ("image" if mt.startswith("image/") else "document")
                else:
                    mtype = "media"
            if want and want not in (mtype or "").lower():
                continue
            if "serialize_message" in dir():
                results.append(await serialize_message(cl, msg, include_media=True, download_media=download))
            else:
                results.append({"message_id": msg.id, "text": msg.text, "date": msg.date.isoformat() if msg.date else None, "media_type": mtype})
            if len(results) >= limit:
                break
        return {"ok": True, "source": "telegram_mtproto", "target": target, "count": len(results), "messages": results, "hint": "Then GET /telegram/media?target=...&message_id=ID&download=true&password=..."}
    except Exception as e:
        return error_response("TEMPORARY_ERROR", str(e), 500)

class SendBody(BaseModel):
    target: str
    text: str
    reply_to: int = None

@app.post("/telegram/send")
async def send_message_v2(body: SendBody, password: str = Query(None), api_key: str = Query(None), x_api_key: str = Header(None, alias="X-API-Key")):
    import os
    if os.getenv("ALLOW_NO_AUTH", "0") != "1":
        key = password or api_key or x_api_key
        if not key or key != API_KEY:
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail={"ok": False, "error": "UNAUTHORIZED", "message": "Valid password required"})
    try:
        cl = await get_client()
        entity = await resolve_target(cl, body.target)
        kwargs = {}
        if body.reply_to:
            kwargs["reply_to"] = body.reply_to
        msg = await cl.send_message(entity, body.text, **kwargs)
        return {"ok": True, "source": "telegram_mtproto", "action": "sent", "target": body.target, "message_id": msg.id, "text": msg.text, "date": msg.date.isoformat() if msg.date else None}
    except Exception as e:
        return error_response("TEMPORARY_ERROR", str(e), 500)


import httpx
from pydantic import BaseModel, Field

async def bot_api(method: str, data=None, files=None, timeout: float = 30.0):
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not configured")
    url = f"{BOT_API}/{method}"
    async with httpx.AsyncClient(timeout=timeout) as hx:
        if files:
            r = await hx.post(url, data=data or {}, files=files)
        else:
            r = await hx.post(url, json=data or {})
        return r.json()

def _auth_password(password=None, api_key=None, x_api_key=None):
    if os.getenv("ALLOW_NO_AUTH", "0") == "1":
        return
    key = password or api_key or x_api_key
    if not key or key != API_KEY:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail={"ok": False, "error": "UNAUTHORIZED", "message": "Valid password required"})

class DeliverBody(BaseModel):
    text: str = None
    chat_id: int = None
    document_url: str = None
    caption: str = None
    parse_mode: str = None

@app.get("/telegram/bot")
async def bot_status(api_key: str = Depends(require_api_key)):
    if not BOT_TOKEN:
        return {"ok": False, "bot_configured": False}
    try:
        data = await bot_api("getMe")
        result = data.get("result") or {}
        return {"ok": bool(data.get("ok")), "bot_configured": True, "bot": {"id": result.get("id"), "username": result.get("username"), "first_name": result.get("first_name")}, "default_deliver_chat_id": DEFAULT_DELIVER_CHAT_ID, "control_bot_enabled": CONTROL_BOT_ENABLED}
    except Exception as e:
        return error_response("BOT_ERROR", str(e), 500)

@app.post("/telegram/deliver")
async def deliver_message(body: DeliverBody, password: str = Query(None), api_key: str = Query(None), x_api_key: str = Header(None, alias="X-API-Key")):
    _auth_password(password, api_key, x_api_key)
    if not BOT_TOKEN:
        return error_response("BOT_NOT_CONFIGURED", "TELEGRAM_BOT_TOKEN missing", 503)
    if not body.text and not body.document_url:
        return error_response("BAD_REQUEST", "Provide text and/or document_url", 400)
    chat_id = body.chat_id or DEFAULT_DELIVER_CHAT_ID
    delivered = []
    try:
        if body.text:
            payload = {"chat_id": chat_id, "text": body.text}
            if body.parse_mode:
                payload["parse_mode"] = body.parse_mode
            data = await bot_api("sendMessage", payload)
            if not data.get("ok"):
                return error_response("DELIVER_FAILED", data.get("description", "sendMessage failed"), 502)
            msg = data["result"]
            delivered.append({"type": "text", "message_id": msg.get("message_id"), "chat_id": (msg.get("chat") or {}).get("id"), "date": msg.get("date")})
        if body.document_url:
            payload = {"chat_id": chat_id, "document": body.document_url}
            if body.caption:
                payload["caption"] = body.caption
            data = await bot_api("sendDocument", payload)
            if not data.get("ok"):
                return error_response("DELIVER_FAILED", data.get("description", "sendDocument failed"), 502)
            msg = data["result"]
            delivered.append({"type": "document", "message_id": msg.get("message_id"), "chat_id": (msg.get("chat") or {}).get("id"), "date": msg.get("date")})
        return {"ok": True, "source": "telegram_bot_api", "action": "delivered", "chat_id": chat_id, "count": len(delivered), "messages": delivered}
    except Exception as e:
        return error_response("TEMPORARY_ERROR", str(e), 500)

async def _control_bot_loop():
    global _control_bot_offset
    while True:
        try:
            if not BOT_TOKEN:
                await asyncio.sleep(30)
                continue
            data = await bot_api("getUpdates", {"offset": _control_bot_offset, "timeout": 25, "allowed_updates": ["message"]}, timeout=35.0)
            if not data.get("ok"):
                await asyncio.sleep(5)
                continue
            for upd in data.get("result") or []:
                _control_bot_offset = max(_control_bot_offset, int(upd.get("update_id", 0)) + 1)
                msg = upd.get("message") or {}
                from_user = msg.get("from") or {}
                uid = from_user.get("id")
                text = (msg.get("text") or "").strip()
                chat_id = (msg.get("chat") or {}).get("id")
                if not text or not uid or not chat_id:
                    continue
                if uid not in CONTROL_BOT_ALLOWED_IDS:
                    try:
                        await bot_api("sendMessage", {"chat_id": chat_id, "text": "Unauthorized."})
                    except Exception:
                        pass
                    continue
                cmd = text.split()[0].split("@")[0].lower()
                reply = None
                if cmd in ("/start", "/help"):
                    reply = "ERMI Control Bot\n/help /status /ping\nAgents: POST /telegram/deliver"
                elif cmd == "/ping":
                    reply = "pong"
                elif cmd == "/status":
                    try:
                        cl = await get_client()
                        me = await cl.get_me()
                        reply = f"ok mtproto=@{me.username} id={me.id} deliver_chat={DEFAULT_DELIVER_CHAT_ID}"
                    except Exception as e:
                        reply = f"status error: {type(e).__name__}"
                else:
                    reply = "Unknown. Allowed: /help /status /ping"
                if reply:
                    await bot_api("sendMessage", {"chat_id": chat_id, "text": reply})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("control bot error: %s", e)
            await asyncio.sleep(5)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
