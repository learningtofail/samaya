"""Ticket checklists, internal notes, tags and export (spec §76.5)."""
import io
import json
import zipfile
from datetime import datetime, timezone

import pytest
from models.db import AuditLog, Ticket, TicketChecklistItem, TicketResponse, TicketTag
from services.ticket_export import export_json, export_markdown_zip, slugify, ticket_markdown
from sqlalchemy import select

ADMIN = "/admin/api"
SECRET_NOTE = "private note about the reporter"
SECRET_ITEM = "email the reporter privately"
SECRET_TAG = "internal-only-tag"


async def _ticket(db_session, title="Add dark mode", status="open", **kw):
    t = Ticket(kind=kw.pop("kind", "feedback"), title=title, description=kw.pop("description", "Some text."),
               status=status, upvote_count=kw.pop("votes", 1), **kw)
    db_session.add(t)
    await db_session.commit()
    return t.id


async def _audit(db_session, table, action):
    rows = (await db_session.execute(
        select(AuditLog).where(AuditLog.table_name == table, AuditLog.action == action))).scalars().all()
    return [(json.loads(r.before) if r.before else None, json.loads(r.after) if r.after else None) for r in rows]


class TestInternalNotes:
    async def test_set_edit_and_clear(self, client, db_session):
        a = await _ticket(db_session)
        r = await client.patch(f"{ADMIN}/tickets/{a}", json={"internal_notes": "## Plan\n- check the **cache**"})
        assert r.status_code == 200 and r.json()["internal_notes"].startswith("## Plan")
        r = await client.patch(f"{ADMIN}/tickets/{a}", json={"internal_notes": "   "})
        assert r.json()["internal_notes"] is None

    async def test_too_long_is_rejected(self, client, db_session):
        a = await _ticket(db_session)
        assert (await client.patch(f"{ADMIN}/tickets/{a}", json={"internal_notes": "x" * 5001})).status_code == 422

    async def test_notes_are_audited(self, client, db_session):
        a = await _ticket(db_session)
        await client.patch(f"{ADMIN}/tickets/{a}", json={"internal_notes": "first"})
        ((before, after),) = await _audit(db_session, "tickets", "update")
        assert before["internal_notes"] is None and after["internal_notes"] == "first"

    async def test_editing_other_fields_keeps_notes(self, client, db_session):
        a = await _ticket(db_session, internal_notes="keep me")
        r = await client.patch(f"{ADMIN}/tickets/{a}", json={"title": "New title"})
        assert r.json()["internal_notes"] == "keep me"


class TestChecklist:
    async def test_add_toggle_edit_delete(self, client, db_session):
        a = await _ticket(db_session)
        r = await client.post(f"{ADMIN}/tickets/{a}/checklist", json={"body": "  reproduce   the bug "})
        assert r.status_code == 201
        item = r.json()["checklist"][0]
        assert item["body"] == "reproduce the bug" and item["done"] is False
        r = await client.patch(f"{ADMIN}/tickets/{a}/checklist/{item['id']}", json={"done": True})
        assert r.json()["checklist"][0]["done"] is True
        r = await client.patch(f"{ADMIN}/tickets/{a}/checklist/{item['id']}", json={"body": "fix it"})
        assert r.json()["checklist"][0]["body"] == "fix it" and r.json()["checklist"][0]["done"] is True
        r = await client.delete(f"{ADMIN}/tickets/{a}/checklist/{item['id']}")
        assert r.status_code == 200 and r.json()["checklist"] == []

    async def test_items_keep_the_order_they_were_added(self, client, db_session):
        a = await _ticket(db_session)
        for body in ("one", "two", "three"):
            await client.post(f"{ADMIN}/tickets/{a}/checklist", json={"body": body})
        rows = (await client.get(f"{ADMIN}/tickets")).json()
        assert [i["body"] for i in next(r for r in rows if r["id"] == a)["checklist"]] == ["one", "two", "three"]

    async def test_blank_and_long_bodies_are_rejected(self, client, db_session):
        a = await _ticket(db_session)
        assert (await client.post(f"{ADMIN}/tickets/{a}/checklist", json={"body": "   "})).status_code == 422
        assert (await client.post(f"{ADMIN}/tickets/{a}/checklist", json={"body": "x" * 201})).status_code == 422

    async def test_at_most_fifty_items(self, client, db_session):
        a = await _ticket(db_session)
        for n in range(50):
            db_session.add(TicketChecklistItem(ticket_id=a, body=f"item {n}", done=False, position=n))
        await db_session.commit()
        r = await client.post(f"{ADMIN}/tickets/{a}/checklist", json={"body": "one too many"})
        assert r.status_code == 422 and "50" in r.text

    async def test_item_of_another_ticket_is_404(self, client, db_session):
        a, b = await _ticket(db_session, "A"), await _ticket(db_session, "B")
        item = (await client.post(f"{ADMIN}/tickets/{a}/checklist", json={"body": "x"})).json()["checklist"][0]
        assert (await client.patch(f"{ADMIN}/tickets/{b}/checklist/{item['id']}", json={"done": True})).status_code == 404
        assert (await client.delete(f"{ADMIN}/tickets/{b}/checklist/{item['id']}")).status_code == 404

    async def test_writes_are_audited(self, client, db_session):
        a = await _ticket(db_session)
        item = (await client.post(f"{ADMIN}/tickets/{a}/checklist", json={"body": "step"})).json()["checklist"][0]
        await client.patch(f"{ADMIN}/tickets/{a}/checklist/{item['id']}", json={"done": True})
        await client.delete(f"{ADMIN}/tickets/{a}/checklist/{item['id']}")
        assert [x[1]["body"] for x in await _audit(db_session, "ticket_checklist_items", "create")] == ["step"]
        ((before, after),) = await _audit(db_session, "ticket_checklist_items", "update")
        assert before["done"] is False and after["done"] is True
        assert (await _audit(db_session, "ticket_checklist_items", "delete"))[0][0]["body"] == "step"

    async def test_deleting_a_ticket_removes_its_items(self, client, db_session):
        a = await _ticket(db_session)
        await client.post(f"{ADMIN}/tickets/{a}/checklist", json={"body": "step"})
        assert (await client.delete(f"{ADMIN}/tickets/{a}")).status_code == 204
        assert (await db_session.execute(select(TicketChecklistItem))).scalars().all() == []

    async def test_viewer_cannot_change_it(self, db_session, tenant, make_user_and_client):
        viewer, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")])
        a = await _ticket(db_session)
        r = await viewer.post(f"{ADMIN}/tickets/{a}/checklist", json={"body": "x"}, headers={"X-Tenant-Slug": tenant["slug"]})
        assert r.status_code == 403


