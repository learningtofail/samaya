"""Daily 16:00 UTC auto-post job (spec §51) — the fourth scheduler job,
alongside regeneration.py/reminders.py/announcements.py, same reasoning
for a dedicated file (see regeneration.py's module docstring).

For every occurrence starting within the next 7 days that isn't posted
(or cancelled) yet, either:
  - it doesn't exist on Discord at all yet -> post it normally, through
    the exact same target-resolution/_post_to_one_tenant() path the
    manual "Post"/"Post Selected" buttons use (routers/admin/occurrences.py);
  - a Discord Scheduled Event with the same name and a close-enough start
    time already exists (created directly on Discord, bypassing Samaya
    entirely) -> quietly record it in PostLog instead of creating a
    duplicate;
  - a Discord Scheduled Event with the same name exists but its start
    time differs meaningfully -> flagged as an issue on the occurrence
    (post_status='error', status_detail explains why) rather than
    silently overwritten — a coordinator resolves it deliberately via the
    existing Discord Sync tooling (routers/admin/discord_sync.py), same
    as any other drift that tool already surfaces.

This removes the daily chore of manually checking a "Post?" box and
clicking "Post"/"Post Selected" for every upcoming occurrence. The
Schedule page's manual controls remain available for anything a
coordinator wants to handle by hand (an occurrence starting too soon for
this job to have caught it yet, or a deliberately-cancelled one) — this
job's own selection doesn't depend on the post_to_discord checkbox at
all, since gating on an opt-in flag that defaults to unchecked would
just reintroduce the same daily chore this job exists to remove.

Every public function here takes an optional session_factory, defaulting
to models.AsyncSessionLocal — see scheduler/regeneration.py's module
docstring for why.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from models import AsyncSessionLocal
from models.db import Occurrence, PostLog, Tenant
from services.discord_api import get_guild_events
from services.notifications import send_notification
from services.time_utils import ensure_utc

logger = logging.getLogger(__name__)

WINDOW_DAYS = 7

# How close a Discord event's own scheduled_start_time has to be to what
# Samaya would have posted before it counts as "the same event" rather
# than a same-named-but-different occurrence (a recurring event whose name
# never changes could otherwise false-match a different week's Discord
# event that happens to still be listed).
MATCH_TOLERANCE_MINUTES = 15

# Same floor post_occurrence() enforces on the manual Post button — an
# occurrence this close to starting is better left to a coordinator's own
# judgment call than auto-posted a few minutes before it begins.
MIN_LEAD_TIME_SECONDS = 900


def _find_matching_discord_event(discord_events: list[dict], event_name: str, occ_start_utc: datetime):
    """Looks for a Discord Scheduled Event that could already BE this
    occurrence — matched by name, picking whichever same-named candidate's
    start time is closest to occ_start_utc (there should ordinarily be at
    most one). Returns None if nothing shares the name at all; otherwise
    (matches: bool, discord_event: dict) — matches distinguishes "close
    enough to treat as ours" from "same name, meaningfully different time,
    needs a human to look at it."
    """
    best, best_delta = None, None
    for d_event in discord_events:
        if d_event.get("name") != event_name:
            continue
        raw_start = d_event.get("scheduled_start_time")
        if not raw_start:
            continue
        try:
            d_start = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
        except ValueError:
            continue
        delta = abs((d_start - occ_start_utc).total_seconds())
        if best_delta is None or delta < best_delta:
            best, best_delta = d_event, delta
    if best is None:
        return None
    return best_delta <= MATCH_TOLERANCE_MINUTES * 60, best


async def _auto_post_one_target(session, occ, event, target: Tenant, guild_cache: dict, now: datetime) -> str:
    """Handles one (occurrence, target tenant) pair. Returns one of
    "posted" (a new Discord event was created, or one already existed for
    this exact target), "synced" (an existing Discord event was matched
    and recorded without a new API call), "flagged" (a same-named Discord
    event exists but doesn't match — occ.status_detail explains why), or
    "error" (posting failed, or no bot token configured)."""
    from routers.admin.deps import PLATFORM_BOT_TOKEN, find_post_log
    from routers.admin.occurrences import _post_to_one_tenant

    existing_log = await find_post_log(session, target.id, event.name, occ.occurrence_date)
    if existing_log:
        return "posted" if existing_log.status == "posted" else "error"

    token = target.server.bot_token or PLATFORM_BOT_TOKEN
    if not token:
        occ.status_detail = f"No Discord bot token configured for {target.name}"
        return "error"

    if target.server_id not in guild_cache:
        guild_cache[target.server_id] = await get_guild_events(token, target.server.guild_id)
    discord_events = guild_cache[target.server_id]

    match = _find_matching_discord_event(discord_events, event.name, ensure_utc(occ.start_datetime_utc))
    if match is not None:
        matches, d_event = match
        if matches:
            log = PostLog(
                tenant_id         = target.id,
                event_id          = event.id,
                event_name        = event.name,
                occurrence_date   = occ.occurrence_date,
                discord_event_id  = str(d_event.get("id", "")),
                discord_guild_id  = target.server.guild_id,
                posted_at_utc     = now,
                posted_by         = "system (auto-post, synced from Discord)",
                status            = "posted",
                status_detail     = "Matched an existing Discord event created outside Samaya — synced rather than reposted.",
            )
            session.add(log)
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                return "posted"  # another tick/request already recorded it
            return "synced"

        occ.status_detail = (
            f"A Discord event named “{d_event.get('name')}” already exists for "
            f"{target.name} but its scheduled time doesn’t match this occurrence "
            f"({occ.occurrence_date}) — not auto-posted. Review it on the Sync tab."
        )
        return "flagged"

    result = await _post_to_one_tenant(session, occ, event, target)
    if result["status"] in ("posted", "skipped"):
        return "posted"
    occ.status_detail = result.get("detail")
    return "error"


async def auto_post_upcoming_occurrences(session_factory=None):
    """Runs once daily at UTC 16:00 (see main.py's scheduler setup).

    session_factory: see module docstring — defaults to AsyncSessionLocal.
    """
    logger.info("auto_post_upcoming_occurrences: starting")
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=WINDOW_DAYS)

    posted = synced = flagged = errored = skipped = 0

    async with (session_factory or AsyncSessionLocal)() as session:
        try:
            result = await session.execute(
                select(Occurrence)
                .options(selectinload(Occurrence.event))
                .where(
                    Occurrence.post_status.notin_(("posted", "cancelled")),
                    Occurrence.start_datetime_utc >= now,
                    Occurrence.start_datetime_utc <= horizon,
                )
                .order_by(Occurrence.start_datetime_utc)
            )
            occurrences = result.scalars().all()

            guild_cache: dict = {}  # DiscordServer.id -> Discord's own guild-events list

            for occ in occurrences:
                event = occ.event
                if event is None or not event.active:
                    skipped += 1
                    continue
                if (ensure_utc(occ.start_datetime_utc) - now).total_seconds() < MIN_LEAD_TIME_SECONDS:
                    skipped += 1
                    continue

                owning = await session.get(Tenant, event.owning_tenant_id)
                if owning is None:
                    skipped += 1
                    continue

                from routers.admin.occurrences import _resolve_post_targets
                targets = await _resolve_post_targets(session, event, owning)

                any_posted = False
                any_error = False
                for target in targets:
                    outcome = await _auto_post_one_target(session, occ, event, target, guild_cache, now)
                    if outcome == "posted":
                        posted += 1
                        any_posted = True
                    elif outcome == "synced":
                        synced += 1
                        any_posted = True
                    elif outcome == "flagged":
                        flagged += 1
                        any_error = True
                    else:
                        errored += 1
                        any_error = True

                # Same aggregation convention post_occurrence() already
                # uses for a kingdom-wide fan-out: any target succeeding
                # counts the occurrence as posted overall, even if another
                # target needs attention — occ.status_detail (set per-target
                # above) still carries the specific reason for whichever
                # one didn't.
                if any_posted and not any_error:
                    occ.post_status   = "posted"
                    occ.status_detail = None
                elif any_error:
                    occ.post_status = "error"
                await session.commit()

            logger.info(
                "auto_post_upcoming_occurrences: complete — "
                f"posted={posted} synced={synced} flagged={flagged} errored={errored} skipped={skipped}"
            )

        except Exception as e:
            await session.rollback()
            logger.error(f"auto_post_upcoming_occurrences: {e}")
            await send_notification(
                subject="[Samaya] Daily auto-post job failed",
                body=f"The daily auto-post job failed at {now.isoformat()}.\n\nError: {e}",
            )
