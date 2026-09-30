"""Posting an Occurrence to Discord — the fan-out/dedup/resolution logic
shared by the manual "Post" button (routers/admin/occurrences.py), the
daily auto-post job (scheduler/auto_post.py), and the public events page's
notification-channel-name resolution (routers/events.py).

Extracted out of routers/admin/occurrences.py (audit remediation, Phase 2):
that router only ever gathered this here because _post_to_one_tenant was
written for its own /post endpoint first — its actual callers were never
router-specific (a scheduler job and a public, unauthenticated router both
needed the same functions, and got them via a local `from
routers.admin.occurrences import ...` inside a function body, purely to
dodge the circular import that a top-level import would have caused). A
service module has no router to import back from, so every one of those
call sites can now import this module normally at the top of the file.

PLATFORM_BOT_TOKEN and find_post_log moved here too, for the same reason:
neither is a FastAPI dependency (find_post_log's own docstring already
said so), and both exist purely to support posting-related logic — they
were only ever in routers/admin/deps.py because nothing else offered a
non-router home for them yet.
"""
import os
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import EventTarget, EventTenantNotification, Occurrence, PostLog, Tenant
from services.discord_api import create_discord_event, send_channel_message
from services.templates import render_placeholders
from services.time_utils import ensure_utc

# Falls back to this when a Tenant's own bot_token is null — see
# models.db.Tenant's docstring for why sharing one token is the default.
PLATFORM_BOT_TOKEN = os.environ.get("PLATFORM_BOT_TOKEN", "")


async def find_post_log(db: AsyncSession, tenant_id: int, event_name: str, occurrence_date) -> PostLog | None:
    """Not a FastAPI dependency — callers need different behavior when no
    row is found (some treat it as fine, some as a 404), so this stays a
    plain helper rather than one more Depends with baked-in error handling."""
    result = await db.execute(
        select(PostLog).where(
            PostLog.tenant_id == tenant_id,
            PostLog.event_name == event_name,
            PostLog.occurrence_date == occurrence_date,
        )
    )
    return result.scalar_one_or_none()


async def _resolve_notification(db, event, target_tenant: Tenant):
    """Which channel/role to ping for this target tenant, if any.

    The owning tenant's bare columns on EventDefinition are always the
    answer for itself. Every other target's channel/role ID belongs to a
    different guild, so it needs its own row somewhere — either:
    - models.db.EventTenantNotification, for a tenant reached via
      kingdom-wide's automatic same-Kingdom fan-out, or
    - models.db.EventTarget (spec §20), for an explicit extra destination
      independent of Kingdom membership (the HTD case: a server that
      isn't necessarily in the owning tenant's Kingdom at all).
    No row in either place for a given target = no ping for that target,
    not an error — the Discord Scheduled Event itself still gets created
    for them regardless (see _post_to_one_tenant).
    """
    if target_tenant.id == event.owning_tenant_id:
        return event.notification_channel_id, event.notification_role_id

    if event.scope == "kingdom-wide":
        result = await db.execute(
            select(EventTenantNotification).where(
                EventTenantNotification.event_id == event.id,
                EventTenantNotification.tenant_id == target_tenant.id,
            )
        )
        override = result.scalar_one_or_none()
        if override:
            return override.notification_channel_id, override.notification_role_id

    target_result = await db.execute(
        select(EventTarget).where(
            EventTarget.event_id == event.id, EventTarget.tenant_id == target_tenant.id
        )
    )
    target = target_result.scalar_one_or_none()
    if not target:
        return "", ""
    return target.notification_channel_id, target.notification_role_id


