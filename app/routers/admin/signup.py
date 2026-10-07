"""Notify me and I'm in setup (spec §87): attendance groups and the Discord role
behind Notify me for an event or an event type.

Groups are Kingdom-owned; Kingdom coordinators write them. A role mapping on an
event is written by whoever can edit the event; one on an event type needs a
Kingdom coordinator. Counts only: no Discord IDs of players ever leave this
module, and nothing here is public.
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import DiscordServer, Event, EventType, SignupGroup, SignupRole, Tenant, User
from services.audit import log_change
from services.db_errors import raise_friendly_integrity_error
from services.discord_client import get_discord
from services.event_engine import platform_bot_token
from services.signup import ROLE_NAME_MAX, group_peers, remove_mapping, unsafe_role_reason

from .deps import check_kingdom_coordinator, get_current_tenants, get_current_user, require_not_viewer
from .unified_events import _get_type_in_kingdom, _load_event, _require_write_access

router = APIRouter(prefix="/api")

_GROUP_TAKEN = {
    "uq_signup_group_name": "This Kingdom already has an attendance group with that name",
    "signup_groups_kingdom_id_key": "This Kingdom already has an attendance group with that name",
}
_COORDINATOR_ONLY = "Managing attendance groups and event type roles requires kingdom coordinator access"


class GroupIn(BaseModel):
    name:              str = Field(min_length=1, max_length=80)
    exclusive_roles:   bool = True
    attendance_window: Literal["none", "day", "week"] = "none"

    @field_validator("name", mode="after")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Name cannot be blank")
        return v


class GroupPatch(BaseModel):
    name:              str | None = Field(default=None, min_length=1, max_length=80)
    exclusive_roles:   bool | None = None
    attendance_window: Literal["none", "day", "week"] | None = None


class SignupRoleIn(BaseModel):
    """Exactly one of: link an existing role, create one named after the event, or remove the mapping."""
    server_id: int
    role_id:   str | None = Field(default=None, max_length=32)
    create:    bool = False
    remove:    bool = False

    @model_validator(mode="after")
    def _one_action(self):
        chosen = [bool(self.role_id), self.create, self.remove]
        if sum(chosen) != 1:
            raise ValueError("Choose exactly one of role_id, create or remove")
        if self.role_id and not self.role_id.isdigit():
            raise ValueError("A Discord role ID is digits only")
        return self


class ServerIn(BaseModel):
    server_id: int


def _group_dict(group: SignupGroup, events: list[Event]) -> dict:
    return {
        "id": group.id, "kingdom_id": group.kingdom_id, "name": group.name,
        "exclusive_roles": group.exclusive_roles, "attendance_window": group.attendance_window,
        "events": [{"id": e.id, "name": e.name, "active": e.active} for e in events],
    }


async def _group_or_404(db: AsyncSession, group_id: int, kingdom_id: int) -> SignupGroup:
    group = await db.get(SignupGroup, group_id)
    if group is None or group.kingdom_id != kingdom_id:
        raise HTTPException(status_code=404, detail="Attendance group not found")
    return group


async def _events_of(db: AsyncSession, group_id: int) -> list[Event]:
    return list((await db.execute(select(Event).where(Event.signup_group_id == group_id).order_by(Event.name))).scalars())


# ── Attendance groups ───────────────────────────────────────

@router.get("/signup-groups")
async def list_groups(tenants: list[Tenant] = Depends(get_current_tenants), db: AsyncSession = Depends(get_db)):
    kingdom_ids = {t.kingdom_id for t in tenants}
    groups = (await db.execute(select(SignupGroup).where(SignupGroup.kingdom_id.in_(kingdom_ids))
                               .order_by(SignupGroup.name))).scalars().all()
    return [_group_dict(g, await _events_of(db, g.id)) for g in groups]


@router.post("/signup-groups", status_code=201)
async def create_group(
    payload: GroupIn, tenant: Tenant = Depends(require_not_viewer), user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await check_kingdom_coordinator(db, user, tenant.kingdom_id, _COORDINATOR_ONLY)
    group = SignupGroup(kingdom_id=tenant.kingdom_id, name=payload.name, exclusive_roles=payload.exclusive_roles,
                        attendance_window=payload.attendance_window)
    db.add(group)
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise_friendly_integrity_error(e, _GROUP_TAKEN)
    await log_change(db, user_id=user.id, tenant_id=tenant.id, table_name="signup_groups", row_id=group.id,
                     action="create", after=_group_audit(group))
    await db.commit()
    return _group_dict(group, [])


def _group_audit(group: SignupGroup) -> dict:
    return {"name": group.name, "exclusive_roles": group.exclusive_roles, "attendance_window": group.attendance_window}


@router.patch("/signup-groups/{group_id}")
async def update_group(
    group_id: int, payload: GroupPatch, tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db),
):
    await check_kingdom_coordinator(db, user, tenant.kingdom_id, _COORDINATOR_ONLY)
    group = await _group_or_404(db, group_id, tenant.kingdom_id)
    before = _group_audit(group)
    given = payload.model_fields_set
    if "name" in given and payload.name is not None:
        group.name = payload.name.strip()
    if "exclusive_roles" in given and payload.exclusive_roles is not None:
        group.exclusive_roles = payload.exclusive_roles
    if "attendance_window" in given and payload.attendance_window is not None:
        group.attendance_window = payload.attendance_window
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise_friendly_integrity_error(e, _GROUP_TAKEN)
    await log_change(db, user_id=user.id, tenant_id=tenant.id, table_name="signup_groups", row_id=group.id,
                     action="update", before=before, after=_group_audit(group))
    await db.commit()
    return _group_dict(group, await _events_of(db, group.id))


@router.delete("/signup-groups/{group_id}")
async def delete_group(
    group_id: int, tenant: Tenant = Depends(require_not_viewer), user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Releases the group's events (no roles or RSVPs change) and answers with their names."""
    await check_kingdom_coordinator(db, user, tenant.kingdom_id, _COORDINATOR_ONLY)
    group = await _group_or_404(db, group_id, tenant.kingdom_id)
    events = await _events_of(db, group.id)
    released = [e.name for e in events]
    for event in events:
        event.signup_group_id = None
    before = _group_audit(group)
    await db.flush()
    await db.delete(group)
    await log_change(db, user_id=user.id, tenant_id=tenant.id, table_name="signup_groups", row_id=group_id,
                     action="delete", before={**before, "released_events": released})
    await db.commit()
    return {"released_events": released}


