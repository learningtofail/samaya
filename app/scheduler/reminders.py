"""Per-minute pre-event reminder delivery — see scheduler/regeneration.py's
module docstring for why this was split out of the old scheduler/jobs.py.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from models import AsyncSessionLocal
from models.db import EventDefinition, EventTenantNotification, Occurrence, PostLog, Tenant
from services.discord_api import send_channel_message

logger = logging.getLogger(__name__)


async def send_pre_event_reminders(session_factory=None):
    """
    Runs every minute. Finds occurrences whose start time falls within
    their event's notify_minutes_before window, where reminder_sent=false
    and post_status=posted. Sends a channel message and marks reminder_sent.

    For a kingdom-wide event, the reminder is sent once per tenant that
    actually has a posted PostLog row and a notification channel
    configured — the owning tenant's bare notification_channel_id/role, or
    another tenant's EventTenantNotification override (see
    routers/admin/occurrences.py._resolve_notification for the same logic
    used at posting time).

    session_factory: see module docstring — defaults to AsyncSessionLocal.
    """
    from routers.admin.deps import PLATFORM_BOT_TOKEN

    now = datetime.now(timezone.utc)

    async with (session_factory or AsyncSessionLocal)() as session:
        try:
            result = await session.execute(
                select(Occurrence, EventDefinition)
                .join(EventDefinition)
                .where(
                    Occurrence.reminder_sent == False,
                    Occurrence.post_status   == "posted",
                    EventDefinition.active   == True,
                    EventDefinition.notify_minutes_before != None,
                )
            )
            rows = result.all()

            if not rows:
                return

            for occ, event in rows:
                minutes_until = (occ.start_datetime_utc - now).total_seconds() / 60

                # Fire when within the notify window but not in the past
                if not (0 <= minutes_until <= event.notify_minutes_before):
                    continue

                # Every tenant with a posted PostLog row for this occurrence
                # gets its own reminder, in its own guild.
                log_result = await session.execute(
                    select(PostLog).where(
                        PostLog.event_id == event.id,
                        PostLog.occurrence_date == occ.occurrence_date,
                        PostLog.status == "posted",
                    )
                )
                logs = log_result.scalars().all()

                any_sent = False
                for log in logs:
                    tenant = await session.get(Tenant, log.tenant_id)
                    if tenant is None:
                        continue

                    if event.scope == "alliance" or tenant.id == event.owning_tenant_id:
                        notify_channel, notify_role = event.notification_channel_id, event.notification_role_id
                    else:
                        override_result = await session.execute(
                            select(EventTenantNotification).where(
                                EventTenantNotification.event_id == event.id,
                                EventTenantNotification.tenant_id == tenant.id,
                            )
                        )
                        override = override_result.scalar_one_or_none()
                        notify_channel = override.notification_channel_id if override else ""
                        notify_role    = override.notification_role_id if override else ""

                    if not notify_channel:
                        continue

                    token = tenant.bot_token or PLATFORM_BOT_TOKEN
                    if not token:
                        continue

                    role_mention = f"<@&{notify_role}> " if notify_role else ""
                    time_str     = occ.start_datetime_utc.strftime("%H:%M UTC")
                    date_str     = occ.occurrence_date.strftime("%a %d %b")
                    mins         = int(minutes_until)
                    time_label   = f"in {mins} minute{'s' if mins != 1 else ''}" if mins > 0 else "now"

                    message = (
                        f"{role_mention}⏰ **{event.name}** starts {time_label}\n"
                        f"📅 {date_str} · {time_str}"
                        + (f" · {event.discord_channel}" if event.discord_channel else "")
                    )

                    try:
                        success, error = await send_channel_message(token, notify_channel, message)
                    except Exception as e:
                        # A raised exception here (vs. the (success, error) tuple
                        # send_channel_message normally returns) would otherwise
                        # propagate out of the loop and roll back every
                        # reminder_sent flag set so far this tick, including ones
                        # for messages that were already delivered to Discord —
                        # those would then be re-sent on the next tick.
                        logger.error(f"Reminder failed for '{event.name}' (tenant {tenant.id}): {e}")
                        continue

                    if success:
                        any_sent = True
                        logger.info(f"Reminder sent for '{event.name}' on {occ.occurrence_date} (tenant {tenant.id})")
                    else:
                        logger.error(f"Reminder failed for '{event.name}' (tenant {tenant.id}): {error}")

                if any_sent:
                    occ.reminder_sent = True
                    # Commit immediately, per-occurrence, rather than once at
                    # the end of the loop. If something goes wrong on a later
                    # occurrence, only this one's already-committed flag is
                    # safe — a single end-of-loop commit would have let a
                    # later failure roll back every reminder_sent flag set so
                    # far, even though those messages were already delivered.
                    await session.commit()

        except Exception as e:
            await session.rollback()
            logger.error(f"send_pre_event_reminders: {e}")


