"""Daily occurrence regeneration — one of the three jobs previously
bundled into scheduler/jobs.py before it grew to three genuinely
separate concerns (regeneration, reminders, announcements) each with
their own trigger in main.py's scheduler setup; split the same way
admin.py was split in Phase 1, for the same reason.

Every public function here takes an optional session_factory, defaulting
to models.AsyncSessionLocal — see the "session_factory" note in each
docstring below for why (these jobs run outside any HTTP request, so
can't use FastAPI's get_db dependency the way routers do).
"""
import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from models import AsyncSessionLocal
from models.db import EventDefinition, Occurrence, SchedulerState, Tenant
from services.recurrence import (
    build_start_datetime, normalise_anchor, occurrences_in_window
)
from services.notifications import send_notification

logger = logging.getLogger(__name__)

WINDOW_DAYS = 28


async def regenerate_occurrences(tenant_id: int | None = None, session_factory=None):
    """
    Daily job — regenerates the 28-day occurrence window for every tenant
    (or just one, when called from the manual "regenerate" button —
    routers/admin/scheduler_control.py passes tenant_id then). Runs at UTC
    00:00. Normalises anchor dates, preserves existing post_to_discord
    checkbox states across rebuilds.

    One tenant's failure doesn't stop the others — each tenant gets its
    own try/except and its own SchedulerState row, exactly like each
    tenant's Discord posting is independent (see routers/admin/occurrences.py).

    session_factory: see module docstring — defaults to AsyncSessionLocal.
    """
    logger.info("regenerate_occurrences: starting")

    async with (session_factory or AsyncSessionLocal)() as session:
        if tenant_id is not None:
            tenants = [await session.get(Tenant, tenant_id)]
        else:
            result = await session.execute(select(Tenant))
            tenants = result.scalars().all()

    for tenant in tenants:
        if tenant is None:
            continue
        await _regenerate_for_tenant(tenant.id, session_factory=session_factory)

    logger.info("regenerate_occurrences: complete for all tenants")


async def _regenerate_for_tenant(tenant_id: int, session_factory=None):
    """The actual regeneration work for one tenant — split out from
    regenerate_occurrences so a single tenant's failure (caught here,
    per-tenant) doesn't need its own try/except duplicated at the call
    site for every tenant in the loop."""
    logger.info(f"regenerate_occurrences: starting for tenant {tenant_id}")
    today = date.today()
    started_at = datetime.now(timezone.utc)

    async with (session_factory or AsyncSessionLocal)() as session:
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


