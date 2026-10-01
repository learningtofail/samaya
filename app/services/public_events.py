"""What the public site and the ICS feeds may show (spec §66.2, §66.6).

One query path for both, so the rule that matters most, "a leadership-only
event appears nowhere public", exists in exactly one place and one test
class covers it. Everything public reads through `public_rows`.
"""
from dataclasses import dataclass
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.db import AllianceAudience, Audience, AudienceDestination, Delivery, Event, EventAlliance, EventOccurrence, Tenant
from services.event_engine import effective_end, effective_start  # noqa: F401  (re-exported for callers)
from services.recurrence import occurs_on


@dataclass
class PublicRow:
    occurrence: EventOccurrence
    event: Event
    tenant: Tenant                  # whose badge and notification channel this row carries
    override: EventAlliance | None  # that tenant's per-alliance overrides, if any
    with_tenant_fields: bool        # combined view only
    posted: bool                    # any delivery for this occurrence has gone out
    destination: AudienceDestination | None = None  # first destination of the alliance's first default, non-leadership Audience (spec §68)

    @property
    def has_calendar_entry(self) -> bool:
        return self.event.duration_hours is not None

    @property
    def message(self) -> str:
        """Same precedence the sender uses (services/event_engine)."""
        if self.occurrence.message_override:
            return self.occurrence.message_override
        if self.override is not None and self.override.message_override:
            return self.override.message_override
        return self.event.message or ""

    @property
    def channel_id(self) -> str:
        return self.destination.channel_id if self.destination is not None else ""

    @property
    def status(self) -> str:
        if self.occurrence.status == "cancelled":
            return "cancelled"
        return "posted" if self.posted else "pending"


async def public_rows(
    db: AsyncSession, window_start: date, window_end: date, tenant: Tenant | None = None,
) -> list[PublicRow]:
    """Rows for one alliance's page (`tenant` given) or the combined page
    (`tenant` None), in date order.

    Rules, all enforced here:
    - inactive and leadership-only events are never returned;
    - a cancelled occurrence is returned (so the page can say so) only while
      the event's current schedule still includes that date. Occurrences kept
      as history after a split or reschedule are not public;
    - a kingdom-wide event is one row. An alliance event is one row per
      audience alliance in the combined view, and one row in an alliance's own view.
    """
    stmt = (
        select(EventOccurrence, Event, Tenant)
        .join(Event, Event.id == EventOccurrence.event_id)
        .join(Tenant, Tenant.id == Event.owning_tenant_id)
        .where(
            EventOccurrence.occurrence_date >= window_start,
            EventOccurrence.occurrence_date <= window_end,
            Event.active.is_(True),
            Event.leadership_only.is_(False),
        )
        .options(selectinload(Event.reminders))
        .order_by(EventOccurrence.occurrence_date, EventOccurrence.start_datetime_utc, Event.name, Event.id)
    )
    if tenant is not None:
        in_audience = select(EventAlliance.event_id).where(EventAlliance.tenant_id == tenant.id)
        stmt = stmt.where(or_(
            Event.owning_tenant_id == tenant.id,
            (Event.scope == "kingdom-wide") & (Tenant.kingdom_id == tenant.kingdom_id),
            Event.id.in_(in_audience),
        ))
    pairs = (await db.execute(stmt)).unique().all()
    pairs = [
        (occ, event, owner_tenant) for occ, event, owner_tenant in pairs
        if occ.status != "cancelled" or occurs_on(
            event.recurrence_kind, event.anchor_date, event.interval_days, event.until_date, occ.occurrence_date)
    ]
    if not pairs:
        return []

    event_ids = {e.id for _, e, _ in pairs}
    alliance_rows: dict[int, dict[int, EventAlliance]] = {}
    for row in (await db.execute(select(EventAlliance).where(EventAlliance.event_id.in_(event_ids)))).scalars():
        alliance_rows.setdefault(row.event_id, {})[row.tenant_id] = row
    tenants = {t.id: t for t in (await db.execute(select(Tenant))).scalars().unique().all()}
    posted_ids = set((await db.execute(
        select(Delivery.occurrence_id).where(
            Delivery.occurrence_id.in_({o.id for o, _, _ in pairs}), Delivery.status == "posted")
    )).scalars().all())

    defaults: dict[int, AudienceDestination] = {}
    for link, dest in (await db.execute(
        select(AllianceAudience, AudienceDestination)
        .join(Audience, Audience.id == AllianceAudience.audience_id)
        .join(AudienceDestination, AudienceDestination.audience_id == Audience.id)
        .where(AllianceAudience.post_by_default.is_(True), Audience.leadership_only.is_(False))
        .order_by(Audience.id, AudienceDestination.id)
    )).unique().all():
        defaults.setdefault(link.tenant_id, dest)

    rows: list[PublicRow] = []
    for occ, event, owner_tenant in pairs:
        by_tenant = alliance_rows.get(event.id, {})
        posted = occ.id in posted_ids
        if tenant is not None:
            rows.append(PublicRow(occ, event, tenant, by_tenant.get(tenant.id), False, posted, defaults.get(tenant.id)))
        elif event.scope == "kingdom-wide":
            rows.append(PublicRow(occ, event, owner_tenant, by_tenant.get(owner_tenant.id), True, posted,
                                  defaults.get(owner_tenant.id)))
        else:
            audience = [tenants[tid] for tid in sorted(by_tenant) if tid in tenants] or [owner_tenant]
            for member in audience:
                rows.append(PublicRow(occ, event, member, by_tenant.get(member.id), True, posted, defaults.get(member.id)))
    return rows
