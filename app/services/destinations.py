"""Where an event's messages go (spec §67.2 to §67.4, as rewritten by §68).

Everything here is pure resolution over the database: no Discord calls. The
delivery engine, the save validation and the admin preview all call the same
functions, so the form can never promise something the engine will not do.

An Audience belongs to the Kingdom and lists destinations (server, channel,
optional role). An alliance uses an Audience through an AllianceAudience link.
Resolution produces **targets**: an (alliance, destination) pair for every
destination of every selected Audience. Targets that share a channel are one
merged send.
"""
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import AllianceAudience, Audience, AudienceDestination, Event, EventAlliance, EventAudience, Tenant

#: Detail text that marks a delivery as folded into another one's send.
MERGED_PREFIX = "Merged into delivery #"


@dataclass(eq=False)
class Target:
    """One alliance posting to one destination of an Audience. The destination's
    own fields are available directly so callers can treat a target like one."""
    tenant: Tenant
    destination: AudienceDestination

    @property
    def tenant_id(self) -> int:
        return self.tenant.id

    @property
    def id(self) -> int:
        return self.destination.id

    @property
    def audience(self) -> Audience:
        return self.destination.audience

    @property
    def label(self) -> str:
        return self.destination.audience.label

    @property
    def channel_id(self) -> str:
        return self.destination.channel_id

    @property
    def role_id(self) -> str:
        return self.destination.role_id

    @property
    def server(self):
        return self.destination.server

    @property
    def leadership_only(self) -> bool:
        return self.destination.audience.leadership_only


@dataclass
class Resolution:
    """The outcome of resolving one event: the targets it posts to, in a
    stable order (alliance id, destination id)."""
    targets: list[Target] = field(default_factory=list)
    #: Audiences that were selected but dropped by the leadership rule.
    dropped: list[Audience] = field(default_factory=list)

    @property
    def destinations(self) -> list[AudienceDestination]:
        """The distinct destinations reached, in first-seen order."""
        seen: dict[int, AudienceDestination] = {}
        for t in self.targets:
            seen.setdefault(t.id, t.destination)
        return list(seen.values())

    @property
    def audiences(self) -> list[Audience]:
        """The distinct Audiences reached, in first-seen order."""
        seen: dict[int, Audience] = {}
        for t in self.targets:
            seen.setdefault(t.audience.id, t.audience)
        return list(seen.values())


def merge_key(target: Target | AudienceDestination) -> tuple[str, str]:
    """Targets that share this key are one send (spec §67.3)."""
    return (target.server.guild_id, target.channel_id)


async def _alliance_links(session: AsyncSession, tenant_ids: list[int]) -> list[tuple[AllianceAudience, Audience]]:
    if not tenant_ids:
        return []
    rows = await session.execute(
        select(AllianceAudience, Audience)
        .join(Audience, Audience.id == AllianceAudience.audience_id)
        .where(AllianceAudience.tenant_id.in_(tenant_ids))
        .order_by(AllianceAudience.tenant_id, Audience.id)
    )
    return [(link, audience) for link, audience in rows.all()]


async def resolve_destinations(
    session: AsyncSession, *, tenants: list[Tenant], owner: Tenant, changes: dict[int, bool], leadership_only: bool,
) -> Resolution:
    """Spec §67.2 as changed by §68. `changes` maps an Audience id to True
    (add) or False (opt out). Step 1: the Audiences each audience alliance
    posts to by default. Step 2: explicit adds. An Audience reached only by an
    add is attributed to the audience alliances linked to it, else to the
    event's owner. Step 3: opt-outs. Then every destination of every chosen
    Audience becomes a target, subject to the leadership rule."""
    by_id = {t.id: t for t in tenants}
    links = await _alliance_links(session, list(by_id))
    audiences: dict[int, Audience] = {a.id: a for _, a in links}
    linked_tenants: dict[int, list[int]] = {}
    chosen: dict[int, list[int]] = {}   # Audience id -> alliance ids that post to it
    for link, audience in links:
        linked_tenants.setdefault(audience.id, []).append(link.tenant_id)
        if link.post_by_default:
            chosen.setdefault(audience.id, []).append(link.tenant_id)

    added = [aid for aid, included in changes.items() if included]
    if added:
        rows = await session.execute(select(Audience).where(Audience.id.in_(added)))
        for a in rows.scalars().all():
            audiences[a.id] = a
            if a.id not in chosen:
                chosen[a.id] = linked_tenants.get(a.id) or [owner.id]
    for aid, included in changes.items():
        if not included:
            chosen.pop(aid, None)

    resolution = Resolution()
    tenants_by_id = {**by_id, owner.id: by_id.get(owner.id, owner)}
    dropped: dict[int, Audience] = {}
    pairs = sorted((tid, d.id, aid) for aid, tids in chosen.items() for tid in set(tids) for d in audiences[aid].destinations)
    for tid, _did, aid in pairs:
        audience = audiences[aid]
        if audience.leadership_only != leadership_only:
            dropped.setdefault(aid, audience)
            continue
        dest = next(d for d in audience.destinations if d.id == _did)
        resolution.targets.append(Target(tenants_by_id[tid], dest))
    for aid in chosen:
        a = audiences[aid]
        if a.leadership_only != leadership_only:
            dropped.setdefault(aid, a)
    resolution.dropped = list(dropped.values())
    return resolution


