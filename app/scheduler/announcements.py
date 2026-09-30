"""Per-minute scheduled-announcement delivery — see
scheduler/regeneration.py's module docstring for why this was split out
of the old scheduler/jobs.py.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from models import AsyncSessionLocal
from models.db import Announcement, AnnouncementTarget, Tenant
from services.discord_api import send_channel_message
from services.discord_posting import PLATFORM_BOT_TOKEN
from services.templates import render_placeholders
from services.time_utils import ensure_utc

logger = logging.getLogger(__name__)


async def send_scheduled_announcements(session_factory=None):
    """Runs every minute, alongside send_pre_event_reminders. Finds
    Announcement rows due (status='scheduled', scheduled_for <= now),
    and posts each AnnouncementTarget independently — one tenant's
    Discord API failure doesn't block or corrupt another's delivery,
    same principle as PostLog's kingdom-wide fan-out in
    services/discord_posting.py's _post_to_one_tenant.

    session_factory: see module docstring — defaults to AsyncSessionLocal.
    """
    now = datetime.now(timezone.utc)

    async with (session_factory or AsyncSessionLocal)() as session:
        try:
            result = await session.execute(
                select(Announcement).where(Announcement.status == "scheduled")
            )
            due = [a for a in result.scalars().all() if ensure_utc(a.scheduled_for) <= now]
            if not due:
                return

            for announcement in due:
                targets_result = await session.execute(
                    select(AnnouncementTarget).where(
                        AnnouncementTarget.announcement_id == announcement.id,
                        AnnouncementTarget.post_status == "pending",
                    )
                )
                targets = targets_result.scalars().all()

                for target in targets:
                    tenant = await session.get(Tenant, target.tenant_id)
                    if tenant is None:
                        target.post_status = "error"
                        target.status_detail = "Tenant no longer exists"
                        continue

                    token = tenant.server.bot_token or PLATFORM_BOT_TOKEN
                    if not token:
                        target.post_status = "error"
                        target.status_detail = "No Discord bot token configured for this tenant"
                        continue

                    # Resolved per-target, at send time — not once for the
                    # whole announcement — so {alliance_name} is this
                    # target's own tenant (not whichever tenant authored
                    # the announcement) and {send_time}/{event_time} are
                    # fresh even on a recurring announcement's later
                    # occurrences (spec §27).
                    body = render_placeholders(
                        announcement.body_markdown,
                        tenant_name=tenant.name,
                        kingdom_name=tenant.kingdom.name,
                        scheduled_for=ensure_utc(announcement.scheduled_for),
                        event_offset_minutes=announcement.event_offset_minutes,
                    )
                    try:
                        success, error = await send_channel_message(
                            token, target.discord_channel_id, body
                        )
                    except Exception as e:
                        target.post_status = "error"
                        target.status_detail = str(e)
                        logger.error(f"Announcement {announcement.id} failed for tenant {tenant.id}: {e}")
                        continue

                    if success:
                        target.post_status = "posted"
                    else:
                        target.post_status = "error"
                        target.status_detail = error
                        logger.error(f"Announcement {announcement.id} failed for tenant {tenant.id}: {error}")

                # Commit per-announcement, not once at the end of the
                # whole tick — same reasoning as send_pre_event_reminders:
                # a later announcement's failure shouldn't roll back an
                # earlier one's already-delivered targets.
                remaining_result = await session.execute(
                    select(AnnouncementTarget).where(
                        AnnouncementTarget.announcement_id == announcement.id,
                        AnnouncementTarget.post_status == "pending",
                    )
                )
                if not remaining_result.scalars().all():
                    all_targets_result = await session.execute(
                        select(AnnouncementTarget).where(AnnouncementTarget.announcement_id == announcement.id)
                    )
                    all_targets = all_targets_result.scalars().all()
                    any_posted = any(t.post_status == "posted" for t in all_targets)
                    announcement.posted_at = now

                    if announcement.recurring:
                        # Re-arm rather than go terminal (spec §13.5): same
                        # interval-days-from-anchor idea EventDefinition
                        # already uses for occurrences, just applied to a
                        # single row instead of generating new ones. Advance
                        # from the announcement's own scheduled_for (its
                        # anchor), not from `now`, so a delayed tick doesn't
                        # compound drift into the next delivery time.
                        anchor = ensure_utc(announcement.scheduled_for)
                        next_run = anchor + timedelta(days=announcement.interval_days)
                        while next_run <= now:
                            next_run += timedelta(days=announcement.interval_days)
                        announcement.scheduled_for = next_run
                        announcement.status = "scheduled"
                        for t in all_targets:
                            t.post_status = "pending"
                            t.status_detail = None
                            t.discord_message_id = None
                    else:
                        announcement.status = "posted" if any_posted else "failed"

                await session.commit()

        except Exception as e:
            await session.rollback()
            logger.error(f"send_scheduled_announcements: {e}")
