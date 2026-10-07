"""Time polls (spec §84): create, list, close and cancel. Players vote with buttons
in Discord (services/time_poll.py); nothing here ever applies a result. Never public."""
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models import get_db
from models.db import (
    AllianceAudience, Audience, Event, EventAlliance, EventOccurrence, Tenant, TimePoll, TimePollSlot, User,
)
from services.audit import log_change
from services.discord_client import get_discord
from services.time_poll import (
    DEFAULT_CLOSE_HOURS, MAX_CLOSE_HOURS, MAX_OPEN_PER_ALLIANCE, MAX_SLOTS, MIN_SLOTS, cancel_poll, close_poll,
    leading_slots, post_poll, slot_views,
)
from services.time_utils import ensure_utc

from .deps import check_kingdom_coordinator, get_current_tenants, get_current_user, require_not_viewer

router = APIRouter(prefix="/api/time-polls")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PollCreate(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    slots: list[datetime] = Field(min_length=MIN_SLOTS, max_length=MAX_SLOTS)
    audience_ids: list[int] = Field(min_length=1, max_length=20)
    closes_in_hours: int = Field(default=DEFAULT_CLOSE_HOURS, ge=1, le=MAX_CLOSE_HOURS)
    occurrence_id: int | None = None
    scope: Literal["alliance", "kingdom-wide"] = "alliance"

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A poll needs a title")
        return value

    @field_validator("slots")
    @classmethod
    def _slots(cls, value: list[datetime]) -> list[datetime]:
        normalised = [(v if v.tzinfo else v.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).replace(second=0, microsecond=0)
                      for v in value]
        if len(set(normalised)) != len(normalised):
            raise ValueError("Each slot must be a different time")
        return normalised


class CloseBody(BaseModel):
    winner_slot_id: int | None = None


async def _poll_json(db: AsyncSession, poll: TimePoll, tenants_by_id: dict[int, Tenant] | None = None) -> dict:
    views = await slot_views(db, poll)
    leaders = {s.id for s in leading_slots(views)}
    tenant = (tenants_by_id or {}).get(poll.tenant_id) if poll.tenant_id else None
    if poll.tenant_id and tenant is None:
        tenant = await db.get(Tenant, poll.tenant_id)
    return {
        "id": poll.id, "title": poll.title, "status": poll.status,
        "scope": "alliance" if poll.tenant_id else "kingdom-wide", "alliance": tenant.slug if tenant else None,
        "occurrence_id": poll.occurrence_id,
        "closes_at": ensure_utc(poll.closes_at).isoformat(),
        "closed_at": ensure_utc(poll.closed_at).isoformat() if poll.closed_at else None,
        "winner_slot_id": poll.winner_slot_id, "created_at": ensure_utc(poll.created_at).isoformat() if poll.created_at else None,
        "total_votes": sum(v.votes for v in views),
        "slots": [{"id": v.id, "starts_at": v.starts_at.isoformat(), "votes": v.votes, "leading": v.id in leaders} for v in views],
        "messages": [{"channel_id": m.channel_id, "error": m.error,
                      "refreshed_at": ensure_utc(m.refreshed_at).isoformat() if m.refreshed_at else None} for m in poll.messages],
    }


def _factory(db: AsyncSession) -> async_sessionmaker:
    return async_sessionmaker(db.bind, class_=AsyncSession, expire_on_commit=False)


async def _visible(db: AsyncSession, tenants: list[Tenant]):
    return or_(
        TimePoll.tenant_id.in_([t.id for t in tenants]),
        (TimePoll.tenant_id.is_(None)) & (TimePoll.kingdom_id.in_({t.kingdom_id for t in tenants})),
    )


async def _load_visible(db: AsyncSession, poll_id: int, tenants: list[Tenant]) -> TimePoll:
    poll = (await db.execute(select(TimePoll).where(TimePoll.id == poll_id, await _visible(db, tenants)))).scalars().first()
    if poll is None:
        raise HTTPException(status_code=404, detail="Poll not found")
    return poll


async def _require_poll_write(db: AsyncSession, user: User, tenant: Tenant, poll: TimePoll) -> None:
    if poll.tenant_id is None:
        if poll.kingdom_id != tenant.kingdom_id:
            raise HTTPException(status_code=404, detail="Poll not found")
        await check_kingdom_coordinator(db, user, poll.kingdom_id, "Managing a kingdom-wide poll requires kingdom coordinator access")
    elif poll.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Poll not found")


@router.post("", status_code=201)
async def create_poll(
    body: PollCreate,
    tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    discord=Depends(get_discord),
):
    now = _now()
    if any(s <= now for s in body.slots):
        raise HTTPException(status_code=422, detail="Every slot must be in the future")
    kingdom_wide = body.scope == "kingdom-wide"
    if kingdom_wide:
        await check_kingdom_coordinator(db, user, tenant.kingdom_id, "Creating a kingdom-wide poll requires kingdom coordinator access")
    open_filter = [TimePoll.status == "open", TimePoll.kingdom_id == tenant.kingdom_id]
    open_filter.append(TimePoll.tenant_id.is_(None) if kingdom_wide else TimePoll.tenant_id == tenant.id)
    if (await db.execute(select(func.count()).select_from(TimePoll).where(*open_filter))).scalar_one() >= MAX_OPEN_PER_ALLIANCE:
        raise HTTPException(status_code=409, detail=f"At most {MAX_OPEN_PER_ALLIANCE} polls can be open at once. Close or cancel one first.")

    ids = sorted(set(body.audience_ids))
    audiences = list((await db.execute(select(Audience).where(Audience.id.in_(ids), Audience.kingdom_id == tenant.kingdom_id))).scalars().unique())
    if len(audiences) != len(ids):
        raise HTTPException(status_code=422, detail="Unknown audience")
    if not kingdom_wide:
        linked = set((await db.execute(select(AllianceAudience.audience_id).where(
            AllianceAudience.tenant_id == tenant.id, AllianceAudience.audience_id.in_(ids)))).scalars())
        if linked != set(ids):
            raise HTTPException(status_code=422, detail="That audience is not linked to this alliance")

    if body.occurrence_id is not None:
        row = (await db.execute(
            select(Event, Tenant).join(EventOccurrence, EventOccurrence.event_id == Event.id)
            .join(Tenant, Tenant.id == Event.owning_tenant_id).where(EventOccurrence.id == body.occurrence_id)
        )).first()
        if row is None or row[1].kingdom_id != tenant.kingdom_id:
            raise HTTPException(status_code=422, detail="Unknown occurrence")
        if not kingdom_wide:
            event = row[0]
            in_audience = (await db.execute(select(EventAlliance).where(
                EventAlliance.event_id == event.id, EventAlliance.tenant_id == tenant.id))).first() is not None
            if event.owning_tenant_id != tenant.id and not in_audience:
                raise HTTPException(status_code=422, detail="Unknown occurrence")

    targets: list[tuple[int | None, str, str]] = []
    seen: set[tuple[str, str]] = set()
    skipped: list[str] = []
    for audience in audiences:
        for dest in audience.destinations:
            key = (dest.server.guild_id, dest.channel_id)
            if key in seen:
                continue
            seen.add(key)
            if dest.paused_at is not None:
                skipped.append(f"Channel {dest.channel_id}: paused after repeated Discord errors")
                continue
            targets.append((dest.id, dest.server.guild_id, dest.channel_id))
    if not targets and not skipped:
        raise HTTPException(status_code=422, detail="Those audiences have no destinations")

    poll = TimePoll(
        kingdom_id=tenant.kingdom_id, tenant_id=None if kingdom_wide else tenant.id, title=body.title,
        occurrence_id=body.occurrence_id, status="open", closes_at=now + timedelta(hours=body.closes_in_hours), created_by=user.id,
    )
    poll.slots = [TimePollSlot(starts_at=s, position=i) for i, s in enumerate(sorted(body.slots))]
    db.add(poll)
    await db.flush()
    await log_change(db, user_id=user.id, tenant_id=tenant.id, table_name="time_polls", row_id=poll.id, action="create",
                     after={"title": poll.title, "slots": [s.isoformat() for s in sorted(body.slots)], "audiences": ids, "scope": body.scope})
    await db.commit()
    poll_id = poll.id

    errors = skipped + await post_poll(_factory(db), discord, poll_id, targets, now)
    db.expire_all()
    result = await _poll_json(db, await db.get(TimePoll, poll_id))
    result["discord_errors"] = errors
    return result


@router.get("")
async def list_polls(
    status: Literal["open", "closed", "cancelled"] | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    tenants: list[Tenant] = Depends(get_current_tenants),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(TimePoll).where(await _visible(db, tenants))
    if status:
        stmt = stmt.where(TimePoll.status == status)
    polls = (await db.execute(stmt.order_by(TimePoll.id.desc()).limit(limit))).scalars().unique().all()
    by_id = {t.id: t for t in tenants}
    return [await _poll_json(db, p, by_id) for p in polls]


@router.get("/{poll_id}")
async def get_poll(poll_id: int, tenants: list[Tenant] = Depends(get_current_tenants), db: AsyncSession = Depends(get_db)):
    return await _poll_json(db, await _load_visible(db, poll_id, tenants), {t.id: t for t in tenants})


@router.post("/{poll_id}/close")
async def close(
    poll_id: int, body: CloseBody | None = None,
    tenant: Tenant = Depends(require_not_viewer), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
    discord=Depends(get_discord),
):
    poll = await db.get(TimePoll, poll_id)
    if poll is None:
        raise HTTPException(status_code=404, detail="Poll not found")
    await _require_poll_write(db, user, tenant, poll)
    winner = body.winner_slot_id if body else None
    if winner is not None and not any(s.id == winner for s in poll.slots):
        raise HTTPException(status_code=422, detail="That slot does not belong to this poll")
    if poll.status != "open":
        raise HTTPException(status_code=409, detail="This poll is not open")
    await log_change(db, user_id=user.id, tenant_id=tenant.id, table_name="time_polls", row_id=poll.id, action="update",
                     before={"status": "open"}, after={"status": "closed", "winner_slot_id": winner})
    await db.commit()
    await close_poll(_factory(db), discord, poll_id, _now(), winner)
    db.expire_all()
    return await _poll_json(db, await db.get(TimePoll, poll_id))


@router.post("/{poll_id}/cancel")
async def cancel(
    poll_id: int,
    tenant: Tenant = Depends(require_not_viewer), user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
    discord=Depends(get_discord),
):
    poll = await db.get(TimePoll, poll_id)
    if poll is None:
        raise HTTPException(status_code=404, detail="Poll not found")
    await _require_poll_write(db, user, tenant, poll)
    if poll.status != "open":
        raise HTTPException(status_code=409, detail="This poll is not open")
    await log_change(db, user_id=user.id, tenant_id=tenant.id, table_name="time_polls", row_id=poll.id, action="update",
                     before={"status": "open"}, after={"status": "cancelled"})
    await db.commit()
    await cancel_poll(_factory(db), discord, poll_id, _now())
    db.expire_all()
    return await _poll_json(db, await db.get(TimePoll, poll_id))
