"""Spec §40-43 — the public feedback/request/error-report board and its
admin triage view. Covers: creating each ticket kind (feedback, event
request, announcement request, error report), the public listing shape
(no submitter_contact, correct sort order), anonymous upvote toggling,
admin full-detail listing (including submitter_contact), status updates,
and viewer-role rejection on the status-change endpoint.
"""
import pytest

from models.db import EventDefinition, Occurrence, Ticket
from datetime import date, datetime, time, timedelta, timezone


@pytest.mark.asyncio
class TestPublicTicketCreation:
    async def test_create_feedback_ticket(self, client):
        r = await client.post("/api/tickets", json={
            "kind": "feedback", "error_type": "Suggestion",
            "title": "Add dark mode", "description": "Would be nice to have a dark theme.",
        })
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["kind"] == "feedback"
        assert body["status"] == "open"
        assert body["upvote_count"] == 1

    async def test_create_event_request_with_alliance(self, client, tenant):
        r = await client.post("/api/tickets", json={
            "kind": "event_request",
            "title": "Weekly training war",
            "description": "Proposed timing: every Saturday\n\nWe want a recurring training war.",
            "tenant_slug": tenant["slug"],
        })
        assert r.status_code == 201, r.text

    async def test_create_event_request_unknown_alliance_404s(self, client):
        r = await client.post("/api/tickets", json={
            "kind": "event_request", "title": "X", "description": "Y", "tenant_slug": "nope",
        })
        assert r.status_code == 404

    async def test_error_report_requires_a_related_row(self, client):
        r = await client.post("/api/tickets", json={
            "kind": "error", "error_type": "Wrong channel", "title": "Issue: X", "description": "wrong channel",
        })
        assert r.status_code == 422

    async def test_error_report_with_related_occurrence(self, client, tenant, db_session):
        event = EventDefinition(
            owning_tenant_id=tenant["id"], name="Weekly Reset", interval_days=7,
            start_time_utc=time(0, 0), duration_hours=1.0, anchor_date=date.today(),
        )
        db_session.add(event)
        await db_session.commit()
        await db_session.refresh(event)
        start = datetime.now(timezone.utc) + timedelta(hours=1)
        occ = Occurrence(
            event_id=event.id, tenant_id=tenant["id"], occurrence_date=start.date(),
            start_datetime_utc=start, end_datetime_utc=start + timedelta(hours=1),
        )
        db_session.add(occ)
        await db_session.commit()
        await db_session.refresh(occ)

        r = await client.post("/api/tickets", json={
            "kind": "error", "error_type": "Wrong date or time",
            "title": f"Issue: {event.name}", "description": "This says Tuesday but it's Wednesday.",
            "related_occurrence_id": occ.id, "tenant_slug": tenant["slug"],
        })
        assert r.status_code == 201, r.text

    async def test_blank_title_rejected(self, client):
        r = await client.post("/api/tickets", json={"kind": "feedback", "title": "   ", "description": "x"})
        assert r.status_code == 422

    async def test_submitter_contact_never_in_create_response(self, client):
        r = await client.post("/api/tickets", json={
            "kind": "feedback", "title": "X", "description": "Y", "submitter_contact": "me@example.com",
        })
        assert "submitter_contact" not in r.json()


@pytest.mark.asyncio
class TestPublicTicketListing:
    async def test_sorted_by_upvote_count_descending(self, client, db_session):
        low = Ticket(kind="feedback", title="Low", description="d", status="open", upvote_count=1)
        high = Ticket(kind="feedback", title="High", description="d", status="open", upvote_count=5)
        db_session.add_all([low, high])
        await db_session.commit()

        r = await client.get("/api/tickets")
        assert r.status_code == 200
        titles = [t["title"] for t in r.json()]
        assert titles.index("High") < titles.index("Low")

    async def test_declined_tickets_excluded(self, client, db_session):
        t = Ticket(kind="feedback", title="Declined one", description="d", status="declined", upvote_count=1)
        db_session.add(t)
        await db_session.commit()

        r = await client.get("/api/tickets")
        assert all(row["title"] != "Declined one" for row in r.json())

    async def test_submitter_contact_never_in_public_listing(self, client, db_session):
        t = Ticket(kind="feedback", title="X", description="d", status="open", upvote_count=1, submitter_contact="secret@example.com")
        db_session.add(t)
        await db_session.commit()

        r = await client.get("/api/tickets")
        assert all("submitter_contact" not in row for row in r.json())


