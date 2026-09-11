from typing import Optional

from pydantic import BaseModel, field_validator

from services.validators import (
    parse_interval_days, parse_duration_hours, parse_alliance,
    parse_start_time_utc, parse_anchor_date, parse_notify_minutes_before,
)


class EventIn(BaseModel):
    name:                    str
    interval_days:           int
    start_time_utc:          str
    duration_hours:          float
    discord_channel:         str = ""
    description:             str = ""
    category:                str = "Other"
    alliance:                str = "Server"
    leadership_only:         bool = False
    anchor_date:             str
    notification_channel_id: str = ""
    notification_role_id:    str = ""
    notify_minutes_before:   Optional[int] = None

    _validate_interval = field_validator("interval_days", mode="before")(parse_interval_days)
    _validate_duration = field_validator("duration_hours", mode="before")(parse_duration_hours)
    _validate_alliance = field_validator("alliance", mode="before")(parse_alliance)
    _validate_time     = field_validator("start_time_utc", mode="before")(parse_start_time_utc)
    _validate_anchor   = field_validator("anchor_date", mode="before")(parse_anchor_date)
    _validate_notify   = field_validator("notify_minutes_before", mode="before")(parse_notify_minutes_before)


class EventPatch(BaseModel):
    name:                    Optional[str]  = None
    interval_days:           Optional[int]  = None
    start_time_utc:          Optional[str]  = None
    duration_hours:          Optional[float]= None
    discord_channel:         Optional[str]  = None
    description:             Optional[str]  = None
    category:                Optional[str]  = None
    alliance:                Optional[str]  = None
    leadership_only:         Optional[bool] = None
    active:                  Optional[bool] = None
    anchor_date:             Optional[str]  = None
    notification_channel_id: Optional[str]  = None
    notification_role_id:    Optional[str]  = None
    notify_minutes_before:   Optional[int]  = None

    # Same parsing rules as EventIn, but None means "leave this field
    # unchanged" on a partial update rather than "invalid".
    _validate_interval = field_validator("interval_days", mode="before")(
        lambda cls, v: parse_interval_days(v, allow_none=True))
    _validate_duration = field_validator("duration_hours", mode="before")(
        lambda cls, v: parse_duration_hours(v, allow_none=True))
    _validate_alliance = field_validator("alliance", mode="before")(
        lambda cls, v: parse_alliance(v, allow_none=True))
    _validate_time     = field_validator("start_time_utc", mode="before")(
        lambda cls, v: parse_start_time_utc(v, allow_none=True))
    _validate_anchor   = field_validator("anchor_date", mode="before")(
        lambda cls, v: parse_anchor_date(v, allow_none=True))
    _validate_notify   = field_validator("notify_minutes_before", mode="before")(
        lambda cls, v: parse_notify_minutes_before(v, allow_none=True))

class DiscordConfigIn(BaseModel):
    bot_token:  str
    guild_id:   str
    public_key: str

class OccurrencePatch(BaseModel):
    post_to_discord: Optional[bool] = None
    post_status:     Optional[str]  = None
