# Telegram Research MCP

MTProto (Telethon) REST API for agents.

## Endpoints
- GET /health
- GET /telegram?target=@channel&limit=50
- GET /telegram/search?target=@channel&q=keyword
- GET /telegram/message?target=@channel&message_id=123
- GET /telegram/media?target=@channel&message_id=123
- GET /telegram/updates?target=@channel&cursor=123

Env: TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION, ALLOW_NO_AUTH=1