class TestTags:
    async def test_create_list_rename_recolor_delete(self, client):
        r = await client.post(f"{ADMIN}/ticket-tags", json={"name": " Frontend ", "color": "#2563eb"})
        assert r.status_code == 201 and r.json()["name"] == "Frontend" and r.json()["color"] == "#2563EB"
        tag_id = r.json()["id"]
        assert r.json()["ink"] in ("#000000", "#FFFFFF", "#ffffff", "#000")
        listed = (await client.get(f"{ADMIN}/ticket-tags")).json()
        assert [t["name"] for t in listed] == ["Frontend"] and listed[0]["ticket_count"] == 0
        r = await client.patch(f"{ADMIN}/ticket-tags/{tag_id}", json={"name": "UI", "color": "#111111"})
        assert r.json()["name"] == "UI" and r.json()["color"] == "#111111"
        assert (await client.delete(f"{ADMIN}/ticket-tags/{tag_id}")).status_code == 204
        assert (await client.get(f"{ADMIN}/ticket-tags")).json() == []

    async def test_names_are_unique_ignoring_case(self, client):
        await client.post(f"{ADMIN}/ticket-tags", json={"name": "Bug", "color": "#CC0000"})
        r = await client.post(f"{ADMIN}/ticket-tags", json={"name": "bug", "color": "#CC0000"})
        assert r.status_code == 422 and "already exists" in r.text
        other = (await client.post(f"{ADMIN}/ticket-tags", json={"name": "Other", "color": "#CC0000"})).json()
        r = await client.patch(f"{ADMIN}/ticket-tags/{other['id']}", json={"name": "BUG"})
        assert r.status_code == 422

    async def test_renaming_to_its_own_name_in_another_case_is_allowed(self, client):
        tag = (await client.post(f"{ADMIN}/ticket-tags", json={"name": "bug", "color": "#CC0000"})).json()
        assert (await client.patch(f"{ADMIN}/ticket-tags/{tag['id']}", json={"name": "Bug"})).status_code == 200

    @pytest.mark.parametrize("payload", [
        {"name": "x", "color": "red"}, {"name": "x", "color": "#12345"}, {"name": "  ", "color": "#112233"},
        {"name": "n" * 25, "color": "#112233"},
    ])
    async def test_invalid_tags_are_rejected(self, client, payload):
        assert (await client.post(f"{ADMIN}/ticket-tags", json=payload)).status_code == 422

    async def test_set_a_tickets_tags_and_count_usage(self, client, db_session):
        a, b = await _ticket(db_session, "A"), await _ticket(db_session, "B")
        t1 = (await client.post(f"{ADMIN}/ticket-tags", json={"name": "Bug", "color": "#CC0000"})).json()["id"]
        t2 = (await client.post(f"{ADMIN}/ticket-tags", json={"name": "Docs", "color": "#0066CC"})).json()["id"]
        r = await client.put(f"{ADMIN}/tickets/{a}/tags", json={"tag_ids": [t2, t1, t1]})
        assert r.status_code == 200 and [t["name"] for t in r.json()["tags"]] == ["Bug", "Docs"]
        await client.put(f"{ADMIN}/tickets/{b}/tags", json={"tag_ids": [t1]})
        counts = {t["name"]: t["ticket_count"] for t in (await client.get(f"{ADMIN}/ticket-tags")).json()}
        assert counts == {"Bug": 2, "Docs": 1}
        r = await client.put(f"{ADMIN}/tickets/{a}/tags", json={"tag_ids": []})
        assert r.json()["tags"] == []

    async def test_at_most_eight_tags_per_ticket(self, client, db_session):
        a = await _ticket(db_session)
        ids = [(await client.post(f"{ADMIN}/ticket-tags", json={"name": f"t{n}", "color": "#112233"})).json()["id"] for n in range(9)]
        assert (await client.put(f"{ADMIN}/tickets/{a}/tags", json={"tag_ids": ids[:8]})).status_code == 200
        assert (await client.put(f"{ADMIN}/tickets/{a}/tags", json={"tag_ids": ids})).status_code == 422

    async def test_unknown_tag_id_is_rejected(self, client, db_session):
        a = await _ticket(db_session)
        r = await client.put(f"{ADMIN}/tickets/{a}/tags", json={"tag_ids": [9999]})
        assert r.status_code == 422 and "9999" in r.text

    async def test_deleting_a_tag_removes_it_from_tickets_and_is_audited(self, client, db_session):
        a = await _ticket(db_session)
        tag = (await client.post(f"{ADMIN}/ticket-tags", json={"name": "Old", "color": "#112233"})).json()["id"]
        await client.put(f"{ADMIN}/tickets/{a}/tags", json={"tag_ids": [tag]})
        await client.delete(f"{ADMIN}/ticket-tags/{tag}")
        rows = (await client.get(f"{ADMIN}/tickets")).json()
        assert next(r for r in rows if r["id"] == a)["tags"] == []
        assert (await _audit(db_session, "ticket_tags", "delete"))[0][0]["ticket_count"] == 1

    async def test_deleting_a_ticket_keeps_the_tag(self, client, db_session):
        a = await _ticket(db_session)
        tag = (await client.post(f"{ADMIN}/ticket-tags", json={"name": "Keep", "color": "#112233"})).json()["id"]
        await client.put(f"{ADMIN}/tickets/{a}/tags", json={"tag_ids": [tag]})
        assert (await client.delete(f"{ADMIN}/tickets/{a}")).status_code == 204
        assert [t.name for t in (await db_session.execute(select(TicketTag))).scalars().all()] == ["Keep"]

    async def test_viewer_reads_but_cannot_write(self, db_session, tenant, make_user_and_client):
        viewer, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")])
        h = {"X-Tenant-Slug": tenant["slug"]}
        assert (await viewer.get(f"{ADMIN}/ticket-tags", headers=h)).status_code == 200
        assert (await viewer.post(f"{ADMIN}/ticket-tags", json={"name": "x", "color": "#112233"}, headers=h)).status_code == 403

    async def test_unknown_tag_404(self, client):
        assert (await client.patch(f"{ADMIN}/ticket-tags/999", json={"name": "x"})).status_code == 404
        assert (await client.delete(f"{ADMIN}/ticket-tags/999")).status_code == 404


