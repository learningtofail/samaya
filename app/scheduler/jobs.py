import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert

from models import AsyncSessionLocal
from models.db import (
    EventDefinition, EventTenantNotification, Occurrence,
    PostLog, SchedulerState, Tenant
)
from services.recurrence import (
    build_start_datetime, normalise_anchor, occurrences_in_window
)
from services.notifications import send_notification

logger = logging.getLogger(__name__)

WINDOW_DAYS = 28


async def regenerate_occurrences(tenant_id: int | None = None):
    """
    Daily job — regenerates the 28-day occurrence window for every tenant
    (or just one, when called from the manual "regenerate" button —
    routers/admin/scheduler_control.py passes tenant_id then). Runs at UTC
    00:00. Normalises anchor dates, preserves existing post_to_discord
    checkbox states across rebuilds.

    One tenant's failure doesn't stop the others — each tenant gets its
    own try/except and its own SchedulerState row, exactly like each
    tenant's Discord posting is independent (see routers/admin/occurrences.py).
    """
    logger.info("regenerate_occurrences: starting")

    async with AsyncSessionLocal() as session:
        if tenant_id is not None:
            tenants = [await session.get(Tenant, tenant_id)]
        else:
            result = await session.execute(select(Tenant))
            tenants = result.scalars().all()

    for tenant in tenants:
        if tenant is None:
            continue
        await _regenerate_for_tenant(tenant.id)

    logger.info("regenerate_occurrences: complete for all tenants")


async def _regenerate_for_tenant(tenant_id: int):
    logger.info(f"regenerate_occurrences: starting for tenant {tenant_id}")
    today = date.today()
    started_at = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as session:
        try:
            # Load this tenant's own active event definitions. A
            # kingdom-wide event's Occurrence rows still belong to its
            # owning tenant only — fan-out happens at posting time
            # (routers/admin/occurrences.py), not at generation time.
            result = await session.execute(
                select(EventDefinition).where(
                    EventDefinition.active == True,
                    EventDefinition.owning_tenant_id == tenant_id,
                )
            )
            events = result.scalars().all()

            if not events:
                await _update_state(session, tenant_id, "regenerate_occurrences", "success", "0 events active")
                return

            # Preserve existing post_to_discord states before deletion
            existing = await session.execute(
                select(Occurrence.event_id, Occurrence.occurrence_date, Occurrence.post_to_discord)
                .where(Occurrence.tenant_id == tenant_id, Occurrence.post_to_discord == True)
            )
            checked = {(r.event_id, r.occurrence_date) for r in existing}

            # Remove stale occurrences (older than 2 days) for this tenant
            await session.execute(
                delete(Occurrence).where(
                    Occurrence.tenant_id == tenant_id,
                    Occurrence.window_generated_at < datetime.now(timezone.utc) - timedelta(days=2),
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
                        tenant_id           = tenant_id,
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
            logger.info(f"regenerate_occurrences: tenant {tenant_id} complete — {detail}")
            await _update_state(session, tenant_id, "regenerate_occurrences", "success", detail)

        except Exception as e:
            await session.rollback()
            logger.error(f"regenerate_occurrences: tenant {tenant_id} failed — {e}")
            await _update_state(session, tenant_id, "regenerate_occurrences", "error", str(e))
            await send_notification(
                subject="[Samaya] Occurrence regeneration failed",
                body=f"The daily regeneration job failed for tenant {tenant_id} at {started_at.isoformat()}.\n\nError: {e}"
            )


async def _update_state(session, tenant_id: int, job_name: str, result: str, detail: str):
    """Updates scheduler_state for a given tenant + job."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    stmt = pg_insert(SchedulerState).values(
        tenant_id    = tenant_id,
        job_name     = job_name,
        last_run_utc = datetime.now(timezone.utc),
        last_result  = result,
        last_detail  = detail,
    ).on_conflict_do_update(
        index_elements=["tenant_id", "job_name"],
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

    For a kingdom-wide event, the reminder is sent once per tenant that
    actually has a posted PostLog row and a notification channel
    configured — the owning tenant's bare notification_channel_id/role, or
    another tenant's EventTenantNotification override (see
    routers/admin/occurrences.py._resolve_notification for the same logic
    used at posting time).
    """
    from services.discord_api import send_channel_message
    from routers.admin.deps import PLATFORM_BOT_TOKEN

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
