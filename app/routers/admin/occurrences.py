"""Occurrence-level operations: list/patch occurrences, and the two
Discord-posting endpoints (create the Discord scheduled event for an
occurrence, and cancel it).

The actual posting logic — the "already posted" race condition, the
PostLog reservation pattern, kingdom-wide fan-out, and shared-DiscordServer
dedup (spec §52) — lives in services/discord_posting.py, not here (audit
remediation, Phase 2): the daily auto-post job (scheduler/auto_post.py)
and the public events page (routers/events.py) both need the exact same
logic, and a router is the wrong place for something two non-router
callers depend on. See that module's own docstring for the full picture;
this file just wires it into the two HTTP endpoints below.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import EventDefinition, Occurrence, Tenant
from services.discord_api import cancel_discord_event
from services.discord_posting import (
    PLATFORM_BOT_TOKEN, _post_to_one_tenant, _resolve_post_targets, find_post_log,
)
from services.time_utils import ensure_utc

from .deps import get_current_tenant, get_current_tenants, get_occurrence_with_event, require_not_viewer
from .schemas import OccurrencePatch
from .serializers import _occurrence_dict

router = APIRouter()


@router.get("/api/occurrences")
async def list_occurrences(
    tenants: list[Tenant] = Depends(get_current_tenants), db: AsyncSession = Depends(get_db)
):
    """See admin/events.py::list_events for the combined-mode reasoning —
    same union-plus-dedupe approach, applied to occurrences instead of
    event definitions.
    """
    from sqlalchemy.orm import selectinload
    tenant_ids = [t.id for t in tenants]
    kingdom_ids = {t.kingdom_id for t in tenants}
    result = await db.execute(
        select(Occurrence)
        .join(Occurrence.event)
        .join(Tenant, EventDefinition.owning_tenant_id == Tenant.id)
        .options(selectinload(Occurrence.event))
        .where(
            or_(
                Occurrence.tenant_id.in_(tenant_ids),
                (EventDefinition.scope == "kingdom-wide") & (Tenant.kingdom_id.in_(kingdom_ids)),
            )
        )
        .order_by(Occurrence.occurrence_date)
    )
    seen = {}
    for occ in result.scalars().all():
        seen[occ.id] = occ
    return [_occurrence_dict(occ, occ.event) for occ in seen.values()]


@router.patch("/api/occurrences/{occ_id}")
async def update_occurrence(
    occ_id: int, payload: OccurrencePatch,
    occ_and_event: tuple = Depends(get_occurrence_with_event),
    db: AsyncSession = Depends(get_db),
    _: Tenant = Depends(require_not_viewer),
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


@router.post("/api/occurrences/{occ_id}/post")
async def post_occurrence(
    db: AsyncSession = Depends(get_db),
    occ_and_event: tuple = Depends(get_occurrence_with_event),
    _: Tenant = Depends(require_not_viewer),
):
    occ, event = occ_and_event

    now = datetime.now(timezone.utc)
    start = ensure_utc(occ.start_datetime_utc)
    if (start - now).total_seconds() < 900:
        raise HTTPException(status_code=400, detail="Event starts in less than 15 minutes")

    owning = await db.get(Tenant, event.owning_tenant_id)
    all_targets = await _resolve_post_targets(db, event, owning)

    if len(all_targets) == 1:
        # Preserves the original single-target response shape/semantics
        # (a bare 409/502 rather than the multi-target {"targets": [...]}
        # shape) for the common case: an alliance-scope event with no
        # explicit targets attached.
        result = await _post_to_one_tenant(db, occ, event, all_targets[0])
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

    results = [await _post_to_one_tenant(db, occ, event, target) for target in all_targets]

    any_posted = any(r["status"] == "posted" for r in results)
    occ.post_status = "posted" if any_posted else "error"
    await db.commit()

    return {"status": occ.post_status, "targets": results}


@router.delete("/api/occurrences/{occ_id}/discord", status_code=200)
async def cancel_occurrence_discord(
    db: AsyncSession = Depends(get_db),
    occ_and_event: tuple = Depends(get_occurrence_with_event),
    tenant: Tenant = Depends(require_not_viewer),
):
    """Cancels only the *current tenant's own* copy of this occurrence's
    Discord post — for a kingdom-wide event, each tenant manages their own
    target independently, same as posting does. If NSR's copy has an issue,
    NSR's coordinator can cancel it without touching MOD's."""
    occ, event = occ_and_event

    log = await find_post_log(db, tenant.id, event.name, occ.occurrence_date)
    if not log or not log.discord_event_id:
        raise HTTPException(status_code=404, detail="No Discord event ID found in PostLog")

    token = tenant.server.bot_token or PLATFORM_BOT_TOKEN
    if not token:
        raise HTTPException(status_code=400, detail="No Discord bot token configured for this tenant")

    success, error = await cancel_discord_event(token, tenant.server.guild_id, log.discord_event_id)

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
