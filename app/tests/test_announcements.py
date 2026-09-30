"""Tests for scheduled announcements: creation and its validation,
permission scoping to the creator's own tenant access, the per-minute
delivery job's independent per-target success/failure, and cancellation.
"""
from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models.db import Announcement, AnnouncementTarget
from scheduler.announcements import send_scheduled_announcements
from services.time_utils import ensure_utc


def _future_iso(minutes=60):
    """A genuinely future ISO timestamp for a create-announcement payload —
    routers/admin/announcements.py now rejects scheduled_for <= now, so
    every create call needs a real future time, not just 'now' (which
    would already be in the past by the time the request is processed)."""
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


async def _backdate_to_due(db_session, announcement_id):
    """Simulates a 'due' announcement for a delivery-job test: create it
    with a valid future scheduled_for via the API (satisfying the new
    past-date guard), then move it into the past directly on the ORM
    object — the same technique test_recurring_announcement_rearms_...
    already used below to make a re-armed announcement due a second
    time, now pulled out since four tests need it."""
    announcement = await db_session.get(Announcement, announcement_id)
    announcement.scheduled_for = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db_session.commit()


async def _run_delivery_job(db_engine):
    """send_scheduled_announcements() now takes an injectable
    session_factory (see scheduler/jobs.py's module docstring) —
    point it at this test's own in-memory engine instead of the real
    database models.AsyncSessionLocal defaults to."""
    TestSessionLocal = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    await send_scheduled_announcements(session_factory=TestSessionLocal)


class TestCreateAnnouncement:

    async def test_create_returns_201(self, client: AsyncClient, tenant: dict):
        r = await client.post("/admin/api/announcements", json={
            "title": "Server maintenance",
            "body_markdown": "**Heads up** — brief downtime tonight.",
            "scheduled_for": _future_iso(),
            "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
        })
        assert r.status_code == 201, r.text
        assert r.json()["status"] == "scheduled"
        assert len(r.json()["targets"]) == 1

    async def test_body_over_2000_chars_rejected(self, client: AsyncClient, tenant: dict):
        r = await client.post("/admin/api/announcements", json={
            "title": "Too long", "body_markdown": "x" * 2001,
            "scheduled_for": _future_iso(),
            "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
        })
        assert r.status_code == 422
        assert "2000" in r.json()["detail"]

    async def test_past_scheduled_for_rejected(self, client: AsyncClient, tenant: dict):
        r = await client.post("/admin/api/announcements", json={
            "title": "Too late", "body_markdown": "hi",
            "scheduled_for": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
            "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
        })
        assert r.status_code == 422
        assert "future" in r.json()["detail"]

    async def test_no_targets_rejected(self, client: AsyncClient):
        r = await client.post("/admin/api/announcements", json={
            "title": "No targets", "body_markdown": "hi", "scheduled_for": _future_iso(), "targets": [],
        })
        assert r.status_code == 422

    async def test_unknown_tenant_slug_rejected(self, client: AsyncClient):
        r = await client.post("/admin/api/announcements", json={
            "title": "Bad target", "body_markdown": "hi", "scheduled_for": _future_iso(),
            "targets": [{"tenant_slug": "nonexistent", "discord_channel_id": "chan-1"}],
        })
        assert r.status_code == 404

    async def test_multi_target_creates_one_row_per_target(
        self, client: AsyncClient, tenant: dict, second_tenant: dict
    ):
        r = await client.post("/admin/api/announcements", json={
            "title": "Kingdom-wide notice", "body_markdown": "Everyone please read this.",
            "scheduled_for": _future_iso(),
            "targets": [
                {"tenant_slug": tenant["slug"], "discord_channel_id": "chan-mod"},
                {"tenant_slug": second_tenant["slug"], "discord_channel_id": "chan-nsr"},
            ],
        })
        assert r.status_code == 201, r.text
        assert len(r.json()["targets"]) == 2

    async def test_leadership_only_and_recurring_round_trip(self, client: AsyncClient, tenant: dict):
        r = await client.post("/admin/api/announcements", json={
            "title": "Weekly reminder", "body_markdown": "Don't forget.",
            "scheduled_for": _future_iso(),
            "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
            "leadership_only": True, "recurring": True, "interval_days": 7,
        })
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["leadership_only"] is True
        assert body["recurring"] is True
        assert body["interval_days"] == 7

    async def test_defaults_are_false_and_null(self, client: AsyncClient, tenant: dict):
        r = await client.post("/admin/api/announcements", json={
            "title": "Plain", "body_markdown": "hi", "scheduled_for": _future_iso(),
            "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
        })
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["leadership_only"] is False
        assert body["recurring"] is False
        assert body["interval_days"] is None

    async def test_recurring_without_interval_days_rejected(self, client: AsyncClient, tenant: dict):
        r = await client.post("/admin/api/announcements", json={
            "title": "Bad recurring", "body_markdown": "hi", "scheduled_for": _future_iso(),
            "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
            "recurring": True,
        })
        assert r.status_code == 422

    async def test_recurring_with_non_positive_interval_days_rejected(self, client: AsyncClient, tenant: dict):
        r = await client.post("/admin/api/announcements", json={
            "title": "Bad recurring", "body_markdown": "hi", "scheduled_for": _future_iso(),
            "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
            "recurring": True, "interval_days": 0,
        })
        assert r.status_code == 422

    async def test_interval_days_nulled_when_not_recurring(self, client: AsyncClient, tenant: dict):
        r = await client.post("/admin/api/announcements", json={
            "title": "Ignored interval", "body_markdown": "hi", "scheduled_for": _future_iso(),
            "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
            "recurring": False, "interval_days": 7,
        })
        assert r.status_code == 201, r.text
        assert r.json()["interval_days"] is None


