"""Occurrence-level operations: list/patch occurrences, and the two
Discord-posting endpoints (create the Discord scheduled event for an
occurrence, and cancel it). This is where the "already posted" race
condition and the PostLog reservation pattern live — see the comment in
_post_to_one_tenant for why the PostLog row is inserted before the Discord
API call rather than after.

Kingdom-wide fan-out: an 'alliance'-scope event posts to exactly one
tenant (the owning one) same as always. A 'kingdom-wide' event's single
Occurrence fans out to every Tenant sharing its Kingdom — one independent
Discord Scheduled Event and one independent PostLog row per tenant, so one
alliance's Discord API hiccup doesn't block or corrupt the others (see
models.db.PostLog).
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import EventTenantNotification, Occurrence, PostLog, Tenant
from services.discord_api import (
    cancel_discord_event, create_discord_event, send_channel_message,
)
from services.time_utils import ensure_utc

from .deps import PLATFORM_BOT_TOKEN, find_post_log, get_current_tenant, get_occurrence_with_event
from .schemas import OccurrencePatch
from .serializers import _occurrence_dict

router = APIRouter()


@router.get("/api/occurrences")
async def list_occurrences(
    tenant: Tenant = Depends(get_current_tenant), db: AsyncSession = Depends(get_db)
):
    from sqlalchemy.orm import selectinload
    from models.db import EventDefinition
    result = await db.execute(
        select(Occurrence)
        .join(Occurrence.event)
        .join(Tenant, EventDefinition.owning_tenant_id == Tenant.id)
        .options(selectinload(Occurrence.event))
        .where(
            or_(
                Occurrence.tenant_id == tenant.id,
                (EventDefinition.scope == "kingdom-wide") & (Tenant.kingdom_id == tenant.kingdom_id),
            )
        )
        .order_by(Occurrence.occurrence_date)
    )
    occs = result.scalars().all()
    return [_occurrence_dict(occ, occ.event) for occ in occs]


@router.patch("/api/occurrences/{occ_id}")
async def update_occurrence(
    occ_id: int, payload: OccurrencePatch,
    occ_and_event: tuple = Depends(get_occurrence_with_event),
    db: AsyncSession = Depends(get_db),
):
    # get_occurrence_with_event already allows any tenant sharing this
    # occurrence's Kingdom for a kingdom-wide event — post_to_discord is a
    # single shared decision on the one Occurrence row for that case (should
    # every alliance post this together), not an independent per-tenant
    # flag; independent per-tenant outcomes are what PostLog tracks once
    # posting actually happens (see _post_to_one_tenant).
    occ, event = occ_and_event
    if payload.post_to_discord is not None: occ.post_to_discord = payload.post_to_discord
    if payload.post_status is not None:     occ.post_status     = payload.post_status
    await db.commit()
    return {"id": occ.id, "post_to_discord": occ.post_to_discord, "post_status": occ.post_status}


async def _resolve_notification(db, event, target_tenant: Tenant):
    """Which channel/role to ping for this target tenant, if any.

    For an alliance-scope event there's exactly one tenant and the bare
    columns on EventDefinition already are that tenant's own channel/role.
    For kingdom-wide, those bare columns belong to the owning tenant only —
    every other target needs its own override row (see
    models.db.EventTenantNotification), since a channel/role ID from one
    guild is meaningless (or wrong) in another. No override row for a
    target = no announcement/reminder for that target, not an error — the
    Discord Scheduled Event itself still gets created for them regardless.
    """
    if event.scope == "alliance" or target_tenant.id == event.owning_tenant_id:
        return event.notification_channel_id, event.notification_role_id
    result = await db.execute(
        select(EventTenantNotification).where(
            EventTenantNotification.event_id == event.id,
            EventTenantNotification.tenant_id == target_tenant.id,
        )
    )
    override = result.scalar_one_or_none()
    if not override:
        return "", ""
    return override.notification_channel_id, override.notification_role_id


async def _post_to_one_tenant(db: AsyncSession, occ: Occurrence, event, target_tenant: Tenant) -> dict:
    """Posts one Discord Scheduled Event, in target_tenant's own guild, for
    the given occurrence — and records one PostLog row for it. Used both
    for the single-tenant alliance case and once per tenant in the
    kingdom-wide fan-out. Never raises for a single target's failure; the
    caller decides how to aggregate results across targets."""
    if await find_post_log(db, target_tenant.id, event.name, occ.occurrence_date):
        return {"tenant_slug": target_tenant.slug, "status": "skipped", "detail": "Already posted"}

    token = target_tenant.bot_token or PLATFORM_BOT_TOKEN
    if not token:
        return {"tenant_slug": target_tenant.slug, "status": "error",
                "detail": "No Discord bot token configured for this tenant"}

    # Reserve the PostLog row now, before calling Discord, instead of after.
    # Two concurrent POSTs for the same occurrence could otherwise both pass
    # the check above, both create a Discord event, and only then collide on
    # uq_post_log — by which point the second request's Discord event has
    # already been created with no PostLog row to record it. Inserting the
    # reservation first means the race is caught by the unique constraint
    # before any Discord call happens.
    log = PostLog(
        tenant_id         = target_tenant.id,
        event_id          = event.id,
        event_name        = event.name,
        occurrence_date   = occ.occurrence_date,
        discord_guild_id  = target_tenant.guild_id,
        posted_by         = "coordinator",
        status            = "pending",
    )
    db.add(log)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        return {"tenant_slug": target_tenant.slug, "status": "skipped", "detail": "Already posted"}
    await db.commit()

    discord_id, error = await create_discord_event(
        token       = token,
        guild_id    = target_tenant.guild_id,
        name        = event.name,
        start       = occ.start_datetime_utc,
        end         = occ.end_datetime_utc,
        description = event.description,
        location    = event.discord_channel,
    )

    if error:
        log.status        = "error"
        log.status_detail = error
        await db.commit()
        return {"tenant_slug": target_tenant.slug, "status": "error", "detail": error}

    log.discord_event_id = discord_id
    log.posted_at_utc    = datetime.now(timezone.utc)
    log.status           = "posted"
    await db.commit()

    notify_channel, notify_role = await _resolve_notification(db, event, target_tenant)
    if notify_channel and notify_role:
        role_mention = f"<@&{notify_role}> "
        time_str     = occ.start_datetime_utc.strftime("%H:%M UTC")
        date_str     = occ.occurrence_date.strftime("%A %d %b")
        announce_msg = (
            f"{role_mention}📅 **{event.name}** has been scheduled\n"
            f"{date_str} · {time_str}"
            + (f" · {event.discord_channel}" if event.discord_channel else "")
            + (f"\nhttps://discord.com/events/{target_tenant.guild_id}/{discord_id}" if discord_id else "")
            + "\n\nClick **Interested** to get a reminder 30 minutes before."
        )
        await send_channel_message(token, notify_channel, announce_msg)

    return {"tenant_slug": target_tenant.slug, "status": "posted", "discord_event_id": discord_id}


@router.post("/api/occurrences/{occ_id}/post")
async def post_occurrence(
    db: AsyncSession = Depends(get_db),
    occ_and_event: tuple = Depends(get_occurrence_with_event),
):
    occ, event = occ_and_event

    now = datetime.now(timezone.utc)
    start = ensure_utc(occ.start_datetime_utc)
    if (start - now).total_seconds() < 900:
        raise HTTPException(status_code=400, detail="Event starts in less than 15 minutes")

    if event.scope == "alliance":
        owning = await db.get(Tenant, event.owning_tenant_id)
        result = await _post_to_one_tenant(db, occ, event, owning)
        if result["status"] == "skipped":
            raise HTTPException(status_code=409, detail="Already posted — see PostLog")
        if result["status"] == "error":
            occ.post_status   = "error"
            occ.status_detail = result["detail"]
            await db.commit()
            raise HTTPException(status_code=502, detail=result["detail"])
        occ.post_status = "posted"
        occ.status_detail = None
        await db.commit()
        return {"discord_event_id": result["discord_event_id"], "status": "posted"}

    # kingdom-wide: fan out to every tenant in the owning tenant's kingdom
    owning = await db.get(Tenant, event.owning_tenant_id)
    targets_result = await db.execute(select(Tenant).where(Tenant.kingdom_id == owning.kingdom_id))
    targets = targets_result.scalars().all()

    results = [await _post_to_one_tenant(db, occ, event, target) for target in targets]

    any_posted = any(r["status"] == "posted" for r in results)
    occ.post_status = "posted" if any_posted else "error"
    await db.commit()

    return {"status": occ.post_status, "targets": results}


@router.delete("/api/occurrences/{occ_id}/discord", status_code=200)
async def cancel_occurrence_discord(
    db: AsyncSession = Depends(get_db),
    occ_and_event: tuple = Depends(get_occurrence_with_event),
    tenant: Tenant = Depends(get_current_tenant),
):
    """Cancels only the *current tenant's own* copy of this occurrence's
    Discord post — for a kingdom-wide event, each tenant manages their own
    target independently, same as posting does. If NSR's copy has an issue,
    NSR's coordinator can cancel it without touching MOD's."""
    occ, event = occ_and_event

    log = await find_post_log(db, tenant.id, event.name, occ.occurrence_date)
    if not log or not log.discord_event_id:
        raise HTTPException(status_code=404, detail="No Discord event ID found in PostLog")

    token = tenant.bot_token or PLATFORM_BOT_TOKEN
    if not token:
        raise HTTPException(status_code=400, detail="No Discord bot token configured for this tenant")

    success, error = await cancel_discord_event(token, tenant.guild_id, log.discord_event_id)

    # discord_event_id keeps the real Discord ID rather than being
    # overwritten with a "CANCELLED — was {id}" marker string — status
    # already carries the cancelled state, and mangling the ID field broke
    # any later webhook lookup by that same ID (see webhooks.py).
    log.status = "cancelled"
    occ.post_status = "cancelled"
    await db.commit()

    if not success and "404" not in error:
        raise HTTPException(status_code=502, detail=error)
    return {"status": "cancelled", "discord_event_id": log.discord_event_id}
