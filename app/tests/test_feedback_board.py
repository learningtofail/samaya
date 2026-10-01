"""Feedback board (spec §40-43, §66.10): public submission and listing, the
active and archived sections, anonymous voting, and admin moderation
(dismiss, edit, delete, public responses, display names)."""
from models.db import Ticket, TicketResponse, User
from sqlalchemy import select
from tests.unified_helpers import make_event, sync

ADMIN = "/admin/api"


async def _ticket(db_session, title="Add dark mode", status="open", votes=1, **kw):
    t = Ticket(kind=kw.pop("kind", "feedback"), title=title, description=kw.pop("description", "Some text."),
               status=status, upvote_count=votes, **kw)
    db_session.add(t)
    await db_session.commit()
    return t.id


async def _set_display_name(db_session, test_user, name="Gibran"):
    user = await db_session.get(User, test_user["id"])
    user.display_name = name
    await db_session.commit()


class TestRateLimit:
    async def test_exceeding_the_limit_returns_429(self, client, monkeypatch):
        import routers.tickets_public as tickets_public
        monkeypatch.setattr(tickets_public._create_ticket_limit, "max_requests", 2)
        monkeypatch.setattr("services.rate_limit.DISABLED", False)
        codes = [(await client.post("/api/tickets", json={
            "kind": "feedback", "title": f"T{n}", "description": "Some description text."})).status_code
            for n in range(3)]
        assert codes == [201, 201, 429]


class TestCreate:
    async def test_feedback(self, client):
        r = await client.post("/api/tickets", json={
            "kind": "feedback", "error_type": "Suggestion", "title": "Dark mode", "description": "Please."})
        assert r.status_code == 201 and r.json()["status"] == "open" and r.json()["upvote_count"] == 1
        assert "submitter_contact" not in r.json()

    async def test_event_request_for_an_alliance(self, client, tenant):
        r = await client.post("/api/tickets", json={
            "kind": "event_request", "title": "Weekly war", "description": "Saturdays", "tenant_slug": tenant["slug"]})
        assert r.status_code == 201

    async def test_unknown_alliance_404(self, client):
        r = await client.post("/api/tickets", json={
            "kind": "event_request", "title": "x", "description": "y", "tenant_slug": "nope"})
        assert r.status_code == 404

    async def test_error_report_needs_an_occurrence(self, client):
        r = await client.post("/api/tickets", json={
            "kind": "error", "error_type": "Other", "title": "Wrong", "description": "It is wrong."})
        assert r.status_code == 422

    async def test_error_report_for_an_unknown_occurrence_is_rejected(self, client):
        r = await client.post("/api/tickets", json={
            "kind": "error", "error_type": "Other", "title": "Wrong", "description": "d",
            "related_occurrence_id": 99999})
        assert r.status_code == 422

    async def test_error_report_for_an_occurrence(self, client, sf, tenant):
        from datetime import date
        from sqlalchemy import select as sel
        from models.db import EventOccurrence
        event_id = await make_event(sf, tenant, anchor=date.today(), interval=None)
        await sync(sf, event_id)
        async with sf() as s:
            occ_id = (await s.execute(sel(EventOccurrence.id))).scalars().first()
        r = await client.post("/api/tickets", json={
            "kind": "error", "error_type": "Wrong date or time", "title": "Late", "description": "Started late",
            "related_occurrence_id": occ_id, "submitter_contact": "me@example.com"})
        assert r.status_code == 201, r.text
        listing = (await client.get(f"{ADMIN}/tickets")).json()
        assert listing[0]["related_name"] == "Bear Hunt" and listing[0]["submitter_contact"] == "me@example.com"

    async def test_blank_title_rejected(self, client):
        assert (await client.post("/api/tickets", json={"kind": "feedback", "title": "  ", "description": "d"})).status_code == 422


