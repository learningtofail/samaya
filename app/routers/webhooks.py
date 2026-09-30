import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from models import AsyncSessionLocal
from models.db import PostLog

logger = logging.getLogger(__name__)
router = APIRouter()

# Falls back to this when no Tenant has its own public_key set — the
# common case, since most tenants share the platform bot (see
# routers/admin/deps.py.PLATFORM_BOT_TOKEN). A tenant with its own custom
# bot AND its own Discord "Interactions Endpoint URL" pointed at this same
# route would need signature verification against its own public_key
# instead — not implemented here: this endpoint tries the platform key
# only, so that narrow combination will 401. Worth revisiting if a tenant
# actually sets up their own bot's interactions endpoint.
PLATFORM_PUBLIC_KEY = os.environ.get("PLATFORM_PUBLIC_KEY", "")


def verify_signature(public_key: str, signature: str, timestamp: str, body: bytes) -> bool:
    try:
        from nacl.signing import VerifyKey
        vk = VerifyKey(bytes.fromhex(public_key))
        vk.verify(timestamp.encode() + body, bytes.fromhex(signature))
        return True
    except Exception:
        return False


@router.post("/discord")
async def discord_webhook(request: Request):
    body      = await request.body()
    signature = request.headers.get("X-Signature-Ed25519", "")
    timestamp = request.headers.get("X-Signature-Timestamp", "")

    if not PLATFORM_PUBLIC_KEY:
        # No config yet — only allow PING for initial Discord verification
        try:
            data = await request.json()
            if data.get("type") == 1:
                return JSONResponse({"type": 1})
        except Exception:
            pass
        raise HTTPException(status_code=401, detail="Discord not configured")

    # Validate signature
    if not verify_signature(PLATFORM_PUBLIC_KEY, signature, timestamp, body):
        raise HTTPException(status_code=401, detail="Invalid request signature")

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = data.get("type")

    # Type 1 — PING (Discord endpoint verification)
    if event_type == 1:
        return JSONResponse({"type": 1})

    # Type — GUILD_SCHEDULED_EVENT_UPDATE / GUILD_SCHEDULED_EVENT_DELETE
    #
    # KNOWN DEAD CODE, kept only as a no-op safety net rather than deleted
    # outright (see spec §45): these two branches, and the handlers below,
    # can never actually run in production. Discord's "Interactions
    # Endpoint URL" — what this route, its signature verification, and its
    # PING handling above all exist for — only ever delivers Interaction
    # objects (PING, application commands, message components, modal
    # submits). GUILD_SCHEDULED_EVENT_UPDATE/_DELETE are Gateway dispatch
    # events, which Discord only ever sends over a bot's persistent Gateway
    # (WebSocket) connection — never as an HTTP POST to this or any other
    # webhook URL. This app has no Gateway client (it's a stateless FastAPI
    # service, not a connected bot process), so `data.get("t")` here will
    # never actually equal either string; `handle_event_update`/
    # `handle_event_delete` are unreachable. The practical consequence —
    # PostLog never learns a Discord event finished or was deleted except
    # through Samaya's own actions — is handled instead by
    # routers/admin/discord_sync.py's read-only drift check, which derives
    # "this occurrence has already ended" from Samaya's own schedule data
    # rather than waiting on a push notification that was never coming.
    if data.get("t") == "GUILD_SCHEDULED_EVENT_UPDATE":
        await handle_event_update(data.get("d", {}))

    if data.get("t") == "GUILD_SCHEDULED_EVENT_DELETE":
        await handle_event_delete(data.get("d", {}))

    return JSONResponse({"status": "ok"})


async def handle_event_update(event_data: dict):
    discord_id = str(event_data.get("id", ""))
    status     = event_data.get("status")  # 1=SCHEDULED 2=ACTIVE 3=COMPLETED 4=CANCELLED

    if not discord_id or status is None:
        return

    status_map = {1: "posted", 2: "active", 3: "completed", 4: "cancelled"}
    new_status = status_map.get(status)
    if not new_status:
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(PostLog).where(PostLog.discord_event_id == discord_id)
        )
        log = result.scalar_one_or_none()
        if log:
            log.status = new_status
            log.status_detail = f"Discord status update: {new_status} at {datetime.now(timezone.utc).isoformat()}"
            await session.commit()
            logger.info(f"PostLog updated for Discord ID {discord_id}: {new_status}")


async def handle_event_delete(event_data: dict):
    discord_id = str(event_data.get("id", ""))
    if not discord_id:
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(PostLog).where(PostLog.discord_event_id == discord_id)
        )
        log = result.scalar_one_or_none()
        if log:
            log.status        = "cancelled"
            log.status_detail = f"Deleted via Discord at {datetime.now(timezone.utc).isoformat()}"
            await session.commit()
            logger.info(f"PostLog marked cancelled for Discord ID {discord_id}")
