"""Admin router package, split by concern. Each submodule owns one slice of
the admin surface; this file wires them together under one `router`."""
from fastapi import APIRouter

from . import (
    ui, discord_config, tenants, invites, me, users, audit_log, tickets,
    unified_events, unified_engine, audiences,
)

router = APIRouter()

router.include_router(ui.router)
router.include_router(discord_config.router)
router.include_router(tenants.router)
router.include_router(invites.router)
router.include_router(me.router)
router.include_router(users.router)
router.include_router(audit_log.router)
router.include_router(tickets.router)
router.include_router(unified_events.router)
router.include_router(unified_engine.router)
router.include_router(audiences.router)
