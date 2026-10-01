"""Where an event's messages go (spec §67.2, §67.3, §67.4).

Everything here is pure resolution over the database: no Discord calls. The
delivery engine, the save validation and the admin preview all call the same
functions, so the form can never promise something the engine will not do.
"""
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import (
    AudienceGroupDestination, Destination, Event, EventAlliance, EventDestination, EventGroup, Tenant,
)

#: Detail text that marks a delivery as folded into another one's send.
MERGED_PREFIX = "Merged into delivery #"


@dataclass
class Resolution:
    """The outcome of resolving one event: the destinations it posts to, in a
    stable order (alliance id, destination id)."""
    destinations: list[Destination] = field(default_factory=list)
    #: Destinations that were selected but dropped by the leadership rule.
    dropped: list[Destination] = field(default_factory=list)


def merge_key(destination: Destination) -> tuple[str, str]:
    """Destinations that share this key are one send (spec §67.3)."""
    return (destination.server.guild_id, destination.channel_id)


async def resolve_destinations(
    session: AsyncSession, *, tenants: list[Tenant], group_ids: list[int],
    changes: dict[int, bool], leadership_only: bool,
) -> Resolution:
    """Spec §67.2. `changes` maps destination id to True (add) or False (opt out)."""
    chosen: dict[int, Destination] = {}
    tenant_ids = [t.id for t in tenants]
    if tenant_ids:
        rows = await session.execute(
            select(Destination).where(Destination.tenant_id.in_(tenant_ids), Destination.post_by_default.is_(True))
        )
        chosen.update({d.id: d for d in rows.scalars().all()})
    if group_ids:
        rows = await session.execute(
            select(Destination).join(AudienceGroupDestination, AudienceGroupDestination.destination_id == Destination.id)
            .where(AudienceGroupDestination.group_id.in_(group_ids))
        )
        chosen.update({d.id: d for d in rows.scalars().all()})
    added = [did for did, included in changes.items() if included]
    if added:
        rows = await session.execute(select(Destination).where(Destination.id.in_(added)))
        chosen.update({d.id: d for d in rows.scalars().all()})
    for did, included in changes.items():
        if not included:
            chosen.pop(did, None)

    resolution = Resolution()
    for dest in sorted(chosen.values(), key=lambda d: (d.tenant_id, d.id)):
        if dest.leadership_only == leadership_only:
            resolution.destinations.append(dest)
        else:
            resolution.dropped.append(dest)
    return resolution


async def event_selection(session: AsyncSession, event: Event) -> tuple[list[int], dict[int, bool]]:
    """The saved group ids and per-destination changes of an event."""
    groups = (await session.execute(select(EventGroup.group_id).where(EventGroup.event_id == event.id))).scalars().all()
    changes = {
        r.destination_id: r.included
        for r in (await session.execute(select(EventDestination).where(EventDestination.event_id == event.id))).scalars().all()
    }
    return sorted(groups), changes


async def resolve_for_event(session: AsyncSession, event: Event, tenants: list[Tenant]) -> Resolution:
    group_ids, changes = await event_selection(session, event)
    return await resolve_destinations(
        session, tenants=tenants, group_ids=group_ids, changes=changes, leadership_only=event.leadership_only,
    )


# ------------------------------------------------------------------ merging
@dataclass
class MergedSend:
    """One real Discord message: every destination in the group, the alliances
    they belong to and the roles to mention."""
    guild_id: str
    channel_id: str
    destinations: list[Destination]

    @property
    def alliance_names(self) -> list[str]:
        return sorted({d.tenant.name for d in self.destinations}, key=str.lower)

    @property
    def role_ids(self) -> list[str]:
        seen: list[str] = []
        for d in self.destinations:
            if d.role_id and d.role_id not in seen:
                seen.append(d.role_id)
        return seen


def group_sends(destinations: list[Destination]) -> list[MergedSend]:
    """Groups destinations that share (guild, channel) into sends."""
    groups: dict[tuple[str, str], MergedSend] = {}
    for dest in destinations:
        key = merge_key(dest)
        groups.setdefault(key, MergedSend(key[0], key[1], [])).destinations.append(dest)
    return list(groups.values())


# ---------------------------------------------------------------- conflicts
@dataclass
class Conflict:
    channel_id: str
    guild_id: str
    alliances: list[str]

    def message(self) -> str:
        return (f"Alliances {', '.join(self.alliances)} post to the same channel ({self.channel_id}) with different "
                "messages. Make their wording match or remove the per-alliance override.")


def alliance_overrides(event_alliances: list[EventAlliance]) -> dict[int, str]:
    return {a.tenant_id: a.message_override for a in event_alliances if a.message_override}


def find_conflicts(
    sends: list[MergedSend], base_message: str, overrides: dict[int, str],
) -> list[Conflict]:
    """Spec §67.4: a send conflicts when its alliances resolve to different
    texts (an override against the base text counts). The per-occurrence
    override applies to every alliance, so callers do not pass it here."""
    conflicts = []
    for send in sends:
        by_alliance = {d.tenant_id: overrides.get(d.tenant_id, base_message) for d in send.destinations}
        if len(set(by_alliance.values())) > 1:
            conflicts.append(Conflict(send.channel_id, send.guild_id, send.alliance_names))
    return conflicts


# --------------------------------------------------------------- the plan
@dataclass
class Plan:
    """Everything the admin needs to know about where an event would post."""
    resolution: Resolution
    sends: list[MergedSend]
    conflicts: list[Conflict]
    problems: list[str] = field(default_factory=list)


async def plan_destinations(
    session: AsyncSession, *, tenants: list[Tenant], group_ids: list[int], changes: dict[int, bool],
    leadership_only: bool, base_message: str, overrides: dict[int, str],
) -> Plan:
    """Resolve, merge and check one event's destinations (spec §67.2 to §67.4).
    `problems` holds selection mistakes the save must reject; `conflicts`
    holds message clashes on a shared channel."""
    problems: list[str] = []
    audience_ids = {t.id for t in tenants}
    group_dest_ids: set[int] = set()
    if group_ids:
        rows = await session.execute(
            select(AudienceGroupDestination.destination_id).where(AudienceGroupDestination.group_id.in_(group_ids))
        )
        group_dest_ids = {r[0] for r in rows.all()}
    added = [did for did, included in changes.items() if included]
    if added:
        rows = await session.execute(select(Destination).where(Destination.id.in_(added)))
        found = {d.id: d for d in rows.scalars().all()}
        for did in added:
            dest = found.get(did)
            if dest is None:
                problems.append(f"Destination {did} does not exist")
            elif dest.tenant_id not in audience_ids and did not in group_dest_ids:
                problems.append(f'"{dest.label}" ({dest.tenant.name}) is not in this event\'s audience or groups')
            elif dest.leadership_only != leadership_only:
                problems.append(
                    f'"{dest.label}" is {"" if dest.leadership_only else "not "}leadership-only, '
                    f'so a {"non-" if not leadership_only else ""}leadership event cannot use it'
                )
    resolution = await resolve_destinations(
        session, tenants=tenants, group_ids=group_ids, changes=changes, leadership_only=leadership_only,
    )
    sends = group_sends(resolution.destinations)
    conflicts = find_conflicts(sends, base_message, overrides)
    return Plan(resolution, sends, conflicts, problems)
