import logging
import os
import sys
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from scheduler.unified_jobs import delivery_tick_job, generation_job, giftcode_tick_job
from services import giftcode_client
from routers import events, admin, webhooks, ics, auth as auth_router, auth_pages, tickets_public, themes_public

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

    # Spec §66.4: one daily generation pass (idempotent, so also run once at
    # startup to cover a restart that skipped the 00:00 slot) and one
    # per-minute delivery tick.
    scheduler.add_job(
        generation_job, CronTrigger(hour=0, minute=0, timezone="UTC"),
        id="unified_generation", replace_existing=True,
    )
    scheduler.add_job(
        delivery_tick_job, IntervalTrigger(minutes=1),
        id="unified_delivery_tick", replace_existing=True,
    )
    if giftcode_client.is_configured():
        # Spec §79.5: off until KS_GIFTCODE_SIGN_KEY is set.
        scheduler.add_job(
            giftcode_tick_job, IntervalTrigger(minutes=1), id="giftcode_tick", replace_existing=True,
            max_instances=1, coalesce=True,
        )
    await generation_job()

    scheduler.start()
    logger.info("Samaya scheduler started: daily generation at UTC 00:00, delivery tick every minute")

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

# ics before events: /events/{slug}.ics must be matched before the page route /events/{slug}.
app.include_router(ics.router)
app.include_router(events.router)
app.include_router(admin.router, prefix="/admin")
app.include_router(webhooks.router, prefix="/webhooks")
app.include_router(auth_router.router)
app.include_router(auth_pages.router)
app.include_router(tickets_public.router)
app.include_router(themes_public.router)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "samaya"}
