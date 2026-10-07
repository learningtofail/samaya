"""Schedule insights (spec §85): read-only views over data Samaya already stores.

Scope follows `get_current_tenants`, so `X-Tenant-Slug: *` means the alliances the
caller can reach. Nothing here writes, nothing is public, and nothing is per
player: the coverage panel returns counts per alliance only.
"""
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import (
    AudienceDestination, Delivery, Event, EventAlliance, EventOccurrence, RedemptionResult, RedemptionRun, Tenant,
)
from services import analytics
from services.event_engine import effective_end, effective_start
from services.giftcode_engine import outcome_group
from services.time_utils import ensure_utc

from .deps import get_current_tenants

router = APIRouter(prefix="/api/analytics")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _day_start(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


async def _blocks(db: AsyncSession, tenants: list[Tenant], start: datetime, end: datetime) -> list[analytics.Block]:
    """Active, not cancelled occurrences starting in [start, end) that are visible from `tenants`
    (the same visibility as the Schedule tab), as time blocks."""
    tenant_ids = [t.id for t in tenants]
    kingdom_ids = {t.kingdom_id for t in tenants}
    in_audience = select(EventAlliance.event_id).where(EventAlliance.tenant_id.in_(tenant_ids))
    pairs = (await db.execute(
        select(EventOccurrence, Event)
        .join(Event, Event.id == EventOccurrence.event_id)
        .join(Tenant, Tenant.id == Event.owning_tenant_id)
        .where(
            EventOccurrence.occurrence_date >= (start - timedelta(days=1)).date(),
            EventOccurrence.occurrence_date <= (end + timedelta(days=1)).date(),
            EventOccurrence.status != "cancelled", Event.active.is_(True),
            or_(
                Event.owning_tenant_id.in_(tenant_ids), Event.id.in_(in_audience),
                (Event.scope == "kingdom-wide") & (Tenant.kingdom_id.in_(kingdom_ids)),
            ),
        )
    )).unique().all()
    pairs = [(o, e) for o, e in pairs if start <= effective_start(o) < end]
    if not pairs:
        return []
    names = {t.id: t.name for t in (await db.execute(select(Tenant))).scalars().unique().all()}
    audience: dict[int, set[int]] = {}
    for row in (await db.execute(select(EventAlliance).where(EventAlliance.event_id.in_({e.id for _, e in pairs})))).scalars():
        audience.setdefault(row.event_id, set()).add(row.tenant_id)
    channels: dict[int, set[str]] = {}
    for occ_id, channel in (await db.execute(
        select(Delivery.occurrence_id, Delivery.channel_id)
        .where(Delivery.occurrence_id.in_({o.id for o, _ in pairs}), Delivery.kind == "reminder", Delivery.channel_id != "")
    )).all():
        channels.setdefault(occ_id, set()).add(channel)
    blocks = []
    for occ, event in pairs:
        ids = audience.get(event.id, set()) | {event.owning_tenant_id}
        begin = effective_start(occ)
        blocks.append(analytics.Block(
            occurrence_id=occ.id, event_id=event.id, name=event.name, start=begin,
            end=analytics.block_end(begin, effective_end(occ)), tenant_ids=frozenset(ids),
            alliances=tuple(sorted(names[i] for i in ids if i in names)), kingdom_wide=event.scope == "kingdom-wide",
            channels=frozenset(channels.get(occ.id, set())),
        ))
    return blocks


@router.get("/schedule-heatmap")
async def schedule_heatmap(
    days: int = Query(default=28, ge=7, le=60),
    tenants: list[Tenant] = Depends(get_current_tenants),
    db: AsyncSession = Depends(get_db),
):
    start = _day_start(_now())
    result = analytics.heatmap(await _blocks(db, tenants, start, start + timedelta(days=days)))
    return {"days": days, "from": start.date().isoformat(), **result}


@router.get("/schedule-overlaps")
async def schedule_overlaps(
    days: int = Query(default=28, ge=7, le=60),
    tenants: list[Tenant] = Depends(get_current_tenants),
    db: AsyncSession = Depends(get_db),
):
    start = _day_start(_now())
    found = analytics.overlaps(await _blocks(db, tenants, start, start + timedelta(days=days)))
    return {"days": days, "count": len(found), "overlaps": found}


@router.get("/free-slots")
async def free_slots(
    duration: int = Query(default=60, ge=15, le=480),
    days: int = Query(default=14, ge=1, le=30),
    from_hour: int = Query(default=0, ge=0, le=23),
    to_hour: int = Query(default=24, ge=1, le=24),
    alliances: str = Query(default="", max_length=500),
    tenants: list[Tenant] = Depends(get_current_tenants),
    db: AsyncSession = Depends(get_db),
):
    if to_hour <= from_hour:
        raise HTTPException(status_code=422, detail="to_hour must be later than from_hour")
    if (to_hour - from_hour) * 60 < duration:
        raise HTTPException(status_code=422, detail="duration does not fit inside the allowed hours")
    selected: set[int] | None = None
    slugs = [s for s in (x.strip() for x in alliances.split(",")) if s]
    if slugs:
        by_slug = {t.slug: t for t in tenants}
        unknown = [s for s in slugs if s not in by_slug]
        if unknown:
            raise HTTPException(status_code=422, detail=f"Unknown alliance: {unknown[0]}")
        selected = {by_slug[s].id for s in slugs}
    now = _now()
    blocks = await _blocks(db, tenants, now - timedelta(days=1), now + timedelta(days=days + 1))
    slots = analytics.free_slots(blocks, now, duration, days, from_hour, to_hour, selected)
    return {"duration": duration, "days": days, "from_hour": from_hour, "to_hour": to_hour, "slots": slots}


@router.get("/delivery-trends")
async def delivery_trends(
    days: int = Query(default=30, ge=1, le=60),
    by: Literal["day", "destination"] = "day",
    tenants: list[Tenant] = Depends(get_current_tenants),
    db: AsyncSession = Depends(get_db),
):
    now = _now()
    first = _day_start(now - timedelta(days=days - 1))
    found = (await db.execute(
        select(Delivery, AudienceDestination)
        .outerjoin(AudienceDestination, AudienceDestination.id == Delivery.destination_id)
        .where(
            Delivery.tenant_id.in_([t.id for t in tenants]), Delivery.kind == "reminder",
            Delivery.merged_into_id.is_(None), Delivery.due_at_utc >= first, Delivery.due_at_utc <= now,
        )
    )).unique().all()
    rows = []
    for delivery, dest in found:
        posted = ensure_utc(delivery.posted_at_utc) if delivery.posted_at_utc else None
        due = ensure_utc(delivery.due_at_utc)
        label = (f"{dest.audience.label}, channel {delivery.channel_id}" if dest is not None
                 else (f"channel {delivery.channel_id}" if delivery.channel_id else "no destination"))
        rows.append({
            "due": due, "status": delivery.status, "destination": label,
            "lateness_seconds": max(0, int((posted - due).total_seconds())) if posted else None,
        })
    return {"window_days": days, "by": by, **analytics.delivery_trends(rows, now, days, by)}


@router.get("/redemption-coverage")
async def redemption_coverage(
    runs: int = Query(default=5, ge=1, le=10),
    tenants: list[Tenant] = Depends(get_current_tenants),
    db: AsyncSession = Depends(get_db),
):
    tenant_ids = {t.id: t.name for t in tenants}
    run_rows = (await db.execute(
        select(RedemptionRun).where(RedemptionRun.kingdom_id.in_({t.kingdom_id for t in tenants}))
        .order_by(RedemptionRun.created_at.desc(), RedemptionRun.id.desc()).limit(runs)
    )).scalars().all()
    if not run_rows:
        return {"runs": []}
    results = (await db.execute(
        select(RedemptionResult.run_id, RedemptionResult.tenant_id, RedemptionResult.status)
        .where(RedemptionResult.run_id.in_([r.id for r in run_rows]), RedemptionResult.tenant_id.in_(tenant_ids))
    )).all()
    coverage = analytics.redemption_coverage(
        [{"id": r.id, "code": r.code, "created_at": ensure_utc(r.created_at).isoformat()} for r in run_rows],
        [{"run_id": run_id, "tenant": tenant_ids[tenant_id], "group": outcome_group(status)} for run_id, tenant_id, status in results],
    )
    return {"runs": coverage}