async def event_selection(session: AsyncSession, event: Event) -> dict[int, bool]:
    """The saved per-Audience changes of an event."""
    return {
        r.audience_id: r.included
        for r in (await session.execute(select(EventAudience).where(EventAudience.event_id == event.id))).scalars().all()
    }


async def resolve_for_event(session: AsyncSession, event: Event, tenants: list[Tenant]) -> Resolution:
    changes = await event_selection(session, event)
    owner = await session.get(Tenant, event.owning_tenant_id)
    return await resolve_destinations(
        session, tenants=tenants, owner=owner, changes=changes, leadership_only=event.leadership_only,
    )


# ------------------------------------------------------------------ merging
@dataclass
class MergedSend:
    """One real Discord message: every target in the group, the alliances
    behind them and the roles to mention."""
    guild_id: str
    channel_id: str
    targets: list[Target]

    @property
    def alliance_names(self) -> list[str]:
        return sorted({t.tenant.name for t in self.targets}, key=str.lower)

    @property
    def role_ids(self) -> list[str]:
        seen: list[str] = []
        for t in self.targets:
            if t.role_id and t.role_id not in seen:
                seen.append(t.role_id)
        return seen


def group_sends(targets: list[Target]) -> list[MergedSend]:
    """Groups targets that share (guild, channel) into sends."""
    groups: dict[tuple[str, str], MergedSend] = {}
    for target in targets:
        key = merge_key(target)
        groups.setdefault(key, MergedSend(key[0], key[1], [])).targets.append(target)
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
        by_alliance = {t.tenant_id: overrides.get(t.tenant_id, base_message) for t in send.targets}
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
    session: AsyncSession, *, tenants: list[Tenant], owner: Tenant, changes: dict[int, bool],
    leadership_only: bool, base_message: str, overrides: dict[int, str],
) -> Plan:
    """Resolve, merge and check one event's Audiences (spec §67.2 to §67.4,
    §68). `problems` holds selection mistakes the save must reject;
    `conflicts` holds message clashes on a shared channel."""
    problems: list[str] = []
    linked_ids = {a.id for _, a in await _alliance_links(session, [t.id for t in tenants])}
    added = [aid for aid, included in changes.items() if included]
    if added:
        rows = await session.execute(select(Audience).where(Audience.id.in_(added)))
        found = {a.id: a for a in rows.scalars().all()}
        for aid in added:
            audience = found.get(aid)
            if audience is None:
                problems.append(f"Audience {aid} does not exist")
            elif audience.kingdom_id != owner.kingdom_id:
                problems.append(f'"{audience.label}" belongs to another Kingdom')
            elif aid not in linked_ids:
                problems.append(
                    f'"{audience.label}" is not used by any alliance in this event. Link it to an alliance in Setup'
                )
            elif audience.leadership_only != leadership_only:
                problems.append(
                    f'"{audience.label}" is {"" if audience.leadership_only else "not "}leadership-only, '
                    f'so a {"non-" if not leadership_only else ""}leadership event cannot use it'
                )
    resolution = await resolve_destinations(
        session, tenants=tenants, owner=owner, changes=changes, leadership_only=leadership_only,
    )
    sends = group_sends(resolution.targets)
    conflicts = find_conflicts(sends, base_message, overrides)
    return Plan(resolution, sends, conflicts, problems)
