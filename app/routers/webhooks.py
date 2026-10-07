import logging
import os

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db, get_session_factory
from services.discord_client import get_discord
from services.discord_commands import handle_interaction_ex


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
async def discord_webhook(
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    discord=Depends(get_discord),
    session_factory=Depends(get_session_factory),
):
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

    # Discord's Interactions Endpoint only ever delivers Interaction objects
    # (PING, commands, autocomplete, modals). Guild scheduled event updates are
    # Gateway events and never arrive here. Spec §70 handles the rest.
    response, followup = await handle_interaction_ex(db, data, discord=discord, session_factory=session_factory)
    if followup is not None:  # spec §87: Discord has its answer before the role call starts
        background.add_task(followup)
    return JSONResponse(response)