class TestPublicBoard:
    async def test_active_and_archived_sections(self, client, db_session):
        for title, status in [("A", "open"), ("B", "planned"), ("C", "in_progress"), ("D", "done"),
                              ("E", "declined"), ("F", "dismissed")]:
            await _ticket(db_session, title, status)
        body = (await client.get("/api/tickets")).json()
        assert sorted(t["title"] for t in body["active"]) == ["A", "B", "C"]
        assert sorted(t["title"] for t in body["archived"]) == ["D", "E"]
        assert all(t["title"] != "F" for t in body["active"] + body["archived"])

    async def test_active_sorted_by_votes(self, client, db_session):
        await _ticket(db_session, "Few", votes=1)
        await _ticket(db_session, "Many", votes=9)
        assert [t["title"] for t in (await client.get("/api/tickets")).json()["active"]] == ["Many", "Few"]

    async def test_submitter_contact_is_never_public(self, client, db_session):
        await _ticket(db_session, submitter_contact="secret@example.com")
        assert "secret@example.com" not in (await client.get("/api/tickets")).text

    async def test_moving_to_the_archive_is_a_status_change_not_a_delay(self, client, db_session):
        ticket_id = await _ticket(db_session, status="open")
        await client.patch(f"{ADMIN}/tickets/{ticket_id}", json={"status": "done"})
        body = (await client.get("/api/tickets")).json()
        assert body["active"] == [] and [t["id"] for t in body["archived"]] == [ticket_id]

    async def test_responses_show_with_the_authors_display_name_and_status(self, client, db_session, test_user):
        await _set_display_name(db_session, test_user, "Gibran")
        ticket_id = await _ticket(db_session, status="planned")
        await client.post(f"{ADMIN}/tickets/{ticket_id}/responses", json={"body": "We will do this."})
        ticket = (await client.get("/api/tickets")).json()["active"][0]
        assert ticket["status"] == "planned"
        assert ticket["responses"][0]["author"] == "Gibran" and ticket["responses"][0]["body"] == "We will do this."

    async def test_renaming_a_user_updates_earlier_responses(self, client, db_session, test_user):
        await _set_display_name(db_session, test_user, "Old Name")
        ticket_id = await _ticket(db_session)
        await client.post(f"{ADMIN}/tickets/{ticket_id}/responses", json={"body": "Hello"})
        await client.patch(f"{ADMIN}/me", json={"display_name": "New Name"})
        assert (await client.get("/api/tickets")).json()["active"][0]["responses"][0]["author"] == "New Name"

    async def test_no_display_name_shows_team_never_the_discord_username(self, client, db_session):
        ticket_id = await _ticket(db_session)
        await client.post(f"{ADMIN}/tickets/{ticket_id}/responses", json={"body": "Hello"})
        text = (await client.get("/api/tickets")).text
        assert '"author":"Team"' in text.replace(" ", "") and "TestSuperadmin" not in text


class TestVoting:
    HEADERS = {"X-Voter-Id": "voter-1"}

    async def test_toggle(self, client, db_session):
        ticket_id = await _ticket(db_session, votes=1)
        r = await client.post(f"/api/tickets/{ticket_id}/vote", headers=self.HEADERS)
        assert r.json() == {"id": ticket_id, "upvote_count": 2, "voted_by_me": True}
        r = await client.post(f"/api/tickets/{ticket_id}/vote", headers=self.HEADERS)
        assert r.json()["upvote_count"] == 1 and r.json()["voted_by_me"] is False

    async def test_voted_by_me_in_listing(self, client, db_session):
        ticket_id = await _ticket(db_session)
        await client.post(f"/api/tickets/{ticket_id}/vote", headers=self.HEADERS)
        mine = (await client.get("/api/tickets", headers=self.HEADERS)).json()["active"][0]
        theirs = (await client.get("/api/tickets", headers={"X-Voter-Id": "other"})).json()["active"][0]
        assert mine["voted_by_me"] is True and theirs["voted_by_me"] is False

    async def test_header_required(self, client, db_session):
        ticket_id = await _ticket(db_session)
        assert (await client.post(f"/api/tickets/{ticket_id}/vote")).status_code == 400

    async def test_archived_tickets_are_read_only(self, client, db_session):
        ticket_id = await _ticket(db_session, status="done")
        assert (await client.post(f"/api/tickets/{ticket_id}/vote", headers=self.HEADERS)).status_code == 409

    async def test_dismissed_and_unknown_tickets_404(self, client, db_session):
        ticket_id = await _ticket(db_session, status="dismissed")
        assert (await client.post(f"/api/tickets/{ticket_id}/vote", headers=self.HEADERS)).status_code == 404
        assert (await client.post("/api/tickets/9999/vote", headers=self.HEADERS)).status_code == 404


