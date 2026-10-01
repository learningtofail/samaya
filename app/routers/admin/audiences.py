"""Audiences (spec §68).

An Audience is a named, reusable list of destinations (Discord server, channel
and optional role) that belongs to the Kingdom. Kingdom coordinators manage
Audiences and their destinations. An alliance uses an Audience through a link
(with a `post_by_default` flag); an alliance owner manages its own links.
Every change regenerates the pending deliveries of the Kingdom's events, so
the schedule follows the new setup.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import (
    AllianceAudience, Audience, AudienceDestination, DiscordServer, Event, EventAudience, Tenant, User, UserTenant,
)
from services.audit import log_change
from services.db_errors import raise_friendly_integrity_error
from services.event_engine import resync_kingdom_events

from .deps import (
    check_kingdom_coordinator, get_current_tenant, get_current_tenants, get_current_user, require_not_viewer,
)

router = APIRouter(prefix="/api")

_COORDINATOR_ONLY = "Managing Kingdom audiences requires kingdom coordinator access"
_AUDIENCE_TAKEN = {
    "uq_audience_label": "This Kingdom already has an audience with that label",
    "audiences_kingdom_id_key": "This Kingdom already has an audience with that label",
}
_DESTINATION_TAKEN = {
    "uq_audience_destination": "This audience already posts to that server, channel and role",
    "audience_destinations_audience_id_key": "This audience already posts to that server, channel and role",
}


def _digits(v: str | None) -> str | None:
    v = (v or "").strip() if v is not None else v
    if v and not v.isdigit():
        raise ValueError("A Discord ID is digits only")
    return v


class DestinationIn(BaseModel):
    server_id:  int
    channel_id: str = Field(min_length=1, max_length=32)
    role_id:    str = Field(default="", max_length=32)

    _ids = field_validator("channel_id", "role_id")(lambda cls, v: _digits(v))


class LinkIn(BaseModel):
    tenant_id:       int
    post_by_default: bool = True


class AudienceIn(BaseModel):
    label:           str = Field(min_length=1, max_length=60)
    leadership_only: bool = False
    destinations:    list[DestinationIn] = Field(min_length=1)
    links:           list[LinkIn] = []

    @field_validator("label")
    @classmethod
    def _label(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Label cannot be blank")
        return v


class AudiencePatch(BaseModel):
    label:           str | None = Field(default=None, min_length=1, max_length=60)
    leadership_only: bool | None = None
    destinations:    list[DestinationIn] | None = Field(default=None, min_length=1)
    links:           list[LinkIn] | None = None


class AllianceLinkIn(BaseModel):
    audience_id:     int
    post_by_default: bool = True


class AllianceLinksIn(BaseModel):
    links: list[AllianceLinkIn] = []


async def _tenant_index(db: AsyncSession) -> dict[int, Tenant]:
    return {t.id: t for t in (await db.execute(select(Tenant))).scalars().unique().all()}


def _audience_dict(a: Audience, tenants: dict[int, Tenant]) -> dict:
    links = []
    for link in sorted(a.links, key=lambda x: x.tenant_id):
        t = tenants.get(link.tenant_id)
        if t is not None:
            links.append({"tenant_id": t.id, "alliance": t.name, "alliance_slug": t.slug,
                          "post_by_default": link.post_by_default})
    return {
        "id": a.id, "kingdom_id": a.kingdom_id, "label": a.label, "leadership_only": a.leadership_only,
        "destinations": [
            {"id": d.id, "server_id": d.server_id, "server_name": d.server.name, "guild_id": d.server.guild_id,
             "channel_id": d.channel_id, "role_id": d.role_id}
            for d in a.destinations
        ],
        "links": links,
    }


def _snapshot(a: Audience) -> dict:
    return {
        "label": a.label, "leadership_only": a.leadership_only,
        "destinations": sorted((d.server_id, d.channel_id, d.role_id) for d in a.destinations),
        "links": sorted((x.tenant_id, x.post_by_default) for x in a.links),
    }


async def _usage(db: AsyncSession, audience_id: int, tenants: dict[int, Tenant]) -> list[str]:
    """What still uses an Audience: alliance links and events (by change row)."""
    links = (await db.execute(
        select(AllianceAudience.tenant_id).where(AllianceAudience.audience_id == audience_id)
    )).scalars().all()
    events = (await db.execute(
        select(Event.name).join(EventAudience, EventAudience.event_id == Event.id)
        .where(EventAudience.audience_id == audience_id)
    )).scalars().all()
    return (
        [f'alliance "{tenants[t].name}"' for t in sorted(set(links)) if t in tenants]
        + [f'event "{n}"' for n in sorted(set(events))]
    )


async def _reload(db: AsyncSession, audience_id: int) -> Audience:
    row = await db.execute(select(Audience).where(Audience.id == audience_id).execution_options(populate_existing=True))
    return row.scalar_one()


async def _check_servers(db: AsyncSession, kingdom_id: int, destinations: list[DestinationIn]) -> None:
    for server_id in {d.server_id for d in destinations}:
        server = await db.get(DiscordServer, server_id)
        if server is None or server.kingdom_id != kingdom_id:
            raise HTTPException(status_code=422, detail="Every server must belong to this Kingdom")


async def _check_link_tenants(db: AsyncSession, kingdom_id: int, tenant_ids: list[int]) -> None:
    for tid in tenant_ids:
        t = await db.get(Tenant, tid)
        if t is None or t.kingdom_id != kingdom_id:
            raise HTTPException(status_code=422, detail=f"Alliance {tid} is not in this Kingdom")


async def _set_destinations(db: AsyncSession, audience: Audience, wanted: list[DestinationIn]) -> None:
    """Makes the Audience's destinations match `wanted`, keeping existing rows
    (and so their delivery history) when the server, channel and role match."""
    keep = {(d.server_id, d.channel_id, d.role_id) for d in wanted}
    existing = {(d.server_id, d.channel_id, d.role_id): d for d in audience.destinations}
    for key, row in existing.items():
        if key not in keep:
            audience.destinations.remove(row)
    for d in wanted:
        key = (d.server_id, d.channel_id, d.role_id)
        if key not in existing:
            audience.destinations.append(
                AudienceDestination(server_id=d.server_id, channel_id=d.channel_id, role_id=d.role_id))
            existing[key] = audience.destinations[-1]


async def _set_links(db: AsyncSession, audience_id: int, links: list[LinkIn]) -> None:
    await db.execute(delete(AllianceAudience).where(AllianceAudience.audience_id == audience_id))
    for link in {x.tenant_id: x for x in links}.values():
        db.add(AllianceAudience(tenant_id=link.tenant_id, audience_id=audience_id,
                                post_by_default=link.post_by_default))
    await db.flush()


@router.get("/kingdom-servers")
async def list_kingdom_servers(
    tenants: list[Tenant] = Depends(get_current_tenants), db: AsyncSession = Depends(get_db),
):
    """The Discord servers of the selected alliance's Kingdom, for the Audience
    editor. Names only: tokens and keys stay superadmin-only."""
    rows = await db.execute(
        select(DiscordServer).where(DiscordServer.kingdom_id.in_({t.kingdom_id for t in tenants}))
        .order_by(DiscordServer.name)
    )
    return [{"id": s.id, "name": s.name, "kingdom_id": s.kingdom_id} for s in rows.scalars().all()]


# ------------------------------------------------------------------- audiences
@router.get("/audiences")
async def list_audiences(
    tenants: list[Tenant] = Depends(get_current_tenants), db: AsyncSession = Depends(get_db),
):
    """Every Audience in the selected alliance's Kingdom (every accessible
    Kingdom's for '*'), each with its destinations and the alliances using it."""
    kingdom_ids = {t.kingdom_id for t in tenants}
    rows = await db.execute(
        select(Audience).where(Audience.kingdom_id.in_(kingdom_ids)).order_by(Audience.kingdom_id, Audience.id)
    )
    index = await _tenant_index(db)
    return [_audience_dict(a, index) for a in rows.scalars().unique().all()]