async def _post_to_one_tenant(db: AsyncSession, occ: Occurrence, event, target_tenant: Tenant) -> dict:
    """Posts one Discord Scheduled Event, in target_tenant's own guild, for
    the given occurrence — and records one PostLog row for it. Used both
    for the single-tenant alliance case and once per tenant in the
    kingdom-wide fan-out. Never raises for a single target's failure; the
    caller decides how to aggregate results across targets.

    Spec §52 — two tenants can share one DiscordServer (Tenant.server_id
    has no unique constraint), and kingdom-wide's automatic fan-out
    resolves targets by Tenant, not by DiscordServer. Without the check
    below, fanning out to two tenants on the same guild would call
    create_discord_event twice and leave two duplicate Scheduled Events
    sitting in that one guild. Instead, once any tenant has successfully
    posted an occurrence to a given guild, every other tenant sharing that
    same guild reuses the existing discord_event_id — no second Discord
    API call — while still getting its own PostLog row (so its own
    Dashboard/PostLog view shows it as posted) and its own notification
    ping if it has one configured (a shared server can still have distinct
    per-alliance channels worth pinging separately)."""
    if await find_post_log(db, target_tenant.id, event.name, occ.occurrence_date):
        return {"tenant_slug": target_tenant.slug, "status": "skipped", "detail": "Already posted"}

    token = target_tenant.server.bot_token or PLATFORM_BOT_TOKEN
    if not token:
        return {"tenant_slug": target_tenant.slug, "status": "error",
                "detail": "No Discord bot token configured for this tenant"}

    shared_result = await db.execute(
        select(PostLog).where(
            PostLog.event_id == event.id,
            PostLog.occurrence_date == occ.occurrence_date,
            PostLog.discord_guild_id == target_tenant.server.guild_id,
            PostLog.status == "posted",
        )
    )
    shared_log = shared_result.scalars().first()
    if shared_log is not None:
        return await _record_shared_guild_post(db, occ, event, target_tenant, shared_log, token)

    # Reserve the PostLog row now, before calling Discord, instead of after.
    # Two concurrent POSTs for the same occurrence could otherwise both pass
    # the check above, both create a Discord event, and only then collide on
    # uq_post_log — by which point the second request's Discord event has
    # already been created with no PostLog row to record it. Inserting the
    # reservation first means the race is caught by the unique constraint
    # before any Discord call happens.
    log = PostLog(
        tenant_id         = target_tenant.id,
        event_id          = event.id,
        event_name        = event.name,
        occurrence_date   = occ.occurrence_date,
        discord_guild_id  = target_tenant.server.guild_id,
        posted_by         = "coordinator",
        status            = "pending",
    )
    db.add(log)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        # Someone else (another tenant sharing this guild, or a concurrent
        # request for this same tenant) may have just posted — re-check
        # for a shared-guild row rather than assuming "already posted" was
        # necessarily this exact tenant's own.
        shared_result = await db.execute(
            select(PostLog).where(
                PostLog.event_id == event.id,
                PostLog.occurrence_date == occ.occurrence_date,
                PostLog.discord_guild_id == target_tenant.server.guild_id,
                PostLog.status == "posted",
            )
        )
        shared_log = shared_result.scalars().first()
        if shared_log is not None:
            return await _record_shared_guild_post(db, occ, event, target_tenant, shared_log, token)
        return {"tenant_slug": target_tenant.slug, "status": "skipped", "detail": "Already posted"}
    await db.commit()

    # spec §32 — the same six {placeholder} names Announcements support
    # (§27) now also resolve in an Event's description, wherever that
    # description reaches Discord: the Scheduled Event's own description
    # field below, and the pre-event ping's extra line further down (both
    # use this one resolved copy, computed once). "send_time" here means
    # "when this ping/event post actually went out" (now) rather than a
    # separately-scheduled send time, since an Event has no such concept
    # of its own — event_offset_minutes is computed from now to this
    # occurrence's own start, so {event_time}/{event_time_relative}
    # resolve to the occurrence's real start time either way.
    now = datetime.now(timezone.utc)
    occ_start = ensure_utc(occ.start_datetime_utc)
    offset_minutes = round((occ_start - now).total_seconds() / 60)
    resolved_description = render_placeholders(
        event.description,
        tenant_name=target_tenant.name,
        kingdom_name=target_tenant.kingdom.name,
        scheduled_for=now,
        event_offset_minutes=offset_minutes,
    ) if event.description else event.description

    discord_id, error = await create_discord_event(
        token       = token,
        guild_id    = target_tenant.server.guild_id,
        name        = event.name,
        start       = occ.start_datetime_utc,
        end         = occ.end_datetime_utc,
        description = resolved_description,
        location    = event.discord_channel,
        image       = event.cover_image_data or None,
    )

    if error:
        log.status        = "error"
        log.status_detail = error
        await db.commit()
        return {"tenant_slug": target_tenant.slug, "status": "error", "detail": error}

    log.discord_event_id = discord_id
    log.posted_at_utc    = datetime.now(timezone.utc)
    log.status           = "posted"
    await db.commit()

    await _send_pre_event_ping(db, event, occ, target_tenant, resolved_description, token, discord_id)

    return {"tenant_slug": target_tenant.slug, "status": "posted", "discord_event_id": discord_id}


