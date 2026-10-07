"""Discord slash commands (spec §70): /schedule, /next and /feedback.

Every handler takes the interaction dict Discord sent and returns the JSON
response dict, so tests call them without Discord. `routers/webhooks.py`
verifies the signature and dispatches here. Reads go through
`services/public_events.public_rows`, the single query path for anything
public, so leadership-only and inactive events never appear.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import DiscordServer, Tenant, TenantSecondaryServer, Ticket
from services.event_engine import effective_end, effective_start
from services.public_events import PublicRow, public_rows
from services.rate_limit import RateLimiter
from services.time_poll import handle_component

logger = logging.getLogger(__name__)

# Interaction types (what Discord sends) and callback types (what we answer).
INTERACTION_COMMAND, INTERACTION_COMPONENT, INTERACTION_AUTOCOMPLETE, INTERACTION_MODAL_SUBMIT = 2, 3, 4, 5
CALLBACK_MESSAGE, CALLBACK_AUTOCOMPLETE, CALLBACK_MODAL = 4, 8, 9
EPHEMERAL = 64
MESSAGE_LIMIT = 1900          # Discord allows 2000; leave room for the trailer
DEFAULT_DAYS, MAX_DAYS = 7, 14
FEEDBACK_CATEGORIES = ("Bug", "Suggestion", "Other")
TITLE_MAX, DESCRIPTION_MAX = 120, 2000   # same ceilings as the public board
KINGDOM_LABEL = "Kingdom"

_ALLIANCE_OPTION = {
    "type": 3, "name": "alliance", "description": "Only this alliance's events", "required": False, "autocomplete": True,
}
COMMANDS = [
    {
        "name": "schedule", "type": 1, "description": "Show upcoming events",
        "options": [
            _ALLIANCE_OPTION,
            {"type": 4, "name": "days", "description": f"How many days ahead (1 to {MAX_DAYS})", "required": False,
             "min_value": 1, "max_value": MAX_DAYS},
        ],
    },
    {"name": "next", "type": 1, "description": "Show the next event", "options": [_ALLIANCE_OPTION]},
    {
        "name": "feedback", "type": 1, "description": "Send feedback to the Samaya team",
        "options": [{
            "type": 3, "name": "category", "description": "What kind of feedback", "required": True,
            "choices": [{"name": c, "value": c} for c in FEEDBACK_CATEGORIES],
        }],
    },
]

#: 3 tickets per Discord user per 10 minutes. The per-IP limiter on the web
#: board cannot apply: every interaction arrives from Discord's addresses.
_feedback_limit = RateLimiter("discord_feedback", max_requests=3, window_seconds=600)


def _reply(content: str) -> dict:
    """An ephemeral message that can never ping anyone."""
    return {"type": CALLBACK_MESSAGE, "data": {"content": content, "flags": EPHEMERAL, "allowed_mentions": {"parse": []}}}


def _options(interaction: dict) -> dict:
    return {o["name"]: o.get("value") for o in (interaction.get("data") or {}).get("options") or []}


def _user(interaction: dict) -> dict:
    return (interaction.get("member") or {}).get("user") or interaction.get("user") or {}


# ------------------------------------------------------------------ scope
async def guild_alliances(db: AsyncSession, guild_id: str | None) -> list[Tenant]:
    """The alliances declared on this Discord server: it is their primary
    server or one of their secondary servers. Sharing is never inferred."""
    if not guild_id:
        return []
    primary = select(Tenant).join(DiscordServer, DiscordServer.id == Tenant.server_id).where(
        DiscordServer.guild_id == guild_id)
    secondary = (
        select(Tenant)
        .join(TenantSecondaryServer, TenantSecondaryServer.tenant_id == Tenant.id)
        .join(DiscordServer, DiscordServer.id == TenantSecondaryServer.server_id)
        .where(DiscordServer.guild_id == guild_id)
    )
    found: dict[int, Tenant] = {}
    for stmt in (primary, secondary):
        for tenant in (await db.execute(stmt)).scalars().unique().all():
            found[tenant.id] = tenant
    return sorted(found.values(), key=lambda t: t.name.lower())


async def _find_alliance(db: AsyncSession, text: str) -> Tenant | None:
    wanted = text.strip().lower()
    for tenant in (await db.execute(select(Tenant))).scalars().unique().all():
        if wanted in (tenant.slug.lower(), tenant.name.lower()):
            return tenant
    return None


class Listing:
    """One upcoming occurrence with the labels of the alliances it is for."""

    def __init__(self, row: PublicRow, start: datetime, end: datetime):
        self.row, self.start, self.end = row, start, end
        self.labels: set[str] = set()


async def upcoming(
    db: AsyncSession, interaction: dict, now: datetime, days: int,
) -> tuple[list[Listing], str | None]:
    """Upcoming calendar events for what the interaction asked about.
    Returns (listings, error). `error` is set when the named alliance does not exist."""
    window_start, window_end = now.date(), now.date() + timedelta(days=days)
    named = _options(interaction).get("alliance")
    allowed: set[int] | None = None
    tenant = None
    if named:
        tenant = await _find_alliance(db, str(named))
        if tenant is None:
            return [], f'No alliance named "{named}".'
    else:
        guild = await guild_alliances(db, interaction.get("guild_id"))
        allowed = {t.id for t in guild} or None
    rows = await public_rows(db, window_start, window_end, tenant)
    listings: dict[int, Listing] = {}
    for row in rows:
        if not row.has_calendar_entry or row.occurrence.status == "cancelled":
            continue
        kingdom_wide = row.event.scope == "kingdom-wide"
        if allowed is not None and not kingdom_wide and row.tenant.id not in allowed:
            continue
        start = effective_start(row.occurrence)
        end = effective_end(row.occurrence) or start
        if end <= now:
            continue
        listing = listings.setdefault(row.occurrence.id, Listing(row, start, end))
        listing.labels.add(KINGDOM_LABEL if kingdom_wide else row.tenant.name)
    return sorted(listings.values(), key=lambda li: (li.start, li.row.event.name)), None


def _stamp(moment: datetime) -> str:
    unix = int(moment.astimezone(timezone.utc).timestamp())
    return f"<t:{unix}:f> (<t:{unix}:R>)"


def _line(listing: Listing) -> str:
    labels = ", ".join(sorted(listing.labels, key=str.lower))
    return f"{_stamp(listing.start)} **{listing.row.event.name}** · {labels}"


# --------------------------------------------------------------- commands
async def handle_schedule(db: AsyncSession, interaction: dict, now: datetime) -> dict:
    days = _options(interaction).get("days") or DEFAULT_DAYS
    days = max(1, min(int(days), MAX_DAYS))
    listings, error = await upcoming(db, interaction, now, days)
    if error:
        return _reply(error)
    if not listings:
        return _reply(f"No events in the next {days} day{'' if days == 1 else 's'}.")
    lines, used = [], 0
    for listing in listings:
        line = _line(listing)
        if used + len(line) + 1 > MESSAGE_LIMIT:
            lines.append(f"…and {len(listings) - len(lines)} more.")
            break
        lines.append(line)
        used += len(line) + 1
    return _reply("\n".join(lines))


async def handle_next(db: AsyncSession, interaction: dict, now: datetime) -> dict:
    listings, error = await upcoming(db, interaction, now, MAX_DAYS)
    if error:
        return _reply(error)
    if not listings:
        return _reply(f"No events in the next {MAX_DAYS} days.")
    first = listings[0]
    where = f"\nLocation: {first.row.event.location}" if first.row.event.location else ""
    return _reply(_line(first) + where)


async def handle_autocomplete(db: AsyncSession, interaction: dict) -> dict:
    """Alliance names for the `alliance` option, narrowed to the asker's
    Kingdom when the server maps to one."""
    typed = ""
    for option in (interaction.get("data") or {}).get("options") or []:
        if option.get("focused"):
            typed = str(option.get("value") or "").strip().lower()
    guild = await guild_alliances(db, interaction.get("guild_id"))
    kingdoms = {t.kingdom_id for t in guild}
    tenants = (await db.execute(select(Tenant).order_by(Tenant.name))).scalars().unique().all()
    choices = [
        {"name": t.name, "value": t.slug} for t in tenants
        if (not kingdoms or t.kingdom_id in kingdoms) and (not typed or typed in t.name.lower() or typed in t.slug.lower())
    ][:25]
    return {"type": CALLBACK_AUTOCOMPLETE, "data": {"choices": choices}}


def _text_input(custom_id: str, label: str, style: int, max_length: int) -> dict:
    return {"type": 1, "components": [{
        "type": 4, "custom_id": custom_id, "label": label, "style": style, "min_length": 1,
        "max_length": max_length, "required": True,
    }]}


def handle_feedback_command(interaction: dict) -> dict:
    category = _options(interaction).get("category")
    if category not in FEEDBACK_CATEGORIES:
        return _reply("Pick Bug, Suggestion or Other.")
    return {"type": CALLBACK_MODAL, "data": {
        "custom_id": f"feedback:{category}",
        "title": f"Send feedback: {category}"[:45],
        "components": [
            _text_input("title", "Summary", 1, TITLE_MAX),
            _text_input("description", "Details", 2, DESCRIPTION_MAX),
        ],
    }}


def _modal_values(interaction: dict) -> dict[str, str]:
    values = {}
    for row in (interaction.get("data") or {}).get("components") or []:
        for field in row.get("components") or []:
            values[field.get("custom_id")] = str(field.get("value") or "").strip()
    return values


async def handle_feedback_submit(db: AsyncSession, interaction: dict) -> dict:
    custom_id = str((interaction.get("data") or {}).get("custom_id") or "")
    category = custom_id.partition(":")[2]
    values = _modal_values(interaction)
    title, description = values.get("title", ""), values.get("description", "")
    if category not in FEEDBACK_CATEGORIES or not title or not description:
        return _reply("That feedback was incomplete. Run /feedback again.")
    user = _user(interaction)
    if not _feedback_limit.allow(str(user.get("id") or "unknown")):
        return _reply("You have sent several already. Try again in a few minutes.")
    guild = await guild_alliances(db, interaction.get("guild_id"))
    name = user.get("global_name") or user.get("username") or "unknown"
    ticket = Ticket(
        kind="feedback", error_type=category, title=title[:TITLE_MAX], description=description[:DESCRIPTION_MAX],
        tenant_id=guild[0].id if len(guild) == 1 else None,
        submitter_contact=f"Discord: {name} ({user.get('id', 'unknown')})",
        status="open", upvote_count=1,
    )
    db.add(ticket)
    await db.commit()
    return _reply(f"Thanks, feedback #{ticket.id} is on the board. Moderators can see your Discord name.")


# --------------------------------------------------------------- dispatch
async def handle_interaction(db: AsyncSession, interaction: dict, now: datetime | None = None) -> dict:
    """Answers one non-PING interaction. Never raises: an unexpected failure
    is logged and the user gets a short apology instead of a Discord error."""
    now = now or datetime.now(timezone.utc)
    kind = interaction.get("type")
    try:
        if kind == INTERACTION_AUTOCOMPLETE:
            return await handle_autocomplete(db, interaction)
        if kind == INTERACTION_MODAL_SUBMIT:
            return await handle_feedback_submit(db, interaction)
        if kind == INTERACTION_COMPONENT:  # spec §84: a time poll button
            return await handle_component(db, interaction, now)
        if kind == INTERACTION_COMMAND:
            name = (interaction.get("data") or {}).get("name")
            if name == "schedule":
                return await handle_schedule(db, interaction, now)
            if name == "next":
                return await handle_next(db, interaction, now)
            if name == "feedback":
                return handle_feedback_command(interaction)
        return _reply("Unknown command.")
    except Exception:
        logger.exception("Discord interaction failed (type %s)", kind)
        return _reply("Something went wrong. Try again in a minute.")