class TestAdminModeration:
    async def test_list_includes_dismissed_and_responses(self, client, db_session):
        await _ticket(db_session, "Spam", "dismissed")
        await _ticket(db_session, "Real")
        assert sorted(t["title"] for t in (await client.get(f"{ADMIN}/tickets")).json()) == ["Real", "Spam"]

    async def test_list_is_kingdom_wide(self, client, db_session, tenant, second_tenant):
        await _ticket(db_session, "For NSR", tenant_id=second_tenant["id"])
        assert (await client.get(f"{ADMIN}/tickets")).json()[0]["tenant_slug"] == "nsr"

    async def test_dismiss_and_restore(self, client, db_session):
        ticket_id = await _ticket(db_session)
        assert (await client.patch(f"{ADMIN}/tickets/{ticket_id}", json={"status": "dismissed"})).json()["status"] == "dismissed"
        assert (await client.get("/api/tickets")).json() == {"active": [], "archived": []}
        await client.patch(f"{ADMIN}/tickets/{ticket_id}", json={"status": "open"})
        assert len((await client.get("/api/tickets")).json()["active"]) == 1

    async def test_invalid_status_rejected(self, client, db_session):
        ticket_id = await _ticket(db_session)
        assert (await client.patch(f"{ADMIN}/tickets/{ticket_id}", json={"status": "wontfix"})).status_code == 422

    async def test_edit_text_and_type_is_audited_with_before_and_after(self, client, db_session):
        from models.db import AuditLog
        ticket_id = await _ticket(db_session, "Call me on 555-1234", description="My number is 555-1234")
        r = await client.patch(f"{ADMIN}/tickets/{ticket_id}", json={
            "title": "Call request", "description": "Contact details removed", "kind": "event_request"})
        assert r.status_code == 200 and r.json()["title"] == "Call request" and r.json()["kind"] == "event_request"
        entry = (await db_session.execute(select(AuditLog).where(AuditLog.table_name == "tickets"))).scalars().one()
        assert "555-1234" in entry.before and "555-1234" not in entry.after

    async def test_blank_edit_rejected(self, client, db_session):
        ticket_id = await _ticket(db_session)
        assert (await client.patch(f"{ADMIN}/tickets/{ticket_id}", json={"title": " "})).status_code == 422

    async def test_delete_removes_votes_and_responses_and_is_audited(self, client, db_session):
        from models.db import AuditLog
        ticket_id = await _ticket(db_session)
        await client.post(f"/api/tickets/{ticket_id}/vote", headers={"X-Voter-Id": "v"})
        await client.post(f"{ADMIN}/tickets/{ticket_id}/responses", json={"body": "Hi"})
        assert (await client.delete(f"{ADMIN}/tickets/{ticket_id}")).status_code == 204
        assert (await db_session.execute(select(Ticket))).scalars().all() == []
        assert (await db_session.execute(select(TicketResponse))).scalars().all() == []
        entry = (await db_session.execute(select(AuditLog).where(
            AuditLog.table_name == "tickets", AuditLog.action == "delete"))).scalars().one()
        assert '"response_count": 1' in entry.before

    async def test_coordinator_cannot_delete(self, db_session, tenant, make_user_and_client):
        ticket_id = await _ticket(db_session)
        c, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "coordinator")])
        c.headers["X-Tenant-Slug"] = tenant["slug"]
        assert (await c.delete(f"{ADMIN}/tickets/{ticket_id}")).status_code == 403

    async def test_coordinator_can_dismiss_edit_and_respond(self, db_session, tenant, make_user_and_client):
        ticket_id = await _ticket(db_session)
        c, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "coordinator")])
        c.headers["X-Tenant-Slug"] = tenant["slug"]
        assert (await c.patch(f"{ADMIN}/tickets/{ticket_id}", json={"status": "dismissed"})).status_code == 200
        assert (await c.patch(f"{ADMIN}/tickets/{ticket_id}", json={"title": "Fixed"})).status_code == 200
        assert (await c.post(f"{ADMIN}/tickets/{ticket_id}/responses", json={"body": "ok"})).status_code == 201

    async def test_viewer_sees_everything_and_changes_nothing(self, db_session, tenant, make_user_and_client):
        ticket_id = await _ticket(db_session)
        v, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")])
        v.headers["X-Tenant-Slug"] = tenant["slug"]
        assert (await v.get(f"{ADMIN}/tickets")).status_code == 200
        assert (await v.patch(f"{ADMIN}/tickets/{ticket_id}", json={"status": "done"})).status_code == 403
        assert (await v.post(f"{ADMIN}/tickets/{ticket_id}/responses", json={"body": "x"})).status_code == 403
        assert (await v.delete(f"{ADMIN}/tickets/{ticket_id}")).status_code == 403

    async def test_unknown_ticket_404(self, client):
        assert (await client.patch(f"{ADMIN}/tickets/9999", json={"status": "done"})).status_code == 404


