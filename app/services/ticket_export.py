"""Export tickets for the admin (spec §76.5): one JSON file, or a zip with one
Markdown file per ticket. Pure functions over loaded Ticket rows, so they are
tested without a database.

submitter_contact is private, so it is left out unless the caller asks for it.
Front matter values are written with json.dumps, which is also a valid YAML
double-quoted string, so a hostile title (quotes, newlines, colons) cannot
break the file.
"""
import io
import json
import re
import zipfile
from datetime import datetime

from models.db import Ticket
from services.ticket_views import author_name


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def ticket_export_dict(
    ticket: Ticket, *, include_contact: bool, tenant_name: str | None = None, related_name: str | None = None,
) -> dict:
    data = {
        "id": ticket.id,
        "kind": ticket.kind,
        "error_type": ticket.error_type,
        "title": ticket.title,
        "description": ticket.description,
        "status": ticket.status,
        "upvote_count": ticket.upvote_count,
        "alliance": tenant_name,
        "about": related_name,
        "created_at": _iso(ticket.created_at),
        "updated_at": _iso(ticket.updated_at),
        "tags": [g.name for g in ticket.tags],
        "internal_notes": ticket.internal_notes,
        "checklist": [{"body": i.body, "done": bool(i.done)} for i in ticket.checklist],
        "responses": [
            {"author": author_name(r), "body": r.body, "created_at": _iso(r.created_at)} for r in ticket.responses
        ],
    }
    if include_contact:
        data["submitter_contact"] = ticket.submitter_contact
    return data


def export_json(tickets: list[dict], exported_at: datetime) -> bytes:
    payload = {"exported_at": exported_at.isoformat(), "ticket_count": len(tickets), "tickets": tickets}
    return json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")


def slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-") or "ticket"


def _q(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def ticket_markdown(data: dict) -> str:
    title = " ".join(str(data["title"]).split())
    front = [
        "---",
        f"id: {data['id']}",
        f"title: {_q(title)}",
        f"kind: {_q(data['kind'])}",
        f"status: {_q(data['status'])}",
        f"error_type: {_q(data['error_type'])}",
        f"alliance: {_q(data['alliance'])}",
        f"upvotes: {data['upvote_count']}",
        f"created: {_q(data['created_at'])}",
        f"updated: {_q(data['updated_at'])}",
        f"tags: {_q(data['tags'])}",
    ]
    if "submitter_contact" in data:
        front.append(f"submitter_contact: {_q(data['submitter_contact'])}")
    front.append("---")
    out = ["\n".join(front), "", f"# {title}", "", "## Description", "", data["description"], ""]
    if data["about"]:
        out += [f"About: {data['about']}", ""]
    if data["internal_notes"]:
        out += ["## Internal notes", "", data["internal_notes"], ""]
    if data["checklist"]:
        out += ["## Checklist", ""]
        out += [f"- [{'x' if i['done'] else ' '}] {' '.join(i['body'].split())}" for i in data["checklist"]]
        out.append("")
    if data["responses"]:
        out += ["## Public responses", ""]
        for r in data["responses"]:
            out += [f"### {' '.join(r['author'].split())}, {r['created_at'] or ''}".rstrip(", "), "", r["body"], ""]
    return "\n".join(out).rstrip() + "\n"


def export_markdown_zip(tickets: list[dict]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for data in tickets:
            archive.writestr(f"{data['id']:04d}-{slugify(data['title'])}.md", ticket_markdown(data).encode("utf-8"))
    return buffer.getvalue()