@router.post("/audiences", status_code=201)
async def create_audience(
    payload: AudienceIn,
    tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_kingdom_coordinator(db, user, tenant.kingdom_id, _COORDINATOR_ONLY)
    await _check_servers(db, tenant.kingdom_id, payload.destinations)
    await _check_link_tenants(db, tenant.kingdom_id, [x.tenant_id for x in payload.links])
    audience = Audience(kingdom_id=tenant.kingdom_id, label=payload.label, leadership_only=payload.leadership_only,
                        destinations=[], links=[])
    db.add(audience)
    try:
        await db.flush()
        await _set_destinations(db, audience, payload.destinations)
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise_friendly_integrity_error(e, {**_AUDIENCE_TAKEN, **_DESTINATION_TAKEN}, fallback="Could not create audience")
    await _set_links(db, audience.id, payload.links)
    audience = await _reload(db, audience.id)
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="audiences", row_id=audience.id, action="create",
        after=_snapshot(audience),
    )
    await resync_kingdom_events(db, tenant.kingdom_id)
    await db.commit()
    return _audience_dict(await _reload(db, audience.id), await _tenant_index(db))


@router.patch("/audiences/{audience_id}")
async def update_audience(
    audience_id: int, payload: AudiencePatch,
    tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_kingdom_coordinator(db, user, tenant.kingdom_id, _COORDINATOR_ONLY)
    audience = await db.get(Audience, audience_id)
    if audience is None or audience.kingdom_id != tenant.kingdom_id:
        raise HTTPException(status_code=404, detail="Audience not found")
    if payload.destinations is not None:
        await _check_servers(db, tenant.kingdom_id, payload.destinations)
    if payload.links is not None:
        await _check_link_tenants(db, tenant.kingdom_id, [x.tenant_id for x in payload.links])
    before = _snapshot(audience)
    if payload.label is not None:
        audience.label = payload.label.strip()
    if payload.leadership_only is not None:
        audience.leadership_only = payload.leadership_only
    try:
        if payload.destinations is not None:
            await _set_destinations(db, audience, payload.destinations)
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise_friendly_integrity_error(e, {**_AUDIENCE_TAKEN, **_DESTINATION_TAKEN}, fallback="Could not update audience")
    if payload.links is not None:
        await _set_links(db, audience.id, payload.links)
    audience = await _reload(db, audience.id)
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="audiences", row_id=audience.id, action="update",
        before=before, after=_snapshot(audience),
    )
    await resync_kingdom_events(db, tenant.kingdom_id)
    await db.commit()
    return _audience_dict(await _reload(db, audience.id), await _tenant_index(db))