class TestNothingPrivateIsPublic:
    async def test_public_payload_has_no_notes_checklist_or_tags(self, client, client_no_session, db_session):
        a = await _ticket(db_session, internal_notes=SECRET_NOTE)
        await client.post(f"{ADMIN}/tickets/{a}/checklist", json={"body": SECRET_ITEM})
        tag = (await client.post(f"{ADMIN}/ticket-tags", json={"name": SECRET_TAG[:24], "color": "#112233"})).json()["id"]
        await client.put(f"{ADMIN}/tickets/{a}/tags", json={"tag_ids": [tag]})
        body = (await client_no_session.get("/api/tickets")).text
        assert "Add dark mode" in body
        for secret in (SECRET_NOTE, SECRET_ITEM, SECRET_TAG[:24], "internal_notes", "checklist", "tags"):
            assert secret not in body, secret


class TestExport:
    async def _seed(self, client, db_session):
        a = await _ticket(db_session, "Dark mode", description="Please add it", submitter_contact="me@example.com",
                          internal_notes="Check **contrast**")
        await _ticket(db_session, "Hidden spam", status="dismissed")
        db_session.add(TicketResponse(ticket_id=a, body="On the list.", author_user_id=None))
        await db_session.commit()
        await client.post(f"{ADMIN}/tickets/{a}/checklist", json={"body": "audit colors"})
        tag = (await client.post(f"{ADMIN}/ticket-tags", json={"name": "UI", "color": "#112233"})).json()["id"]
        await client.put(f"{ADMIN}/tickets/{a}/tags", json={"tag_ids": [tag]})
        return a

    async def test_json_leaves_contact_out_by_default(self, client, db_session):
        a = await self._seed(client, db_session)
        r = await client.get(f"{ADMIN}/tickets/export")
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
        assert "attachment" in r.headers["content-disposition"] and r.headers["cache-control"] == "no-store"
        data = r.json()
        assert data["ticket_count"] == 2
        first = next(t for t in data["tickets"] if t["id"] == a)
        assert "submitter_contact" not in first and "me@example.com" not in r.text
        assert first["tags"] == ["UI"] and first["internal_notes"] == "Check **contrast**"
        assert first["checklist"] == [{"body": "audit colors", "done": False}]
        assert first["responses"][0]["body"] == "On the list." and first["responses"][0]["author"] == "Team"
        assert {t["status"] for t in data["tickets"]} == {"open", "dismissed"}

    async def test_json_with_contact_when_asked(self, client, db_session):
        a = await self._seed(client, db_session)
        data = (await client.get(f"{ADMIN}/tickets/export?include_contact=true")).json()
        assert next(t for t in data["tickets"] if t["id"] == a)["submitter_contact"] == "me@example.com"

    async def test_markdown_zip_has_one_file_per_ticket(self, client, db_session):
        a = await self._seed(client, db_session)
        r = await client.get(f"{ADMIN}/tickets/export?format=markdown")
        assert r.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            names = sorted(z.namelist())
            assert names == [f"{a:04d}-dark-mode.md", f"{a + 1:04d}-hidden-spam.md"]
            text = z.read(names[0]).decode()
        assert text.startswith("---\nid: ") and "# Dark mode" in text and "## Internal notes" in text
        assert "- [ ] audit colors" in text and "## Public responses" in text and "me@example.com" not in text

    async def test_bad_format_is_rejected(self, client):
        assert (await client.get(f"{ADMIN}/tickets/export?format=csv")).status_code == 422

    async def test_every_export_is_audited(self, client, db_session):
        await self._seed(client, db_session)
        await client.get(f"{ADMIN}/tickets/export?include_contact=true")
        ((_, after),) = await _audit(db_session, "ticket_exports", "create")
        assert after == {"format": "json", "include_contact": True, "ticket_count": 2}

    async def test_viewer_can_export(self, db_session, tenant, make_user_and_client):
        viewer, _ = await make_user_and_client(tenant_grants=[(tenant["id"], "viewer")])
        r = await viewer.get(f"{ADMIN}/tickets/export", headers={"X-Tenant-Slug": tenant["slug"]})
        assert r.status_code == 200


