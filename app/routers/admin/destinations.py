"""Destinations and audience groups (spec §67).

A destination is where one alliance's messages can go: a server, a channel
and an optional role. Alliance owners manage their own. An audience group is
a named set of destinations at Kingdom level, managed by kingdom coordinators.
Every change regenerates the pending deliveries of the Kingdom's events, so
the schedule follows the new setup.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import (
    AudienceGroup, AudienceGroupDestination, Destination, Event, EventDestination, EventGroup, Tenant, User,
)
from services.audit import log_change
from services.db_errors import raise_friendly_integrity_error
from services.event_engine import resync_kingdom_events

from .deps import (
    check_kingdom_coordinator, get_current_tenant, get_current_tenants, get_current_user, require_not_viewer,
    require_tenant_owner,
)

router = APIRouter(prefix="/api")

_DESTINATION_TAKEN = {
    "uq_destination_label": "This alliance already has a destination with that label",
    "uq_destination_target": "This alliance already has a destination for that server, channel and role",
    "destinations_tenant_id_key": "This alliance already has a destination with that label or target",
}
_GROUP_TAKEN = {
    "uq_audience_group_name": "An audience group with that name already exists",
    "audience_groups_kingdom_id_key": "An audience group with that name already exists",
}


def _digits(v: str | None) -> str | None:
    v = (v or "").strip() if v is not None else v
    if v and not v.isdigit():
        raise ValueError("A Discord ID is digits only")
    return v


class DestinationIn(BaseModel):
    label:           str = Field(min_length=1, max_length=60)
    server_id:       int
    channel_id:      str = Field(min_length=1, max_length=32)
    role_id:         str = Field(default="", max_length=32)
    post_by_default: bool = True
    leadership_only: bool = False

    _ids = field_validator("channel_id", "role_id")(lambda cls, v: _digits(v))

    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Label cannot be blank")
        return v


class DestinationPatch(BaseModel):
    label:           str | None = Field(default=None, min_length=1, max_length=60)
    server_id:       int | None = None
    channel_id:      str | None = Field(default=None, min_length=1, max_length=32)
    role_id:         str | None = Field(default=None, max_length=32)
    post_by_default: bool | None = None
    leadership_only: bool | None = None

    _ids = field_validator("channel_id", "role_id")(lambda cls, v: _digits(v))


class GroupIn(BaseModel):
    name:           str = Field(min_length=1, max_length=80)
    description:    str = Field(default="", max_length=300)
    destination_ids: list[int] = []

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Name cannot be blank")
        return v


class GroupPatch(BaseModel):
    name:           str | None = Field(default=None, min_length=1, max_length=80)
    description:    str | None = Field(default=None, max_length=300)
    destination_ids: list[int] | None = None


def _dest_dict(d: Destination) -> dict:
    return {
        "id": d.id, "tenant_id": d.tenant_id, "alliance": d.tenant.name, "alliance_slug": d.tenant.slug,
        "server_id": d.server_id, "server_name": d.server.name, "guild_id": d.server.guild_id,
        "channel_id": d.channel_id, "role_id": d.role_id, "label": d.label,
        "post_by_default": d.post_by_default, "leadership_only": d.leadership_only,
    }


def _group_dict(g: AudienceGroup) -> dict:
    return {
        "id": g.id, "kingdom_id": g.kingdom_id, "name": g.name, "description": g.description,
        "destination_ids": [d.id for d in g.destinations],
        "destinations": [
            {"id": d.id, "label": d.label, "alliance": d.tenant.name, "channel_id": d.channel_id,
             "leadership_only": d.leadership_only}
            for d in g.destinations
        ],
    }


def _allowed_server_ids(tenant: Tenant) -> set[int]:
    return {tenant.server_id} | {r.server_id for r in tenant.secondary_servers}


async def _usage(db: AsyncSession, destination_id: int) -> list[str]:
    """What still uses a destination: events (by change row) and groups."""
    events = (await db.execute(
        select(Event.name).join(EventDestination, EventDestination.event_id == Event.id)
        .where(EventDestination.destination_id == destination_id)
    )).scalars().all()
    groups = (await db.execute(
        select(AudienceGroup.name).join(AudienceGroupDestination, AudienceGroupDestination.group_id == AudienceGroup.id)
        .where(AudienceGroupDestination.destination_id == destination_id)
    )).scalars().all()
    return [f'event "{n}"' for n in sorted(set(events))] + [f'group "{n}"' for n in sorted(set(groups))]


async def _reload_destination(db: AsyncSession, destination_id: int) -> Destination:
    row = await db.execute(select(Destination).where(Destination.id == destination_id).execution_options(populate_existing=True))
    return row.scalar_one()


# ---------------------------------------------------------------- destinations
@router.get("/destinations")
async def list_destinations(
    kingdom: bool = Query(default=False),
    tenants: list[Tenant] = Depends(get_current_tenants),
    db: AsyncSession = Depends(get_db),
):
    """The selected alliance's destinations (every accessible alliance's for
    '*'). With `kingdom=true`, every destination in the Kingdom, so the Events
    form can offer a group's or another audience alliance's destinations."""
    if kingdom:
        kingdom_ids = {t.kingdom_id for t in tenants}
        stmt = select(Destination).join(Tenant, Tenant.id == Destination.tenant_id).where(Tenant.kingdom_id.in_(kingdom_ids))
    else:
        stmt = select(Destination).where(Destination.tenant_id.in_([t.id for t in tenants]))
    rows = await db.execute(stmt.order_by(Destination.tenant_id, Destination.id))
    return [_dest_dict(d) for d in rows.scalars().all()]


@router.post("/destinations", status_code=201)
async def create_destination(
    payload: DestinationIn,
    tenant: Tenant = Depends(require_tenant_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if payload.server_id not in _allowed_server_ids(tenant):
        raise HTTPException(status_code=422, detail="The server must be this alliance's primary or a secondary server")
    dest = Destination(
        tenant_id=tenant.id, server_id=payload.server_id, channel_id=payload.channel_id, role_id=payload.role_id,
        label=payload.label, post_by_default=payload.post_by_default, leadership_only=payload.leadership_only,
    )
    db.add(dest)
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise_friendly_integrity_error(e, _DESTINATION_TAKEN, fallback="Could not create destination")
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="destinations", row_id=dest.id, action="create",
        after={"label": dest.label, "server_id": dest.server_id, "channel_id": dest.channel_id, "role_id": dest.role_id,
               "post_by_default": dest.post_by_default, "leadership_only": dest.leadership_only},
    )
    await resync_kingdom_events(db, tenant.kingdom_id)
    await db.commit()
    return _dest_dict(await _reload_destination(db, dest.id))


@router.patch("/destinations/{destination_id}")
async def update_destination(
    destination_id: int, payload: DestinationPatch,
    tenant: Tenant = Depends(require_tenant_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    dest = await db.get(Destination, destination_id)
    if dest is None or dest.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Destination not found")
    if payload.server_id is not None and payload.server_id not in _allowed_server_ids(tenant):
        raise HTTPException(status_code=422, detail="The server must be this alliance's primary or a secondary server")
    before = {"label": dest.label, "server_id": dest.server_id, "channel_id": dest.channel_id, "role_id": dest.role_id,
              "post_by_default": dest.post_by_default, "leadership_only": dest.leadership_only}
    for field in ("label", "server_id", "channel_id", "role_id", "post_by_default", "leadership_only"):
        value = getattr(payload, field)
        if value is not None:
            setattr(dest, field, value.strip() if isinstance(value, str) else value)
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise_friendly_integrity_error(e, _DESTINATION_TAKEN, fallback="Could not update destination")
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="destinations", row_id=dest.id, action="update",
        before=before,
        after={"label": dest.label, "server_id": dest.server_id, "channel_id": dest.channel_id, "role_id": dest.role_id,
               "post_by_default": dest.post_by_default, "leadership_only": dest.leadership_only},
    )
    await resync_kingdom_events(db, tenant.kingdom_id)
    await db.commit()
    return _dest_dict(await _reload_destination(db, dest.id))


@router.delete("/destinations/{destination_id}", status_code=204)
async def delete_destination(
    destination_id: int,
    tenant: Tenant = Depends(require_tenant_owner),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    dest = await db.get(Destination, destination_id)
    if dest is None or dest.tenant_id != tenant.id:
        raise HTTPException(status_code=404, detail="Destination not found")
    used_by = await _usage(db, destination_id)
    if used_by:
        raise HTTPException(
            status_code=409,
            detail=f"This destination is still used by {', '.join(used_by)}. Remove it there first.",
        )
    before = {"label": dest.label, "channel_id": dest.channel_id, "role_id": dest.role_id}
    await db.delete(dest)
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="destinations", row_id=destination_id,
        action="delete", before=before,
    )
    await db.flush()
    await resync_kingdom_events(db, tenant.kingdom_id)
    await db.commit()


# ---------------------------------------------------------------------- groups
async def _check_group_destinations(db: AsyncSession, kingdom_id: int, destination_ids: list[int]) -> list[int]:
    unique = list(dict.fromkeys(destination_ids))
    for did in unique:
        dest = await db.get(Destination, did)
        if dest is None or dest.tenant.kingdom_id != kingdom_id:
            raise HTTPException(status_code=422, detail=f"Destination {did} not found in this Kingdom")
    return unique


async def _set_group_members(db: AsyncSession, group: AudienceGroup, destination_ids: list[int]) -> None:
    await db.execute(delete(AudienceGroupDestination).where(AudienceGroupDestination.group_id == group.id))
    for did in destination_ids:
        db.add(AudienceGroupDestination(group_id=group.id, destination_id=did))
    await db.flush()


async def _reload_group(db: AsyncSession, group_id: int) -> AudienceGroup:
    row = await db.execute(select(AudienceGroup).where(AudienceGroup.id == group_id).execution_options(populate_existing=True))
    return row.scalar_one()


@router.get("/audience-groups")
async def list_groups(tenant: Tenant = Depends(get_current_tenant), db: AsyncSession = Depends(get_db)):
    rows = await db.execute(
        select(AudienceGroup).where(AudienceGroup.kingdom_id == tenant.kingdom_id).order_by(AudienceGroup.name)
    )
    return [_group_dict(g) for g in rows.scalars().all()]


@router.post("/audience-groups", status_code=201)
async def create_group(
    payload: GroupIn,
    tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_kingdom_coordinator(db, user, tenant.kingdom_id)
    members = await _check_group_destinations(db, tenant.kingdom_id, payload.destination_ids)
    group = AudienceGroup(kingdom_id=tenant.kingdom_id, name=payload.name, description=payload.description.strip())
    db.add(group)
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise_friendly_integrity_error(e, _GROUP_TAKEN, fallback="Could not create audience group")
    await _set_group_members(db, group, members)
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="audience_groups", row_id=group.id, action="create",
        after={"name": group.name, "destination_ids": members},
    )
    await db.commit()
    return _group_dict(await _reload_group(db, group.id))


@router.patch("/audience-groups/{group_id}")
async def update_group(
    group_id: int, payload: GroupPatch,
    tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_kingdom_coordinator(db, user, tenant.kingdom_id)
    group = await db.get(AudienceGroup, group_id)
    if group is None or group.kingdom_id != tenant.kingdom_id:
        raise HTTPException(status_code=404, detail="Audience group not found")
    before = {"name": group.name, "description": group.description, "destination_ids": [d.id for d in group.destinations]}
    if payload.name is not None:
        group.name = payload.name.strip()
    if payload.description is not None:
        group.description = payload.description.strip()
    members = None
    if payload.destination_ids is not None:
        members = await _check_group_destinations(db, tenant.kingdom_id, payload.destination_ids)
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise_friendly_integrity_error(e, _GROUP_TAKEN, fallback="Could not update audience group")
    if members is not None:
        await _set_group_members(db, group, members)
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="audience_groups", row_id=group.id, action="update",
        before=before, after={"name": group.name, "description": group.description,
                              "destination_ids": members if members is not None else before["destination_ids"]},
    )
    await resync_kingdom_events(db, tenant.kingdom_id)
    await db.commit()
    return _group_dict(await _reload_group(db, group.id))


@router.delete("/audience-groups/{group_id}", status_code=204)
async def delete_group(
    group_id: int,
    tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_kingdom_coordinator(db, user, tenant.kingdom_id)
    group = await db.get(AudienceGroup, group_id)
    if group is None or group.kingdom_id != tenant.kingdom_id:
        raise HTTPException(status_code=404, detail="Audience group not found")
    used_by = (await db.execute(
        select(Event.name).join(EventGroup, EventGroup.event_id == Event.id).where(EventGroup.group_id == group_id)
    )).scalars().all()
    if used_by:
        names = ", ".join(f'"{n}"' for n in sorted(set(used_by)))
        raise HTTPException(status_code=409, detail=f"This group is still used by event(s) {names}. Remove it there first.")
    before = {"name": group.name, "destination_ids": [d.id for d in group.destinations]}
    await db.execute(delete(AudienceGroupDestination).where(AudienceGroupDestination.group_id == group_id))
    await db.delete(group)
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="audience_groups", row_id=group_id,
        action="delete", before=before,
    )
    await db.commit()