async def _send_pre_event_ping(db, event, occ: Occurrence, target_tenant: Tenant, resolved_description: str, token: str, discord_id: str):
    """The pre-event channel ping, factored out of _post_to_one_tenant so
    the shared-guild reuse path (spec §52 — a tenant that reused another
    tenant's already-created Scheduled Event still wants its own
    notification pinged, since two tenants sharing one Discord server can
    still have distinct per-alliance channels) doesn't duplicate it."""
    notify_channel, notify_role = await _resolve_notification(db, event, target_tenant)
    if not (notify_channel and notify_role):
        return
    role_mention = f"<@&{notify_role}> "
    time_str     = occ.start_datetime_utc.strftime("%H:%M UTC")
    date_str     = occ.occurrence_date.strftime("%A %d %b")
    announce_msg = (
        f"{role_mention}📅 **{event.name}** has been scheduled\n"
        f"{date_str} · {time_str}"
        + (f" · {event.discord_channel}" if event.discord_channel else "")
        + (f"\n{resolved_description}" if resolved_description else "")
        + (f"\nhttps://discord.com/events/{target_tenant.server.guild_id}/{discord_id}" if discord_id else "")
        + "\n\nClick **Interested** to get a reminder 30 minutes before."
    )
    await send_channel_message(token, notify_channel, announce_msg)


async def _record_shared_guild_post(db, occ: Occurrence, event, target_tenant: Tenant, shared_log: PostLog, token: str) -> dict:
    """Records target_tenant's own PostLog row for an occurrence whose
    Discord Scheduled Event was already created for a different tenant
    sharing the same DiscordServer (spec §52) — no second
    create_discord_event call, since Discord would show that as two
    separate events in the one guild. target_tenant still gets its own
    PostLog row (so its own Dashboard/PostLog view shows this as posted)
    and its own pre-event ping if it has a notification channel/role
    configured."""
    log = PostLog(
        tenant_id         = target_tenant.id,
        event_id          = event.id,
        event_name        = event.name,
        occurrence_date   = occ.occurrence_date,
        discord_event_id  = shared_log.discord_event_id,
        discord_guild_id  = target_tenant.server.guild_id,
        posted_by         = "system (shared Discord server)",
        posted_at_utc     = datetime.now(timezone.utc),
        status            = "posted",
        status_detail     = "Shares a Discord server with another alliance already posting this occurrence — reused that Scheduled Event instead of creating a duplicate.",
    )
    db.add(log)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        return {"tenant_slug": target_tenant.slug, "status": "skipped", "detail": "Already posted"}
    await db.commit()

    now = datetime.now(timezone.utc)
    occ_start = ensure_utc(occ.start_datetime_utc)
    offset_minutes = round((occ_start - now).total_seconds() / 60)
    resolved_description = render_placeholders(
        event.description,
        tenant_name=target_tenant.name,
        kingdom_name=target_tenant.kingdom.name,
        scheduled_for=now,
        event_offset_minutes=offset_minutes,
    ) if event.description else event.description

    await _send_pre_event_ping(db, event, occ, target_tenant, resolved_description, token, shared_log.discord_event_id)

    return {"tenant_slug": target_tenant.slug, "status": "posted", "discord_event_id": shared_log.discord_event_id}


async def _resolve_post_targets(db: AsyncSession, event, owning: Tenant) -> list[Tenant]:
    """Every tenant this occurrence should be posted to: the kingdom-wide
    automatic same-Kingdom fan-out (scope == 'kingdom-wide') plus any
    explicit EventTarget rows (spec §20), deduplicated. Factored out of
    post_occurrence (spec §50) so the daily auto-post job
    (scheduler/auto_post.py) resolves targets identically to the manual
    Post button, rather than a second, potentially-drifting copy of this
    logic."""
    if event.scope == "kingdom-wide":
        kingdom_result = await db.execute(select(Tenant).where(Tenant.kingdom_id == owning.kingdom_id))
        base_targets = list(kingdom_result.scalars().all())
    else:
        base_targets = [owning]

    # Explicit extra targets (spec §20) — additive, independent of scope
    # and of Kingdom membership entirely (the HTD case). A tenant that's
    # both an automatic fan-out recipient *and* an explicit target is
    # deduplicated below rather than posted to twice.
    explicit_result = await db.execute(
        select(Tenant).join(EventTarget, EventTarget.tenant_id == Tenant.id)
        .where(EventTarget.event_id == event.id)
    )
    seen_ids = {t.id for t in base_targets}
    return base_targets + [t for t in explicit_result.scalars().all() if t.id not in seen_ids]