def _row(**over):
    base = dict(
        id=7, kind="feedback", error_type=None, title="T", description="D", status="open", upvote_count=2,
        alliance=None, about=None, created_at="2026-10-01T00:00:00+00:00", updated_at=None, tags=[],
        internal_notes=None, checklist=[], responses=[],
    )
    base.update(over)
    return base


class TestExportPure:
    def test_hostile_title_cannot_break_front_matter_or_the_heading(self):
        text = ticket_markdown(_row(title='x"\n---\nevil: yes\n# fake'))
        head = text.split("\n---\n", 1)[0].splitlines()
        assert head[0] == "---" and sum(1 for line in text.splitlines() if line == "---") == 2
        assert json.loads(next(line for line in head if line.startswith("title: "))[len("title: "):]) == 'x" --- evil: yes # fake'
        assert "\n# x\" --- evil: yes # fake\n" in text

    def test_slug(self):
        assert slugify("Wrong channel / Duplicate!") == "wrong-channel-duplicate"
        assert slugify("日本語") == "ticket"
        assert len(slugify("a" * 200)) == 40

    def test_zip_names_are_padded_ids(self):
        with zipfile.ZipFile(io.BytesIO(export_markdown_zip([_row(id=3, title="Hello World")]))) as z:
            assert z.namelist() == ["0003-hello-world.md"]

    def test_json_wrapper(self):
        out = json.loads(export_json([_row()], datetime(2026, 10, 3, tzinfo=timezone.utc)))
        assert out["ticket_count"] == 1 and out["exported_at"].startswith("2026-10-03")

    def test_checklist_and_responses_render(self):
        text = ticket_markdown(_row(
            checklist=[{"body": "a\nb", "done": True}], responses=[{"author": "Dev", "body": "Done.", "created_at": "2026-10-02"}]))
        assert "- [x] a b" in text and "### Dev, 2026-10-02" in text and "Done." in text

