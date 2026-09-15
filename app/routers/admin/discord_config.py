"""Per-tenant Discord metadata: listing the current tenant's own guild's
channels/roles for the event-creation form's dropdowns. Setting a tenant's
bot_token/guild_id itself now happens via tenants.py (PATCH /api/tenants/{id}),
not here — this file used to own the old DiscordConfig singleton, which
tenants.py has superseded.
"""
from fastapi import APIRouter, Depends, HTTPException

from services.discord_api import get_guild_channels, get_guild_roles
from services.auth import require_admin_key

from .deps import DiscordCreds, get_discord_config

router = APIRouter(dependencies=[Depends(require_admin_key)])


@router.get("/api/discord/channels")
async def list_discord_channels(cfg: DiscordCreds = Depends(get_discord_config)):
    channels, error = await get_guild_channels(cfg.bot_token, cfg.guild_id)
    if error:
        raise HTTPException(status_code=502, detail=error)
    return channels


@router.get("/api/discord/roles")
async def list_discord_roles(cfg: DiscordCreds = Depends(get_discord_config)):
    roles, error = await get_guild_roles(cfg.bot_token, cfg.guild_id)
    if error:
        raise HTTPException(status_code=502, detail=error)
    return roles
