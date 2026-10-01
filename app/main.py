import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from models import AsyncSessionLocal
from scheduler.regeneration import regenerate_occurrences
from scheduler.reminders import send_pre_event_reminders
from scheduler.announcements import send_scheduled_announcements
from scheduler.auto_post import auto_post_upcoming_occurrences
from scheduler.unified_jobs import delivery_tick_job, generation_job
from routers.admin.unified_engine import engine_enabled
from routers import events, admin, webhooks, ics, auth as auth_router, auth_pages, tickets_public

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")

# Phase 4 replaced the shared X-Admin-Key bridge with real per-user
# sessions signed by SECRET_KEY (see services/sessions.py) — refuse to
# boot on the insecure dev-only fallback key, the same fail-closed
# posture ADMIN_API_KEY used before this.
if not os.environ.get("SECRET_KEY"):
    sys.exit(
        "SECRET_KEY is not set. Generate one (e.g. `openssl rand -hex 32`) "
        "and add it to .env before starting the app — sessions are signed "
        "with this key, and the insecure dev-only fallback in "
        "services/sessions.py must never be used in production."
    )
if not os.environ.get("DISCORD_OAUTH_CLIENT_ID") or not os.environ.get("DISCORD_OAUTH_CLIENT_SECRET"):
    sys.exit(
        "DISCORD_OAUTH_CLIENT_ID / DISCORD_OAUTH_CLIENT_SECRET are not set. "
        "Register an application at https://discord.com/developers/applications "
        "and add its credentials to .env before starting the app."
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Schema creation/changes are Alembic's job now (see alembic/versions/
    # 6fc935931248_baseline_schema_as_of_spec_62.py) — `alembic upgrade
    # head` runs as its own deploy step before the app starts, not here.
    # This used to call Base.metadata.create_all() on every boot, which
    # only ever creates a table that's entirely missing; it silently did
    # nothing for every ALTER-shaped schema change (a new column, a widened
    # CHECK constraint), which is why every one of those needed its own
    # hand-run migrate_*.py script in the first place. Removing it doesn't
    # change what already exists in production — it just stops boot from
    # quietly papering over a schema that's actually out of date.
    #
    # tests/conftest.py still calls Base.metadata.create_all() directly
    # against its own in-memory SQLite engine — that's independent of this
    # lifespan and unaffected by this change.

    # SchedulerState rows are now per-tenant (tenant_id, job_name), created
    # lazily by scheduler.jobs._update_state on each tenant's first run —
    # there's no fixed set of rows to pre-seed the way there was with one
    # global row per job, since tenants can be added at any time via the
    # tenants admin API.

    # Check if regeneration is overdue (>25h since last run) for any
    # existing tenant, or missing entirely (a tenant with no state row
    # yet). Runs for every tenant if so — cheap and idempotent, and
    # simpler than tracking per-tenant overdue-ness separately at startup.
    async with AsyncSessionLocal() as session:
        from sqlalchemy import select
        from models.db import SchedulerState, Tenant
        from services.time_utils import ensure_utc
        tenants_result = await session.execute(select(Tenant))
        tenant_ids = [t.id for t in tenants_result.scalars().all()]

        overdue = False
        for tid in tenant_ids:
            row = await session.execute(
                select(SchedulerState).where(
                    SchedulerState.tenant_id == tid,
                    SchedulerState.job_name == "regenerate_occurrences",
                )
            )
            state = row.scalar_one_or_none()
            if state is None or state.last_run_utc is None or \
               (datetime.now(timezone.utc) - ensure_utc(state.last_run_utc)).total_seconds() > 90000:
                overdue = True
                break

        if overdue:
            logger.info("Overdue regeneration detected on startup — running now for all tenants")
            await regenerate_occurrences()

    if engine_enabled():
        # Spec §66.4: the unified engine replaces all four old jobs. They must
        # not run alongside it, or every event would be posted twice.
        scheduler.add_job(
            generation_job, CronTrigger(hour=0, minute=0, timezone="UTC"),
            id="unified_generation", replace_existing=True,
        )
        scheduler.add_job(
            delivery_tick_job, IntervalTrigger(minutes=1),
            id="unified_delivery_tick", replace_existing=True,
        )
        await generation_job()  # startup catch-up; idempotent
        logger.info("Unified event engine enabled (SAMAYA_UNIFIED_ENGINE): legacy jobs not registered")
    else:
        # Daily regeneration at UTC 00:00
        scheduler.add_job(
            regenerate_occurrences,
            CronTrigger(hour=0, minute=0, timezone="UTC"),
            id="regenerate_occurrences",
            replace_existing=True,
        )

        # Pre-event reminder — runs every minute
        scheduler.add_job(
            send_pre_event_reminders,
            IntervalTrigger(minutes=1),
            id="pre_event_notifier",
            replace_existing=True,
        )

        # Scheduled announcement delivery — runs every minute
        scheduler.add_job(
            send_scheduled_announcements,
            IntervalTrigger(minutes=1),
            id="announcement_delivery",
            replace_existing=True,
        )

        # Daily auto-post of upcoming occurrences (spec §51) — deliberately
        # after regeneration's own UTC 00:00 slot (16:00 UTC), so a fresh
        # day's regenerated occurrences are always in place before this job
        # looks for anything to post.
        scheduler.add_job(
            auto_post_upcoming_occurrences,
            CronTrigger(hour=16, minute=0, timezone="UTC"),
            id="auto_post_upcoming_occurrences",
            replace_existing=True,
        )

    scheduler.start()
    logger.info(
        "Samaya scheduler started — daily regen at UTC 00:00, daily auto-post at UTC 16:00, "
        "reminders and announcements every minute"
    )

    yield

    scheduler.shutdown()
    logger.info("Samaya scheduler stopped")


app = FastAPI(
    title="Samaya — Kingshot Community Event Scheduler",
    version="1.0.0",
    lifespan=lifespan,
)

@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    errors = []
    for err in exc.errors():
        field = " → ".join(str(e) for e in err["loc"] if e != "body")
        errors.append(f"{field}: {err['msg']}" if field else err["msg"])
    return JSONResponse(
        status_code=422,
        content={"detail": " | ".join(errors)},
    )

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(events.router)
app.include_router(admin.router, prefix="/admin")
app.include_router(webhooks.router, prefix="/webhooks")
app.include_router(ics.router)
app.include_router(auth_router.router)
app.include_router(auth_pages.router)
app.include_router(tickets_public.router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "samaya"}