class TestResponses:
    async def test_validation(self, client, db_session):
        ticket_id = await _ticket(db_session)
        url = f"{ADMIN}/tickets/{ticket_id}/responses"
        assert (await client.post(url, json={"body": "   "})).status_code == 422
        assert (await client.post(url, json={"body": "x" * 2001})).status_code == 422
        assert (await client.post(url, json={"body": "x" * 2000})).status_code == 201

    async def test_author_can_edit_and_delete_own_response(self, client, db_session):
        ticket_id = await _ticket(db_session)
        rid = (await client.post(f"{ADMIN}/tickets/{ticket_id}/responses", json={"body": "one"})).json()["id"]
        edited = await client.patch(f"{ADMIN}/tickets/{ticket_id}/responses/{rid}", json={"body": "two"})
        assert edited.json()["body"] == "two"
        assert (await client.delete(f"{ADMIN}/tickets/{ticket_id}/responses/{rid}")).status_code == 204

    async def test_another_coordinator_cannot_change_it_but_a_superadmin_can(
            self, client, db_session, tenant, make_user_and_client):
        ticket_id = await _ticket(db_session)
        c, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "coordinator")])
        c.headers["X-Tenant-Slug"] = tenant["slug"]
        rid = (await c.post(f"{ADMIN}/tickets/{ticket_id}/responses", json={"body": "mine"})).json()["id"]
        # a different coordinator
        other, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "coordinator")], discord_id="other-2")
        other.headers["X-Tenant-Slug"] = tenant["slug"]
        assert (await other.patch(f"{ADMIN}/tickets/{ticket_id}/responses/{rid}", json={"body": "x"})).status_code == 403
        assert (await other.delete(f"{ADMIN}/tickets/{ticket_id}/responses/{rid}")).status_code == 403
        # the default client is a superadmin
        assert (await client.patch(f"{ADMIN}/tickets/{ticket_id}/responses/{rid}", json={"body": "edited"})).status_code == 200
        assert (await client.delete(f"{ADMIN}/tickets/{ticket_id}/responses/{rid}")).status_code == 204

    async def test_response_on_another_ticket_is_404(self, client, db_session):
        a, b = await _ticket(db_session, "A"), await _ticket(db_session, "B")
        rid = (await client.post(f"{ADMIN}/tickets/{a}/responses", json={"body": "x"})).json()["id"]
        assert (await client.patch(f"{ADMIN}/tickets/{b}/responses/{rid}", json={"body": "y"})).status_code == 404

    async def test_archived_tickets_keep_their_responses_public(self, client, db_session):
        ticket_id = await _ticket(db_session, status="declined")
        await client.post(f"{ADMIN}/tickets/{ticket_id}/responses", json={"body": "Why we declined"})
        assert (await client.get("/api/tickets")).json()["archived"][0]["responses"][0]["body"] == "Why we declined"