# ── Role mappings ───────────────────────────────────────────

def _mapping_audit(mapping: SignupRole | None) -> dict | None:
    if mapping is None:
        return None
    return {"event_id": mapping.event_id, "type_id": mapping.type_id, "server_id": mapping.server_id,
            "role_id": mapping.role_id, "role_name": mapping.role_name, "created_by_samaya": mapping.created_by_samaya}


async def _server_in_kingdom(db: AsyncSession, server_id: int, kingdom_id: int) -> DiscordServer:
    server = await db.get(DiscordServer, server_id)
    if server is None or server.kingdom_id != kingdom_id:
        raise HTTPException(status_code=404, detail="Discord server not found")
    return server


async def _existing(db: AsyncSession, *, event: Event | None, etype: EventType | None, server_id: int) -> SignupRole | None:
    scope = SignupRole.event_id == event.id if event is not None else SignupRole.type_id == etype.id
    return (await db.execute(select(SignupRole).where(scope, SignupRole.server_id == server_id))).scalar_one_or_none()


async def _role_context(discord, server: DiscordServer) -> tuple[str, dict, int | None]:
    token = server.bot_token or platform_bot_token()
    if not token:
        raise HTTPException(status_code=502, detail="No Discord bot token is configured for this server")
    roles, bot_top, error = await discord.get_role_context(token, server.guild_id)
    if error:
        raise HTTPException(status_code=502, detail=f"Discord: {error}")
    return token, roles, bot_top


async def _check_group_clash(db: AsyncSession, event: Event, server: DiscordServer, role_id: str) -> None:
    """Two events of one exclusive group must not share a role: Switch could not tell them apart."""
    for peer, mapping, _srv in await group_peers(db, event, server.guild_id):
        if mapping.role_id == role_id:
            raise HTTPException(status_code=422, detail=(
                f"{peer.name} is in the same attendance group and already uses that role. Give each event its own role."))


async def _apply_mapping(
    db: AsyncSession, discord, user: User, tenant: Tenant, *, event: Event | None, etype: EventType | None,
    kingdom_id: int, body: SignupRoleIn,
) -> dict:
    server = await _server_in_kingdom(db, body.server_id, kingdom_id)
    existing = await _existing(db, event=event, etype=etype, server_id=server.id)
    table_row = event.id if event is not None else etype.id
    before = _mapping_audit(existing)

    if body.remove:
        if existing is None:
            raise HTTPException(status_code=404, detail="No Notify me role is set for that server")
        await remove_mapping(db, existing)
        await log_change(db, user_id=user.id, tenant_id=tenant.id, table_name="signup_roles", row_id=table_row,
                         action="delete", before=before)
        await db.commit()
        return {"removed": True}

    token, roles, bot_top = await _role_context(discord, server)
    created = False
    if body.create:
        name = event.name if event is not None else etype.name
        if len(name) > ROLE_NAME_MAX:
            raise HTTPException(status_code=422, detail=(
                f"Discord role names are limited to {ROLE_NAME_MAX} characters and this name has {len(name)}. "
                "Shorten the name or link an existing role."))
        if any(r["name"] == name for r in roles.values()):
            raise HTTPException(status_code=409, detail=(
                f'A role named "{name}" already exists in {server.name}. Link it instead of creating a duplicate.'))
        made, error = await discord.create_role(token, server.guild_id, name, f"Notify me role for {name}")
        if made is None:
            raise HTTPException(status_code=502, detail=f"Discord: {error}")
        role_id, role_name, created = made["id"], made["name"], True
    else:
        role = roles.get(body.role_id)
        reason = unsafe_role_reason(role, bot_top, server.guild_id)
        if reason:
            raise HTTPException(status_code=422, detail=reason)
        role_id, role_name = body.role_id, role["name"]
        if event is not None:
            await _check_group_clash(db, event, server, role_id)

    if existing is not None and existing.role_id != role_id:
        await remove_mapping(db, existing)  # the old role's subscribers go with it unless another mapping uses it
        existing = None
    if existing is None:
        existing = SignupRole(event_id=event.id if event is not None else None,
                              type_id=etype.id if etype is not None else None, server_id=server.id, role_id=role_id)
        db.add(existing)
    existing.role_name = role_name
    if created:
        existing.created_by_samaya = True
    existing.last_error = existing.last_error_at = None
    await db.flush()
    await log_change(db, user_id=user.id, tenant_id=tenant.id, table_name="signup_roles", row_id=table_row,
                     action="update" if before else "create", before=before, after=_mapping_audit(existing))
    await db.commit()
    return {"role_id": role_id, "role_name": role_name, "created_by_samaya": existing.created_by_samaya}