@pytest.mark.asyncio
class TestAnonymousUpvoting:
    async def test_vote_then_unvote_toggles(self, client, db_session):
        t = Ticket(kind="feedback", title="X", description="d", status="open", upvote_count=1)
        db_session.add(t)
        await db_session.commit()
        await db_session.refresh(t)

        client.headers["X-Voter-Id"] = "voter-abc"
        r1 = await client.post(f"/api/tickets/{t.id}/vote")
        assert r1.status_code == 200
        assert r1.json()["upvote_count"] == 2
        assert r1.json()["voted_by_me"] is True

        r2 = await client.post(f"/api/tickets/{t.id}/vote")
        assert r2.json()["upvote_count"] == 1
        assert r2.json()["voted_by_me"] is False

    async def test_voted_by_me_reflected_in_listing(self, client, db_session):
        t = Ticket(kind="feedback", title="X", description="d", status="open", upvote_count=1)
        db_session.add(t)
        await db_session.commit()
        await db_session.refresh(t)

        client.headers["X-Voter-Id"] = "voter-xyz"
        await client.post(f"/api/tickets/{t.id}/vote")
        r = await client.get("/api/tickets")
        row = next(row for row in r.json() if row["id"] == t.id)
        assert row["voted_by_me"] is True

    async def test_vote_requires_voter_id_header(self, client, db_session):
        t = Ticket(kind="feedback", title="X", description="d", status="open", upvote_count=1)
        db_session.add(t)
        await db_session.commit()
        await db_session.refresh(t)
        client.headers.pop("X-Voter-Id", None)
        r = await client.post(f"/api/tickets/{t.id}/vote")
        assert r.status_code == 400

    async def test_vote_unknown_ticket_404s(self, client):
        client.headers["X-Voter-Id"] = "voter-1"
        r = await client.post("/api/tickets/999999/vote")
        assert r.status_code == 404


@pytest.mark.asyncio
class TestAdminTicketTriage:
    async def test_admin_list_includes_submitter_contact(self, client, db_session):
        t = Ticket(kind="feedback", title="X", description="d", status="open", upvote_count=1, submitter_contact="me@example.com")
        db_session.add(t)
        await db_session.commit()

        r = await client.get("/admin/api/tickets")
        assert r.status_code == 200
        row = next(row for row in r.json() if row["title"] == "X")
        assert row["submitter_contact"] == "me@example.com"

    async def test_admin_list_is_kingdom_wide_across_tenants(self, client, db_session, tenant, second_tenant):
        t1 = Ticket(kind="feedback", title="For MOD", description="d", tenant_id=tenant["id"], status="open", upvote_count=1)
        t2 = Ticket(kind="feedback", title="For NSR", description="d", tenant_id=second_tenant["id"], status="open", upvote_count=1)
        db_session.add_all([t1, t2])
        await db_session.commit()

        # client defaults to X-Tenant-Slug: mod, but the ticket list must
        # still include the other tenant's ticket too (no per-alliance filter).
        r = await client.get("/admin/api/tickets")
        titles = {row["title"] for row in r.json()}
        assert {"For MOD", "For NSR"} <= titles

    async def test_update_status(self, client, db_session):
        t = Ticket(kind="feedback", title="X", description="d", status="open", upvote_count=1)
        db_session.add(t)
        await db_session.commit()
        await db_session.refresh(t)

        r = await client.patch(f"/admin/api/tickets/{t.id}", json={"status": "planned"})
        assert r.status_code == 200
        assert r.json()["status"] == "planned"

    async def test_invalid_status_rejected(self, client, db_session):
        t = Ticket(kind="feedback", title="X", description="d", status="open", upvote_count=1)
        db_session.add(t)
        await db_session.commit()
        await db_session.refresh(t)
        r = await client.patch(f"/admin/api/tickets/{t.id}", json={"status": "not-a-real-status"})
        assert r.status_code == 422

    async def test_viewer_cannot_update_status(self, db_session, tenant, make_user_and_client):
        t = Ticket(kind="feedback", title="X", description="d", status="open", upvote_count=1)
        db_session.add(t)
        await db_session.commit()
        await db_session.refresh(t)

        client2, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")])
        client2.headers["X-Tenant-Slug"] = tenant["slug"]
        r = await client2.get("/admin/api/tickets")
        assert r.status_code == 200  # viewer can still read

        r2 = await client2.patch(f"/admin/api/tickets/{t.id}", json={"status": "planned"})
        assert r2.status_code == 403
        await client2.aclose()
