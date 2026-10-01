"""Database-level guarantees of the unified event model (spec §66.1): the
CHECK and UNIQUE constraints hold even if the API layer is bypassed."""
from datetime import date, datetime, time, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from models.db import Delivery, Event, EventAlliance, EventOccurrence, EventReminder, EventType


@pytest.fixture
async def etype(db_session, tenant):
    t = EventType(kingdom_id=tenant["kingdom_id"], name="General")
    db_session.add(t)
    await db_session.commit()
    return t


def _event(tenant, etype, **kw):
    fields = dict(owning_tenant_id=tenant["id"], type_id=etype.id, name="E", start_time_utc=time(19, 0),
                  anchor_date=date(2026, 10, 5), recurrence_kind="none")
    fields.update(kw)
    return Event(**fields)


async def _rejected(db_session, *rows):
    db_session.add_all(rows)
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


class TestEventConstraints:
    async def test_defaults(self, db_session, tenant, etype):
        e = _event(tenant, etype)
        db_session.add(e)
        await db_session.commit()
        assert e.series_id and len(e.series_id) == 32
        assert e.active is True and e.duration_hours is None and e.scope == "alliance"

    async def test_series_ids_are_unique_per_event(self, db_session, tenant, etype):
        a, b = _event(tenant, etype), _event(tenant, etype)
        db_session.add_all([a, b])
        await db_session.commit()
        assert a.series_id != b.series_id

    @pytest.mark.parametrize("kw", [
        {"recurrence_kind": "none", "interval_days": 7},
        {"recurrence_kind": "interval_days", "interval_days": None},
        {"recurrence_kind": "interval_days", "interval_days": 0},
        {"recurrence_kind": "weekly"},
        {"scope": "galaxy"},
        {"duration_hours": 0},
        {"until_date": date(2026, 10, 1)},
    ])
    async def test_check_constraints(self, db_session, tenant, etype, kw):
        await _rejected(db_session, _event(tenant, etype, **kw))

    async def test_until_equal_to_anchor_is_allowed(self, db_session, tenant, etype):
        db_session.add(_event(tenant, etype, until_date=date(2026, 10, 5)))
        await db_session.commit()


class TestEventTypeConstraints:
    async def test_name_unique_per_kingdom(self, db_session, tenant, etype):
        await _rejected(db_session, EventType(kingdom_id=tenant["kingdom_id"], name="General"))

    async def test_positive_defaults(self, db_session, tenant):
        await _rejected(db_session, EventType(kingdom_id=tenant["kingdom_id"], name="A", default_duration_hours=0))
        await _rejected(db_session, EventType(kingdom_id=tenant["kingdom_id"], name="B", default_interval_days=0))


class TestChildTables:
    async def test_reminder_rules(self, db_session, tenant, etype):
        e = _event(tenant, etype)
        db_session.add(e)
        await db_session.commit()
        event_id = e.id
        await _rejected(db_session, EventReminder(event_id=event_id, minutes_before=-1))
        db_session.add(EventReminder(event_id=event_id, minutes_before=10))
        await db_session.commit()
        await _rejected(db_session, EventReminder(event_id=event_id, minutes_before=10))

    async def test_one_audience_row_per_alliance(self, db_session, tenant, etype):
        e = _event(tenant, etype)
        db_session.add(e)
        await db_session.commit()
        event_id = e.id
        db_session.add(EventAlliance(event_id=event_id, tenant_id=tenant["id"]))
        await db_session.commit()
        await _rejected(db_session, EventAlliance(event_id=event_id, tenant_id=tenant["id"]))

    async def test_one_occurrence_per_date_and_valid_status(self, db_session, tenant, etype):
        e = _event(tenant, etype)
        db_session.add(e)
        await db_session.commit()
        event_id = e.id
        start = datetime(2026, 10, 5, 19, tzinfo=timezone.utc)
        occ = EventOccurrence(event_id=event_id, occurrence_date=date(2026, 10, 5), start_datetime_utc=start)
        db_session.add(occ)
        await db_session.commit()
        assert occ.status == "scheduled" and occ.end_datetime_utc is None
        await _rejected(db_session, EventOccurrence(event_id=event_id, occurrence_date=date(2026, 10, 5), start_datetime_utc=start))
        await _rejected(db_session, EventOccurrence(event_id=event_id, occurrence_date=date(2026, 10, 6), start_datetime_utc=start, status="done"))


class TestDeliveryConstraints:
    async def _occurrence(self, db_session, tenant, etype):
        e = _event(tenant, etype)
        db_session.add(e)
        await db_session.commit()
        occ = EventOccurrence(event_id=e.id, occurrence_date=date(2026, 10, 5),
                              start_datetime_utc=datetime(2026, 10, 5, 19, tzinfo=timezone.utc))
        db_session.add(occ)
        await db_session.commit()
        return occ.id

    def _delivery(self, occ_id, tenant, **kw):
        fields = dict(occurrence_id=occ_id, tenant_id=tenant["id"], kind="discord_event", reminder_minutes=-1,
                      due_at_utc=datetime(2026, 9, 28, 19, tzinfo=timezone.utc))
        fields.update(kw)
        return Delivery(**fields)

    async def test_defaults_and_uniqueness(self, db_session, tenant, etype):
        occ = await self._occurrence(db_session, tenant, etype)  # an id
        d = self._delivery(occ, tenant)
        db_session.add(d)
        await db_session.commit()
        assert d.status == "pending"
        await _rejected(db_session, self._delivery(occ, tenant))
        # a different reminder offset is a different delivery
        db_session.add_all([self._delivery(occ, tenant, kind="reminder", reminder_minutes=m) for m in (60, 10)])
        await db_session.commit()

    @pytest.mark.parametrize("kw", [
        {"kind": "sms"},
        {"status": "queued"},
        {"kind": "reminder", "reminder_minutes": -1},
        {"kind": "discord_event", "reminder_minutes": 10},
    ])
    async def test_check_constraints(self, db_session, tenant, etype, kw):
        occ = await self._occurrence(db_session, tenant, etype)  # an id
        await _rejected(db_session, self._delivery(occ, tenant, **kw))