@router.put("/event-types/{type_id}/signup-roles")
async def set_type_role(
    type_id: int, payload: SignupRoleIn, tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db), discord=Depends(get_discord),
):
    await check_kingdom_coordinator(db, user, tenant.kingdom_id, _COORDINATOR_ONLY)
    etype = await _get_type_in_kingdom(db, type_id, tenant.kingdom_id)
    return await _apply_mapping(db, discord, user, tenant, event=None, etype=etype,
                                kingdom_id=tenant.kingdom_id, body=payload)


async def _writable_event(db: AsyncSession, user: User, tenant: Tenant, event_id: int) -> tuple[Event, Tenant]:
    event = await _load_event(db, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")
    await _require_write_access(db, user, tenant, event)
    return event, await db.get(Tenant, event.owning_tenant_id)


@router.put("/events/{event_id}/signup-roles")
async def set_event_role(
    event_id: int, payload: SignupRoleIn, tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db), discord=Depends(get_discord),
):
    event, owner = await _writable_event(db, user, tenant, event_id)
    if event.leadership_only:
        raise HTTPException(status_code=422, detail="A leadership-only event cannot have a Notify me role")
    return await _apply_mapping(db, discord, user, tenant, event=event, etype=None,
                                kingdom_id=owner.kingdom_id, body=payload)


@router.post("/events/{event_id}/signup-roles/find")
async def find_event_roles(
    event_id: int, payload: ServerIn, tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db), discord=Depends(get_discord),
):
    """Server roles named exactly like the event that no event or type maps yet: how a lost mapping is reconnected."""
    event, owner = await _writable_event(db, user, tenant, event_id)
    server = await _server_in_kingdom(db, payload.server_id, owner.kingdom_id)
    _token, roles, bot_top = await _role_context(discord, server)
    taken = {r for (r,) in (await db.execute(select(SignupRole.role_id).where(SignupRole.server_id == server.id)))}
    candidates = []
    for role in roles.values():
        if role["name"] != event.name or role["id"] in taken or role["id"] == server.guild_id or role.get("managed"):
            continue
        candidates.append({"id": role["id"], "name": role["name"],
                           "unsafe_reason": unsafe_role_reason(role, bot_top, server.guild_id)})
    return {"roles": candidates}


@router.post("/events/{event_id}/signup-roles/sync-name")
async def sync_role_name(
    event_id: int, payload: ServerIn, tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db), discord=Depends(get_discord),
):
    """Renames the event's role to the event's current name. Only roles Samaya created are renamed."""
    event, owner = await _writable_event(db, user, tenant, event_id)
    server = await _server_in_kingdom(db, payload.server_id, owner.kingdom_id)
    mapping = await _existing(db, event=event, etype=None, server_id=server.id)
    if mapping is None:
        raise HTTPException(status_code=404, detail="This event has no role of its own in that server")
    if not mapping.created_by_samaya:
        raise HTTPException(status_code=422, detail="Samaya only renames roles it created")
    if len(event.name) > ROLE_NAME_MAX:
        raise HTTPException(status_code=422, detail=f"Discord role names are limited to {ROLE_NAME_MAX} characters")
    token, _roles, _top = await _role_context(discord, server)
    ok, error = await discord.rename_role(token, server.guild_id, mapping.role_id, event.name, f"Event renamed to {event.name}")
    if not ok:
        raise HTTPException(status_code=502, detail=f"Discord: {error}")
    before = _mapping_audit(mapping)
    mapping.role_name = event.name
    await log_change(db, user_id=user.id, tenant_id=tenant.id, table_name="signup_roles", row_id=event.id,
                     action="update", before=before, after=_mapping_audit(mapping))
    await db.commit()
    return {"role_id": mapping.role_id, "role_name": mapping.role_name}