class TestAnnouncementPermissions:

    async def test_cannot_target_tenant_without_access(
        self, make_user_and_client, tenant: dict, second_tenant: dict
    ):
        """A coordinator can only select tenants they hold UserTenant
        access to — matching the invite system's own access list."""
        client, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "coordinator")])
        r = await client.post(
            "/admin/api/announcements",
            json={
                "title": "Sneaky", "body_markdown": "hi", "scheduled_for": _future_iso(),
                "targets": [{"tenant_slug": second_tenant["slug"], "discord_channel_id": "chan-1"}],
            },
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r.status_code == 403
        await client.aclose()

    async def test_can_target_tenant_with_access(
        self, make_user_and_client, tenant: dict, second_tenant: dict
    ):
        client, _ = await make_user_and_client(
            tenant_grants=[(tenant["id"], "coordinator"), (second_tenant["id"], "coordinator")]
        )
        r = await client.post(
            "/admin/api/announcements",
            json={
                "title": "Legit", "body_markdown": "hi", "scheduled_for": _future_iso(),
                "targets": [
                    {"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"},
                    {"tenant_slug": second_tenant["slug"], "discord_channel_id": "chan-2"},
                ],
            },
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        assert r.status_code == 201, r.text
        await client.aclose()


class TestAnnouncementDelivery:

    async def test_due_announcement_delivered_and_marked_posted(
        self, client: AsyncClient, db_session, db_engine, tenant: dict, monkeypatch
    ):
        created = await client.post("/admin/api/announcements", json={
            "title": "Now", "body_markdown": "Delivering now",
            "scheduled_for": _future_iso(),
            "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
        })
        announcement_id = created.json()["id"]
        await _backdate_to_due(db_session, announcement_id)

        calls = []
        async def fake_send(token, channel_id, message):
            calls.append((channel_id, message))
            return True, ""
        monkeypatch.setattr("scheduler.announcements.send_channel_message", fake_send)

        await _run_delivery_job(db_engine)

        announcement = await db_session.get(Announcement, announcement_id)
        assert announcement.status == "posted"
        assert announcement.posted_at is not None
        assert calls == [("chan-1", "Delivering now")]

    async def test_one_target_failing_does_not_block_or_corrupt_the_other(
        self, client: AsyncClient, db_session, db_engine, tenant: dict, second_tenant: dict, monkeypatch
    ):
        created = await client.post("/admin/api/announcements", json={
            "title": "Partial failure", "body_markdown": "Testing resilience",
            "scheduled_for": _future_iso(),
            "targets": [
                {"tenant_slug": tenant["slug"], "discord_channel_id": "chan-mod"},
                {"tenant_slug": second_tenant["slug"], "discord_channel_id": "chan-nsr"},
            ],
        })
        announcement_id = created.json()["id"]
        await _backdate_to_due(db_session, announcement_id)

        async def flaky_send(token, channel_id, message):
            if channel_id == "chan-nsr":
                return False, "502 Discord unavailable"
            return True, ""
        monkeypatch.setattr("scheduler.announcements.send_channel_message", flaky_send)

        await _run_delivery_job(db_engine)

        targets = (await db_session.execute(
            select(AnnouncementTarget).where(AnnouncementTarget.announcement_id == announcement_id)
        )).scalars().all()
        by_channel = {t.discord_channel_id: t for t in targets}
        assert by_channel["chan-mod"].post_status == "posted"
        assert by_channel["chan-nsr"].post_status == "error"

        announcement = await db_session.get(Announcement, announcement_id)
        # Overall status is "posted" because at least one target succeeded
        # — matching the same any_posted logic as kingdom-wide event PostLog.
        assert announcement.status == "posted"

    async def test_not_yet_due_announcement_is_not_delivered(
        self, client: AsyncClient, db_session, db_engine, tenant: dict, monkeypatch
    ):
        created = await client.post("/admin/api/announcements", json={
            "title": "Later", "body_markdown": "Not yet",
            "scheduled_for": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
        })
        announcement_id = created.json()["id"]

        calls = []
        async def fake_send(token, channel_id, message):
            calls.append(channel_id)
            return True, ""
        monkeypatch.setattr("scheduler.announcements.send_channel_message", fake_send)

        await _run_delivery_job(db_engine)

        assert calls == []
        announcement = await db_session.get(Announcement, announcement_id)
        assert announcement.status == "scheduled"


    async def test_recurring_announcement_rearms_instead_of_going_terminal(
        self, client: AsyncClient, db_session, db_engine, tenant: dict, monkeypatch
    ):
        created = await client.post("/admin/api/announcements", json={
            "title": "Weekly check-in", "body_markdown": "Reminder",
            "scheduled_for": _future_iso(),
            "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
            "recurring": True, "interval_days": 7,
        })
        announcement_id = created.json()["id"]
        await _backdate_to_due(db_session, announcement_id)
        original_scheduled_for = ensure_utc((await db_session.get(Announcement, announcement_id)).scheduled_for)

        calls = []
        async def fake_send(token, channel_id, message):
            calls.append(channel_id)
            return True, ""
        monkeypatch.setattr("scheduler.announcements.send_channel_message", fake_send)

        await _run_delivery_job(db_engine)

        announcement = await db_session.get(Announcement, announcement_id)
        # Still scheduled, not posted/failed — recurring rows never go
        # terminal (spec §13.5).
        assert announcement.status == "scheduled"
        assert announcement.posted_at is not None
        assert calls == ["chan-1"]
        new_scheduled_for = ensure_utc(announcement.scheduled_for)
        assert new_scheduled_for > original_scheduled_for
        # Advanced by (a whole number of) 7-day intervals from the
        # original anchor, not from whenever the tick happened to run.
        delta_days = (new_scheduled_for - original_scheduled_for).days
        assert delta_days % 7 == 0 and delta_days > 0

        targets = (await db_session.execute(
            select(AnnouncementTarget).where(AnnouncementTarget.announcement_id == announcement_id)
        )).scalars().all()
        assert all(t.post_status == "pending" for t in targets)

        # And it's due again once the next tick catches up to the new time.
        await db_session.refresh(announcement)
        announcement.scheduled_for = datetime.now(timezone.utc) - timedelta(minutes=1)
        await db_session.commit()

        await _run_delivery_job(db_engine)
        assert calls == ["chan-1", "chan-1"]

    async def test_one_time_announcement_still_goes_terminal(
        self, client: AsyncClient, db_session, db_engine, tenant: dict, monkeypatch
    ):
        created = await client.post("/admin/api/announcements", json={
            "title": "Once", "body_markdown": "hi",
            "scheduled_for": _future_iso(),
            "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
        })
        announcement_id = created.json()["id"]
        await _backdate_to_due(db_session, announcement_id)

        async def fake_send(token, channel_id, message):
            return True, ""
        monkeypatch.setattr("scheduler.announcements.send_channel_message", fake_send)

        await _run_delivery_job(db_engine)

        announcement = await db_session.get(Announcement, announcement_id)
        assert announcement.status == "posted"


class TestCancelAnnouncement:

    async def test_cancel_scheduled_announcement(self, client: AsyncClient, tenant: dict):
        created = await client.post("/admin/api/announcements", json={
            "title": "To cancel", "body_markdown": "hi",
            "scheduled_for": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
        })
        announcement_id = created.json()["id"]

        r = await client.post(f"/admin/api/announcements/{announcement_id}/cancel")
        assert r.status_code == 200

        listed = await client.get("/admin/api/announcements")
        found = next(a for a in listed.json() if a["id"] == announcement_id)
        assert found["status"] == "cancelled"

    async def test_cancelled_announcement_not_delivered(
        self, client: AsyncClient, db_session, db_engine, tenant: dict, monkeypatch
    ):
        created = await client.post("/admin/api/announcements", json={
            "title": "Cancel before due", "body_markdown": "hi",
            "scheduled_for": _future_iso(minutes=5),
            "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
        })
        announcement_id = created.json()["id"]
        await client.post(f"/admin/api/announcements/{announcement_id}/cancel")

        calls = []
        async def fake_send(token, channel_id, message):
            calls.append(channel_id)
            return True, ""
        monkeypatch.setattr("scheduler.announcements.send_channel_message", fake_send)

        await _run_delivery_job(db_engine)
        assert calls == []


class TestRetryFailedTargets:
    """Spec §30 — requeueing an errored AnnouncementTarget for the next
    delivery tick, since nothing else ever revisits a one-time
    announcement once it's gone terminal (posted/failed)."""

    async def test_retry_requeues_failed_target_and_it_delivers_next_tick(
        self, client: AsyncClient, db_session, db_engine, tenant: dict, monkeypatch
    ):
        created = await client.post("/admin/api/announcements", json={
            "title": "Flaky", "body_markdown": "hi",
            "scheduled_for": _future_iso(),
            "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
        })
        announcement_id = created.json()["id"]
        await _backdate_to_due(db_session, announcement_id)

        calls = []
        async def failing_send(token, channel_id, message):
            calls.append(message)
            return False, "502 Discord unavailable"
        monkeypatch.setattr("scheduler.announcements.send_channel_message", failing_send)
        await _run_delivery_job(db_engine)

        # select(), not db_session.get() — .get() short-circuits to the
        # identity map with no SQL if the row's already loaded there, so
        # it wouldn't see the delivery job's changes made via a different
        # session (same staleness class as spec §25.5's Tenant.server bug).
        announcement = (await db_session.execute(
            select(Announcement).where(Announcement.id == announcement_id)
        )).scalars().one()
        assert announcement.status == "failed"
        target = (await db_session.execute(
            select(AnnouncementTarget).where(AnnouncementTarget.announcement_id == announcement_id)
        )).scalars().one()
        assert target.post_status == "error"

        r = await client.post(f"/admin/api/announcements/{announcement_id}/retry-failed-targets")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "scheduled"
        assert r.json()["targets"][0]["post_status"] == "pending"
        assert r.json()["targets"][0]["status_detail"] is None

        # Now let it succeed on the "next tick" — no re-backdating needed,
        # scheduled_for was never advanced past `now`.
        async def succeeding_send(token, channel_id, message):
            calls.append(message)
            return True, ""
        monkeypatch.setattr("scheduler.announcements.send_channel_message", succeeding_send)
        await _run_delivery_job(db_engine)

        # db_session already has this row identity-mapped from the check
        # above, and expire_on_commit=False means a plain select() leaves
        # an already-loaded object's attributes exactly as they were
        # rather than overwriting them from the fresh query result —
        # expire_all() forces the next read to actually hit the DB again
        # (same staleness class as spec §25.5's Tenant.server bug, but for
        # a whole row rather than one relationship).
        db_session.expire_all()
        announcement = (await db_session.execute(
            select(Announcement).where(Announcement.id == announcement_id)
        )).scalars().one()
        assert announcement.status == "posted"
        assert len(calls) == 2  # the original failed attempt, then the retry

    async def test_retry_with_no_failed_targets_400s(self, client: AsyncClient, tenant: dict):
        created = await client.post("/admin/api/announcements", json={
            "title": "Never sent", "body_markdown": "hi",
            "scheduled_for": _future_iso(),
            "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
        })
        announcement_id = created.json()["id"]
        r = await client.post(f"/admin/api/announcements/{announcement_id}/retry-failed-targets")
        assert r.status_code == 400

    async def test_retry_nonexistent_announcement_404s(self, client: AsyncClient, tenant: dict):
        r = await client.post("/admin/api/announcements/999999/retry-failed-targets")
        assert r.status_code == 404

    async def test_cannot_retry_another_tenants_announcement(
        self, client: AsyncClient, tenant: dict, second_tenant: dict
    ):
        created = await client.post(
            "/admin/api/announcements",
            json={
                "title": "MOD's", "body_markdown": "hi", "scheduled_for": _future_iso(),
                "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
            },
            headers={"X-Tenant-Slug": tenant["slug"]},
        )
        announcement_id = created.json()["id"]
        r = await client.post(
            f"/admin/api/announcements/{announcement_id}/retry-failed-targets",
            headers={"X-Tenant-Slug": second_tenant["slug"]},
        )
        assert r.status_code == 404


class TestDeleteAnnouncement:

    async def test_cannot_delete_scheduled_announcement(self, client: AsyncClient, tenant: dict):
        """Cleanup is for finished announcements only — a still-scheduled
        one has to be cancelled first (TestCancelAnnouncement above), same
        as the rest of the admin UI never lets a live thing be deleted out
        from under itself."""
        created = await client.post("/admin/api/announcements", json={
            "title": "Still scheduled", "body_markdown": "hi", "scheduled_for": _future_iso(),
            "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
        })
        announcement_id = created.json()["id"]

        r = await client.delete(f"/admin/api/announcements/{announcement_id}")
        assert r.status_code == 400
        assert "cancel" in r.json()["detail"].lower()

        listed = await client.get("/admin/api/announcements")
        assert any(a["id"] == announcement_id for a in listed.json())

    async def test_can_delete_cancelled_announcement(self, client: AsyncClient, tenant: dict):
        created = await client.post("/admin/api/announcements", json={
            "title": "To cancel then delete", "body_markdown": "hi", "scheduled_for": _future_iso(),
            "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
        })
        announcement_id = created.json()["id"]
        await client.post(f"/admin/api/announcements/{announcement_id}/cancel")

        r = await client.delete(f"/admin/api/announcements/{announcement_id}")
        assert r.status_code == 200

        listed = await client.get("/admin/api/announcements")
        assert not any(a["id"] == announcement_id for a in listed.json())

    async def test_can_delete_posted_announcement(
        self, client: AsyncClient, db_session, db_engine, tenant: dict, monkeypatch
    ):
        created = await client.post("/admin/api/announcements", json={
            "title": "To post then delete", "body_markdown": "hi", "scheduled_for": _future_iso(),
            "targets": [{"tenant_slug": tenant["slug"], "discord_channel_id": "chan-1"}],
        })
        announcement_id = created.json()["id"]
        await _backdate_to_due(db_session, announcement_id)

        async def fake_send(token, channel_id, message):
            return True, ""
        monkeypatch.setattr("scheduler.announcements.send_channel_message", fake_send)
        await _run_delivery_job(db_engine)

        r = await client.delete(f"/admin/api/announcements/{announcement_id}")
        assert r.status_code == 200

        listed = await client.get("/admin/api/announcements")
        assert not any(a["id"] == announcement_id for a in listed.json())

    async def test_delete_nonexistent_announcement_404s(self, client: AsyncClient):
        r = await client.delete("/admin/api/announcements/999999")
        assert r.status_code == 404
