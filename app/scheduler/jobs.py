import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert

from models import AsyncSessionLocal
from models.db import (
    DiscordConfig, EventDefinition, Occurrence,
    PostLog, SchedulerState
)
from services.recurrence import (
    build_start_datetime, normalise_anchor, occurrences_in_window
)
from services.notifications import send_notification

logger = logging.getLogger(__name__)

WINDOW_DAYS = 28


async def regenerate_occurrences():
    """
    Daily job — regenerates the 28-day occurrence window.
    Runs at UTC 00:00. Normalises anchor dates, preserves
    existing post_to_discord checkbox states across rebuilds.
    """
    logger.info("regenerate_occurrences: starting")
    today = date.today()
    started_at = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as session:
        try:
            # Load all active event definitions
            result = await session.execute(
                select(EventDefinition).where(EventDefinition.active == True)
            )
            events = result.scalars().all()

            if not events:
                logger.warning("regenerate_occurrences: no active events found")
                await _update_state(session, "regenerate_occurrences", "success", "0 events active")
                return

            # Preserve existing post_to_discord states before deletion
            existing = await session.execute(
                select(Occurrence.event_id, Occurrence.occurrence_date, Occurrence.post_to_discord)
                .where(Occurrence.post_to_discord == True)
            )
            checked = {(r.event_id, r.occurrence_date) for r in existing}

            # Remove stale occurrences (older than 2 days)
            await session.execute(
                delete(Occurrence).where(
                    Occurrence.window_generated_at < datetime.now(timezone.utc) - timedelta(days=2)
                )
            )

            # Generate new occurrences for each event
            inserted = 0
            for event in events:
                # Normalise anchor date
                new_anchor = normalise_anchor(event.anchor_date, event.interval_days, today)
                if new_anchor != event.anchor_date:
                    event.anchor_date = new_anchor
                    logger.info(f"Anchor normalised for '{event.name}': {new_anchor}")

                for occ_date in occurrences_in_window(new_anchor, event.interval_days, today, WINDOW_DAYS):
                    start_dt = build_start_datetime(occ_date, event.start_time_utc)
                    end_dt   = start_dt + timedelta(hours=float(event.duration_hours))
                    checked_state = (event.id, occ_date) in checked

                    stmt = insert(Occurrence).values(
                        event_id            = event.id,
                        occurrence_date     = occ_date,
                        start_datetime_utc  = start_dt,
                        end_datetime_utc    = end_dt,
                        post_to_discord     = checked_state,
                        post_status         = "pending",
                        window_generated_at = datetime.now(timezone.utc),
                    ).on_conflict_do_update(
                        index_elements=["event_id", "occurrence_date"],
                        set_={
                            "start_datetime_utc":  start_dt,
                            "end_datetime_utc":    end_dt,
                            "window_generated_at": datetime.now(timezone.utc),
                            # preserve post_to_discord and post_status if already set
                        }
                    )
                    await session.execute(stmt)
                    inserted += 1

            await session.commit()
            detail = f"{len(events)} events, {inserted} occurrences generated"
            logger.info(f"regenerate_occurrences: complete — {detail}")
            await _update_state(session, "regenerate_occurrences", "success", detail)

        except Exception as e:
            await session.rollback()
            logger.error(f"regenerate_occurrences: failed — {e}")
            await _update_state(session, "regenerate_occurrences", "error", str(e))
            send_notification(
                subject="[Samaya] Occurrence regeneration failed",
                body=f"The daily regeneration job failed at {started_at.isoformat()}.\n\nError: {e}"
            )


async def _update_state(session, job_name: str, result: str, detail: str):
    """Updates scheduler_state for a given job."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    stmt = pg_insert(SchedulerState).values(
        job_name     = job_name,
        last_run_utc = datetime.now(timezone.utc),
        last_result  = result,
        last_detail  = detail,
    ).on_conflict_do_update(
        index_elements=["job_name"],
        set_={
            "last_run_utc": datetime.now(timezone.utc),
            "last_result":  result,
            "last_detail":  detail,
        }
    )
    await session.execute(stmt)
    await session.commit()


async def send_pre_event_reminders():
    """
    Runs every minute. Finds occurrences whose start time falls within
    their event's notify_minutes_before window, where reminder_sent=false
    and post_status=posted. Sends a channel message and marks reminder_sent.
    """
    from services.discord_api import send_channel_message

    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as session:
        try:
            result = await session.execute(
                select(Occurrence, EventDefinition)
                .join(EventDefinition)
                .where(
                    Occurrence.reminder_sent == False,
                    Occurrence.post_status   == "posted",
                    EventDefinition.active   == True,
                    EventDefinition.notify_minutes_before != None,
                    EventDefinition.notification_channel_id != "",
                )
            )
            rows = result.all()

            if not rows:
                return

            cfg_result = await session.execute(select(DiscordConfig))
            cfg = cfg_result.scalar_one_or_none()
            if not cfg:
                return

            for occ, event in rows:
                minutes_until = (occ.start_datetime_utc - now).total_seconds() / 60

                # Fire when within the notify window but not in the past
                if not (0 <= minutes_until <= event.notify_minutes_before):
                    continue

                # Build notification message
                role_mention = f"<@&{event.notification_role_id}> " if event.notification_role_id else ""
                time_str     = occ.start_datetime_utc.strftime("%H:%M UTC")
                date_str     = occ.occurrence_date.strftime("%a %d %b")
                mins         = int(minutes_until)
                time_label   = f"in {mins} minute{'s' if mins != 1 else ''}" if mins > 0 else "now"

                message = (
                    f"{role_mention}⏰ **{event.name}** starts {time_label}\n"
                    f"📅 {date_str} · {time_str}"
                    + (f" · {event.discord_channel}" if event.discord_channel else "")
                )

                success, error = await send_channel_message(
                    cfg.bot_token,
                    event.notification_channel_id,
                    message,
                )

                if success:
                    occ.reminder_sent = True
                    logger.info(f"Reminder sent for '{event.name}' on {occ.occurrence_date}")
                else:
                    logger.error(f"Reminder failed for '{event.name}': {error}")

            await session.commit()

        except Exception as e:
            await session.rollback()
            logger.error(f"send_pre_event_reminders: {e}")
