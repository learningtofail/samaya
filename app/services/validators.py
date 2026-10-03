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

from services.recurrence import RECURRENCE_KINDS


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


def parse_scope(v, allow_none: bool = False):
    if v is None and allow_none:
        return v
    v = str(v).strip()
    if v not in ("alliance", "kingdom-wide"):
        raise ValueError("Scope must be 'alliance' or 'kingdom-wide'")
    return v


# Discord accepts PNG/JPEG/GIF for a Scheduled Event's cover image; this
# just checks the payload is shaped like one of those data URIs rather
# than re-deriving Discord's own actual size/format limits, which Discord
# enforces itself on the create/update call either way. ~8MB of base64
# text is a generous ceiling meant to catch a client bug (e.g. sending a
# whole video file) rather than to be the real limit — Discord's own
# response error is the source of truth for "too big"/"wrong format".
_COVER_IMAGE_RE = re.compile(r"^data:image/(png|jpeg|jpg|gif);base64,[A-Za-z0-9+/]+=*$")
_COVER_IMAGE_MAX_CHARS = 8 * 1024 * 1024


def parse_cover_image_data(v, allow_none: bool = False):
    # allow_none=True (EventPatch): an explicit null (or the field being
    # left out of the request entirely, which never reaches this
    # validator at all — see field default handling) means "leave the
    # current image alone". An explicit "" is a *different* value from
    # null and does reach here, meaning "remove the image" — the modal's
    # Remove button sends exactly that, same "empty string clears a
    # normally-non-nullable field" convention EventPatch already uses for
    # discord_channel/description, adapted for a nullable column.
    if v is None and allow_none:
        return v
    if v in (None, ""):
        return ""
    v = str(v)
    if len(v) > _COVER_IMAGE_MAX_CHARS:
        raise ValueError("Cover image is too large")
    if not _COVER_IMAGE_RE.match(v):
        raise ValueError("Cover image must be a PNG, JPEG, or GIF image")
    return v


# --- unified event model (spec §66) -----------------------------------------

_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_REMINDERS = 10
MAX_REMINDER_MINUTES = 28 * 24 * 60  # four weeks


def parse_hex_color(v, allow_none: bool = False):
    if v is None and allow_none:
        return v
    v = str(v).strip()
    if not _HEX_COLOR_RE.match(v):
        raise ValueError("Color must be a 6-digit hex value like #E65100")
    return v.upper()


_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")


def parse_slug(v, allow_none: bool = False):
    """An alliance slug appears in public URLs (/events/{slug} and /events/{slug}.ics), so it
    must be one path segment with no dot: lowercase letters, digits and hyphens, 1 to 32 characters."""
    if v is None and allow_none:
        return v
    v = str(v).strip()
    if not _SLUG_RE.match(v):
        raise ValueError("Slug must be 1 to 32 characters: lowercase letters, digits and hyphens, not starting or ending with a hyphen")
    return v


def parse_clearable_hex_color(v):
    """PATCH semantics for a nullable color: omitted/null leaves it alone, an
    empty string resets it to the built-in default, anything else must be hex."""
    if v is None or (isinstance(v, str) and v.strip() == ""):
        return v if v is None else ""
    return parse_hex_color(v)


def parse_optional_duration_hours(v, allow_none: bool = True):
    """A duration is optional in the unified model: None means 'no calendar
    entry, a plain message'. A present value must be a positive number."""
    if v is None:
        return None
    return parse_duration_hours(v)


def parse_optional_interval_days(v, allow_none: bool = True):
    if v is None:
        return None
    return parse_interval_days(v)


def parse_recurrence_kind(v, allow_none: bool = False):
    if v is None and allow_none:
        return v
    v = str(v).strip()
    if v not in RECURRENCE_KINDS:
        raise ValueError("Recurrence must be 'none' or 'interval_days'")
    return v


def parse_reminder_minutes(v, allow_none: bool = False):
    if v is None and allow_none:
        return v
    if not isinstance(v, (list, tuple)):
        raise ValueError("Reminders must be a list of minute offsets")
    out: list[int] = []
    for item in v:
        try:
            minutes = int(item)
        except (TypeError, ValueError):
            raise ValueError("Each reminder must be a whole number of minutes")
        if minutes < 0:
            raise ValueError("A reminder cannot be negative (0 means at the start)")
        if minutes > MAX_REMINDER_MINUTES:
            raise ValueError("A reminder cannot be more than 4 weeks ahead")
        out.append(minutes)
    if len(set(out)) != len(out):
        raise ValueError("Reminder offsets must be unique")
    if len(out) > MAX_REMINDERS:
        raise ValueError(f"At most {MAX_REMINDERS} reminders per event")
    return sorted(out, reverse=True)


def check_recurrence_shape(kind: str, interval_days, anchor, until):
    """Cross-field rules shared by create and the merged result of a patch.
    Raises ValueError with a message safe to show a user."""
    if kind == "none" and interval_days is not None:
        raise ValueError("A one-off event cannot have an interval")
    if kind == "interval_days" and interval_days is None:
        raise ValueError("A repeating event needs an interval in days")
    if until is not None and anchor is not None and until < anchor:
        raise ValueError("The end date cannot be before the first date")
