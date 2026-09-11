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
from models.db import Base, SchedulerState
from models import AsyncSessionLocal
from scheduler.jobs import regenerate_occurrences, send_pre_event_reminders
from routers import events, admin, webhooks, ics

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")

# Admin routes previously had no application-level auth and relied
# entirely on Cloudflare Access at the network edge. ADMIN_API_KEY adds
# a second, independent layer (see services/auth.py). Refuse to boot
# without it rather than silently running the admin API open.
if not os.environ.get("ADMIN_API_KEY"):
    sys.exit(
        "ADMIN_API_KEY is not set. Generate one (e.g. `openssl rand -hex 32`) "
        "and add it to .env before starting the app."
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as session:
        from sqlalchemy.dialects.postgresql import insert
        for job in ("regenerate_occurrences", "pre_event_notifier"):
            await session.execute(
                insert(SchedulerState)
                .values(job_name=job)
                .on_conflict_do_nothing()
            )
        await session.commit()

    # Check if regeneration is overdue (>25h since last run)
    async with AsyncSessionLocal() as session:
        from sqlalchemy import select
        row = await session.execute(
            select(SchedulerState).where(SchedulerState.job_name == "regenerate_occurrences")
        )
        state = row.scalar_one_or_none()
        if state is None or state.last_run_utc is None or \
           (datetime.now(timezone.utc) - state.last_run_utc).total_seconds() > 90000:
            logger.info("Overdue regeneration detected on startup — running now")
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

    scheduler.start()
    logger.info("Samaya scheduler started — daily regen at UTC 00:00, reminders every minute")

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


@app.get("/health")
async def health():
    return {"status": "ok", "service": "samaya"}
