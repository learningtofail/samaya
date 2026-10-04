"""Placeholder substitution for Announcement/AnnouncementTemplate body
text (spec §27). Deliberately not Python's str.format() — a template
author's own literal `{` in Discord markdown (not that Discord markdown
uses braces, but nothing should assume it never will) would raise
KeyError instead of passing through untouched, and str.format also can't
silently ignore an unrecognized placeholder someone mistyped. A plain
regex substitution over a fixed, known set of names sidesteps both: an
unrecognized `{whatever}` is left exactly as written rather than erroring
or vanishing, which is also friendlier for a template being drafted
incrementally.

Resolution happens per-target, at delivery time (scheduler/announcements.py)
— never when a template is applied to the New Announcement modal, and
never once at creation — so a recurring announcement's later occurrences
render a fresh Discord timestamp each time, and a multi-target
announcement shows each target's own alliance name rather than whichever
tenant happened to be active in the admin UI when it was written.
"""
import re
from datetime import datetime, timedelta

PLACEHOLDER_NAMES = (
    "alliance_name", "kingdom_name",
    "send_time", "send_time_relative",
    "event_time", "event_time_relative",
)

_PLACEHOLDER_RE = re.compile(r"\{(" + "|".join(PLACEHOLDER_NAMES) + r")\}")


def discord_timestamp(dt: datetime, style: str) -> str:
    """Discord's dynamic timestamp markdown (<t:UNIX:STYLE>) — renders in
    each viewer's own local time and locale, client-side, with no app
    code involved once posted. 'F' = long date+time, 'R' = relative
    ("in 15 minutes"/"3 days ago")."""
    return f"<t:{int(dt.timestamp())}:{style}>"


def default_reminder_text(event_name: str, minutes_before: int) -> str:
    """What a reminder says when neither it nor the event has a message
    (spec §77.4): "1 hour until Bear Hunt". The largest whole unit is used, so
    90 minutes stays "90 minutes". A reminder at the start says the event is
    starting now. The name is inserted as written, not rendered as a template."""
    if minutes_before <= 0:
        return f"{event_name} is starting now"
    for size, unit in ((1440, "day"), (60, "hour"), (1, "minute")):
        if minutes_before % size == 0:
            count = minutes_before // size
            return f"{count} {unit}{'' if count == 1 else 's'} until {event_name}"
    raise AssertionError("unreachable: every positive integer is a whole number of minutes")


def render_placeholders(
    text: str,
    *,
    tenant_name: str,
    kingdom_name: str,
    scheduled_for: datetime,
    event_offset_minutes: int = 0,
) -> str:
    """Substitutes every recognized {placeholder} in `text`. Unrecognized
    `{...}` sequences are left untouched — see module docstring."""
    event_time = scheduled_for + timedelta(minutes=event_offset_minutes or 0)
    values = {
        "alliance_name":       tenant_name,
        "kingdom_name":        kingdom_name,
        "send_time":           discord_timestamp(scheduled_for, "F"),
        "send_time_relative":  discord_timestamp(scheduled_for, "R"),
        "event_time":          discord_timestamp(event_time, "F"),
        "event_time_relative": discord_timestamp(event_time, "R"),
    }
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], text)
