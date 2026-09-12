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
        "name":                     e.name,
        "interval_days":            e.interval_days,
        "start_time_utc":           e.start_time_utc.strftime("%H:%M"),
        "duration_hours":           float(e.duration_hours),
        "discord_channel":          e.discord_channel,
        "description":              e.description,
        "category":                 e.category,
        "alliance":                 e.alliance,
        "leadership_only":          e.leadership_only,
        "active":                   e.active,
        "anchor_date":              str(e.anchor_date),
        "notification_channel_id":  e.notification_channel_id,
        "notification_role_id":     e.notification_role_id,
        "notify_minutes_before":    e.notify_minutes_before,
    }

def _occurrence_dict(occ: Occurrence, ev: EventDefinition) -> dict:
    return {
        "id":                 occ.id,
        "event_id":           occ.event_id,
        "event_name":         ev.name,
        "category":           ev.category,
        "occurrence_date":    str(occ.occurrence_date),
        "start_datetime_utc": occ.start_datetime_utc.isoformat(),
        "end_datetime_utc":   occ.end_datetime_utc.isoformat(),
        "post_to_discord":    occ.post_to_discord,
        "post_status":        occ.post_status,
        "status_detail":      occ.status_detail,
        "discord_channel":    ev.discord_channel,
        "duration_hours":     float(ev.duration_hours),
        "reminder_sent":      occ.reminder_sent,
        "alliance":           ev.alliance,
        "leadership_only":    ev.leadership_only,
    }

def _log_dict(l: PostLog, ev: EventDefinition | None = None) -> dict:
    today = date.today()
    diff  = (l.occurrence_date - today).days
    timing = "Today" if diff == 0 else (f"In {diff} days" if diff > 0 else f"{abs(diff)} days ago")
    return {
        "id":               l.id,
        "event_name":       l.event_name,
        "occurrence_date":  str(l.occurrence_date),
        "discord_event_id": l.discord_event_id,
        "posted_at_utc":    l.posted_at_utc.isoformat() if l.posted_at_utc else None,
        "posted_by":        l.posted_by,
        "status":           l.status,
        "status_detail":    l.status_detail,
        "timing":           timing,
        "alliance":         ev.alliance if ev else None,
        "leadership_only":  ev.leadership_only if ev else False,
    }
