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

from models import engine
from models.db import Base
from models import AsyncSessionLocal
from scheduler.regeneration import regenerate_occurrences
from scheduler.reminders import send_pre_event_reminders
from scheduler.announcements import send_scheduled_announcements
from routers import events, admin, webhooks, ics, auth as auth_router, auth_pages

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
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

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
               (datetime.now(timezone.utc) - state.last_run_utc).total_seconds() > 90000:
                overdue = True
                break

        if overdue:
            logger.info("Overdue regeneration detected on startup — running now for all tenants")
            await regenerate_occurrences()

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

    scheduler.start()
    logger.info("Samaya scheduler started — daily regen at UTC 00:00, reminders and announcements every minute")

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


@app.get("/health")
async def health():
    return {"status": "ok", "service": "samaya"}