@router.delete("/audiences/{audience_id}", status_code=204)
async def delete_audience(
    audience_id: int,
    tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_kingdom_coordinator(db, user, tenant.kingdom_id, _COORDINATOR_ONLY)
    audience = await db.get(Audience, audience_id)
    if audience is None or audience.kingdom_id != tenant.kingdom_id:
        raise HTTPException(status_code=404, detail="Audience not found")
    used_by = await _usage(db, audience_id, await _tenant_index(db))
    if used_by:
        raise HTTPException(
            status_code=409,
            detail=f"This audience is still used by {', '.join(used_by)}. Remove it there first.",
        )
    before = _snapshot(audience)
    await db.delete(audience)
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="audiences", row_id=audience_id,
        action="delete", before=before,
    )
    await db.flush()
    await resync_kingdom_events(db, tenant.kingdom_id)
    await db.commit()


async def _require_alliance_links_access(db: AsyncSession, user: User, tenant: Tenant) -> None:
    """Owner of the alliance, coordinator of its Kingdom, or superadmin (spec §68.2)."""
    if user.is_superadmin:
        return
    owner = (await db.execute(
        select(UserTenant).where(UserTenant.user_id == user.id, UserTenant.tenant_id == tenant.id,
                                 UserTenant.role == "owner")
    )).scalar_one_or_none()
    if owner is None:
        await check_kingdom_coordinator(
            db, user, tenant.kingdom_id, "Choosing an alliance's audiences requires owner or kingdom coordinator access")


@router.put("/alliance-audiences")
async def set_alliance_audiences(
    payload: AllianceLinksIn,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Replaces the selected alliance's Audience links (spec §68.3)."""
    await _require_alliance_links_access(db, user, tenant)
    wanted = {x.audience_id: x.post_by_default for x in payload.links}
    for aid in wanted:
        audience = await db.get(Audience, aid)
        if audience is None or audience.kingdom_id != tenant.kingdom_id:
            raise HTTPException(status_code=422, detail=f"Audience {aid} not found in this Kingdom")
    before = sorted((await db.execute(
        select(AllianceAudience.audience_id, AllianceAudience.post_by_default)
        .where(AllianceAudience.tenant_id == tenant.id))).all())
    await db.execute(delete(AllianceAudience).where(AllianceAudience.tenant_id == tenant.id))
    for aid, default in wanted.items():
        db.add(AllianceAudience(tenant_id=tenant.id, audience_id=aid, post_by_default=default))
    await db.flush()
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id, table_name="alliance_audiences", row_id=tenant.id, action="update",
        before={"links": [list(r) for r in before]}, after={"links": sorted(wanted.items())},
    )
    await resync_kingdom_events(db, tenant.kingdom_id)
    await db.commit()
    return {"tenant_id": tenant.id, "links": [{"audience_id": a, "post_by_default": p} for a, p in sorted(wanted.items())]}