class TestDisplayNames:
    async def test_user_sets_their_own(self, client):
        assert (await client.patch(f"{ADMIN}/me", json={"display_name": "  Gibran  "})).json()["display_name"] == "Gibran"
        assert (await client.get(f"{ADMIN}/me")).json()["display_name"] == "Gibran"

    async def test_blank_clears_it(self, client):
        await client.patch(f"{ADMIN}/me", json={"display_name": "X"})
        assert (await client.patch(f"{ADMIN}/me", json={"display_name": " "})).json()["display_name"] is None

    async def test_superadmin_sets_anyones(self, client, db_session):
        other = User(discord_id="o1", discord_username="Other", is_superadmin=False)
        db_session.add(other)
        await db_session.commit()
        r = await client.patch(f"{ADMIN}/users/{other.id}", json={"display_name": "Coordinator One"})
        assert r.status_code == 200 and r.json()["display_name"] == "Coordinator One"
        assert r.json()["is_superadmin"] is False

    async def test_non_superadmin_cannot_set_someone_elses(self, db_session, tenant, make_user_and_client):
        other = User(discord_id="o2", discord_username="Other2", is_superadmin=False)
        db_session.add(other)
        await db_session.commit()
        c, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "owner")])
        c.headers["X-Tenant-Slug"] = tenant["slug"]
        assert (await c.patch(f"{ADMIN}/users/{other.id}", json={"display_name": "x"})).status_code == 403

    async def test_superadmin_cannot_demote_self_via_display_name_edit(self, client, test_user):
        assert (await client.patch(f"{ADMIN}/users/{test_user['id']}", json={"is_superadmin": False})).status_code == 400
        assert (await client.patch(f"{ADMIN}/users/{test_user['id']}", json={"display_name": "Me"})).status_code == 200


class TestNotificationDestination:
    async def test_owner_sets_and_clears(self, client, tenant):
        r = await client.put(f"{ADMIN}/notification-destination", json={
            "notification_channel_id": "123456", "notification_role_id": "789"})
        assert r.status_code == 200
        assert (r.json()["notification_channel_id"], r.json()["notification_role_id"]) == ("123456", "789")
        r = await client.put(f"{ADMIN}/notification-destination", json={"notification_role_id": ""})
        assert (r.json()["notification_channel_id"], r.json()["notification_role_id"]) == ("123456", "")

    async def test_ids_must_be_digits(self, client):
        assert (await client.put(f"{ADMIN}/notification-destination", json={"notification_channel_id": "#general"})).status_code == 422

    async def test_coordinator_cannot_change_it(self, tenant, make_user_and_client):
        c, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "coordinator")])
        c.headers["X-Tenant-Slug"] = tenant["slug"]
        assert (await c.put(f"{ADMIN}/notification-destination", json={"notification_channel_id": "1"})).status_code == 403

    async def test_tenant_list_shows_the_destination(self, client, tenant):
        await client.put(f"{ADMIN}/notification-destination", json={"notification_channel_id": "42"})
        listing = (await client.get(f"{ADMIN}/tenants")).json()
        assert next(t for t in listing if t["slug"] == "mod")["notification_channel_id"] == "42"
