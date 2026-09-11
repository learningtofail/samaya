"""
Shared Pydantic field-parsing rules for event create/update payloads.

EventIn and EventPatch previously each defined their own copy of every
@field_validator, differing only in a `if v is None: return v` guard for
EventPatch (where None means "leave this field unchanged" on a partial
update). That's the only real difference, so it's pulled out here as an
`allow_none` flag instead of five duplicated validator bodies per schema.
"""
import re
from datetime import date as _date

ALLOWED_ALLIANCES = ("M0D", "NSR", "Server")


def parse_interval_days(v, allow_none: bool = False):
    if v is None and allow_none:
        return v
    try:
        val = int(v)
    except (TypeError, ValueError):
        raise ValueError("Interval must be a whole number")
    if val < 1:
        raise ValueError(
            "Interval must be at least 1 day (1=daily, 7=weekly, 14=biweekly, 28=every 4 weeks)"
        )
    return val


def parse_duration_hours(v, allow_none: bool = False):
    if v is None and allow_none:
        return v
    try:
        val = float(v)
    except (TypeError, ValueError):
        raise ValueError("Duration must be a number")
    if val <= 0:
        raise ValueError("Duration must be greater than 0 hours")
    return val


def parse_alliance(v, allow_none: bool = False):
    if v is None and allow_none:
        return v
    v = str(v).strip()
    if v not in ALLOWED_ALLIANCES:
        raise ValueError(f"Alliance must be one of: {', '.join(ALLOWED_ALLIANCES)}")
    return v


def parse_start_time_utc(v, allow_none: bool = False):
    if v is None and allow_none:
        return v
    v = str(v).strip()
    if not re.match(r"^\d{1,2}:\d{2}$", v):
        raise ValueError("Start time must be in HH:MM format (e.g. 19:00)")
    h, m = map(int, v.split(":"))
    if not (0 <= h <= 23):
        raise ValueError(f"Hour {h} is invalid — must be 00 to 23")
    if not (0 <= m <= 59):
        raise ValueError(f"Minute {m} is invalid — must be 00 to 59")
    return f"{h:02d}:{m:02d}"


def parse_anchor_date(v, allow_none: bool = False):
    if v is None and allow_none:
        return v
    try:
        _date.fromisoformat(str(v))
    except (ValueError, TypeError):
        raise ValueError("Anchor date must be in yyyy-mm-dd format (e.g. 2025-05-01)")
    return str(v)


def parse_notify_minutes_before(v, allow_none: bool = False):
    # allow_none is accepted for a consistent call signature across all six
    # parsers, but notify_minutes_before is already Optional on both schemas
    # and "", "null" and None all mean the same thing here regardless.
    if v is None or v == "" or v == "null":
        return None
    try:
        val = int(v)
    except (TypeError, ValueError):
        raise ValueError("Notify minutes must be a whole number")
    if val < 1:
        raise ValueError("Notify minutes must be at least 1")
    return val
