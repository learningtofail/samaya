"""Response-shaping helpers shared across the admin routers.

Each admin.<domain> module returns these dicts rather than the raw
SQLAlchemy model, so a field rename or format change (e.g. how dates are
stringified) happens once here instead of at every call site.
"""
from datetime import date

from models.db import EventDefinition, Occurrence, PostLog


def _event_dict(e: EventDefinition) -> dict:
    return {
        "id":                       e.id,
        "owning_tenant_id":         e.owning_tenant_id,
        "scope":                    e.scope,
        "name":                     e.name,
        "interval_days":            e.interval_days,
        "start_time_utc":           e.start_time_utc.strftime("%H:%M"),
        "duration_hours":           float(e.duration_hours),
        "discord_channel":          e.discord_channel,
        "description":              e.description,
        "leadership_only":          e.leadership_only,
        "active":                   e.active,
        "anchor_date":              str(e.anchor_date),
        "notification_channel_id":  e.notification_channel_id,
        "notification_role_id":     e.notification_role_id,
        "notify_minutes_before":    e.notify_minutes_before,
        "cover_image_data":         e.cover_image_data or None,
        # Requires e.targets to already be loaded (selectinload in
        # list_events, or populated in-memory by create/update_event) —
        # accessing an unloaded relationship here would raise under async
        # SQLAlchemy rather than silently lazy-loading.
        "targets": [
            {
                "tenant_id":               t.tenant_id,
                "notification_channel_id": t.notification_channel_id,
                "notification_role_id":    t.notification_role_id,
            }
            for t in e.targets
        ],
    }

def _occurrence_dict(occ: Occurrence, ev: EventDefinition) -> dict:
    return {
        "id":                 occ.id,
        "event_id":           occ.event_id,
        "event_name":         ev.name,
        "owning_tenant_id":   ev.owning_tenant_id,
        "scope":              ev.scope,
        "occurrence_date":    str(occ.occurrence_date),
        "start_datetime_utc": occ.start_datetime_utc.isoformat(),
        "end_datetime_utc":   occ.end_datetime_utc.isoformat(),
        "post_to_discord":    occ.post_to_discord,
        "post_status":        occ.post_status,
        "status_detail":      occ.status_detail,
        "discord_channel":    ev.discord_channel,
        "duration_hours":     float(ev.duration_hours),
        "reminder_sent":      occ.reminder_sent,
        "leadership_only":    ev.leadership_only,
        # Spec §44 — the admin-side "preview as it would look on Discord"
        # modal (Dashboard/Schedule) needs the same two fields
        # occurrences.py's own posting path already sends to Discord
        # (description, resolved through the same placeholder set at
        # preview time; cover_image_data verbatim) — previously omitted
        # here since nothing read them off an Occurrence before.
        "description":        ev.description,
        "cover_image_data":   ev.cover_image_data or None,
    }

def _log_dict(log: PostLog, ev: EventDefinition | None = None) -> dict:
    today = date.today()
    diff  = (log.occurrence_date - today).days
    timing = "Today" if diff == 0 else (f"In {diff} days" if diff > 0 else f"{abs(diff)} days ago")
    return {
        "id":               log.id,
        "tenant_id":        log.tenant_id,
        "event_name":       log.event_name,
        "occurrence_date":  str(log.occurrence_date),
        "discord_event_id": log.discord_event_id,
        "posted_at_utc":    log.posted_at_utc.isoformat() if log.posted_at_utc else None,
        "posted_by":        log.posted_by,
        "status":           log.status,
        "status_detail":    log.status_detail,
        "timing":           timing,
        "scope":            ev.scope if ev else None,
        "leadership_only":  ev.leadership_only if ev else False,
    }
