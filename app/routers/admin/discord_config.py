"""Per-tenant Discord metadata: listing the current tenant's own guild's
channels/roles for the event-creation form's dropdowns. Setting a tenant's
bot_token/guild_id itself now happens via tenants.py (PATCH /api/tenants/{id}),
not here — this file used to own the old DiscordConfig singleton, which
tenants.py has superseded.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import Tenant
from services.discord_api import get_guild_channels, get_guild_info, get_guild_roles

from .deps import DiscordCreds, PLATFORM_BOT_TOKEN, get_current_tenants, get_discord_config

router = APIRouter()


@router.get("/api/discord/guild")
async def get_current_guild_info(cfg: DiscordCreds = Depends(get_discord_config)):
    """The current tenant's Discord server's own display name — shown
    next to Channel Name in the Config tab so it's clear which physical
    Discord server (not which alliance) a channel belongs to; several
    tenants can point at the same guild_id (see MOD/NSR)."""
    info, error = await get_guild_info(cfg.bot_token, cfg.guild_id)
    if error:
        raise HTTPException(status_code=502, detail=error)
    return info


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


@router.get("/api/discord/config-overview")
async def discord_config_overview(
    tenants: list[Tenant] = Depends(get_current_tenants), db: AsyncSession = Depends(get_db)
):
    """Spec §38.5 — backs the consolidated Discord Config tab's
    accordion-by-server layout. Grouped by DiscordServer rather than by
    Tenant because `Tenant.server_id` has no unique constraint: more than
    one alliance can already share one Discord server, and a flat
    per-tenant list would show that server's identical channel/role list
    twice under two different alliance names. Each accessible tenant is
    only ever listed under servers it actually uses, and — deliberately —
    only the tenants *this caller* can see are named per server (never the
    server's full backing list), so a coordinator with access to just one
    of two tenants sharing a server never learns the other tenant's slug
    through this endpoint."""
    by_server: dict[int, dict] = {}
    for t in tenants:
        entry = by_server.setdefault(t.server_id, {
            "server_id":   t.server_id,
            "server_name": t.server.name,
            "guild_id":    t.server.guild_id,
            "tenants":     [],
        })
        entry["tenants"].append({"slug": t.slug, "name": t.name, "color": t.color})

    results = []
    for entry in by_server.values():
        token = None
        # Any tenant in this group carries the same server's bot_token —
        # re-derive it the same way get_discord_config does rather than
        # re-fetching the Tenant, since we already have it in `tenants`.
        server_tenant = next(t for t in tenants if t.server_id == entry["server_id"])
        token = server_tenant.server.bot_token or PLATFORM_BOT_TOKEN

        if not token:
            results.append({**entry, "error": "No Discord bot token configured for this server"})
            continue

        guild_info, guild_error = await get_guild_info(token, entry["guild_id"])
        channels, channels_error = await get_guild_channels(token, entry["guild_id"])
        roles, roles_error = await get_guild_roles(token, entry["guild_id"])
        error = guild_error or channels_error or roles_error

        results.append({
            **entry,
            "guild_name": guild_info.get("name") if guild_info else None,
            "channels":   channels or [],
            "roles":      roles or [],
            "error":      error,
        })

    results.sort(key=lambda r: r["server_name"])
    return {"servers": results}
