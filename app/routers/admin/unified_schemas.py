"""Pydantic request models for the unified event model (spec §66): event
types and events.

For create, a field the client leaves out falls back to the event type's
default (duration, recurrence, message, reminders, role mention), so these
models rely on `model_fields_set` to tell "omitted" from "explicitly null".
For patch, only fields present in the request are applied.
"""
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from services.validators import (
    parse_anchor_date, parse_cover_image_data, parse_hex_color,
    parse_optional_duration_hours, parse_optional_interval_days,
    parse_recurrence_kind, parse_reminder_messages, parse_reminder_minutes, parse_scope, parse_start_time_utc,
)

MAX_MESSAGE_CHARS = 2000  # Discord's own limit for a channel message


class EventTypeIn(BaseModel):
    name:                     str = Field(min_length=1, max_length=60)
    color:                    str = "#475569"
    default_duration_hours:   Optional[float] = None
    default_interval_days:    Optional[int] = None
    default_message:          str = Field(default="", max_length=MAX_MESSAGE_CHARS)
    default_reminder_minutes: list[int] = []
    default_mention_role:     bool = False
    sort_order:               int = 0

    _validate_color = field_validator("color", mode="before")(parse_hex_color)
    _validate_duration = field_validator("default_duration_hours", mode="before")(parse_optional_duration_hours)
    _validate_interval = field_validator("default_interval_days", mode="before")(parse_optional_interval_days)
    _validate_reminders = field_validator("default_reminder_minutes", mode="before")(parse_reminder_minutes)

    @field_validator("name", mode="after")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Name cannot be blank")
        return v


class EventTypePatch(BaseModel):
    name:                     Optional[str] = Field(default=None, min_length=1, max_length=60)
    color:                    Optional[str] = None
    default_duration_hours:   Optional[float] = None
    default_interval_days:    Optional[int] = None
    default_message:          Optional[str] = Field(default=None, max_length=MAX_MESSAGE_CHARS)
    default_reminder_minutes: Optional[list[int]] = None
    default_mention_role:     Optional[bool] = None
    sort_order:               Optional[int] = None

    _validate_color = field_validator("color", mode="before")(
        lambda cls, v: parse_hex_color(v, allow_none=True))
    _validate_duration = field_validator("default_duration_hours", mode="before")(parse_optional_duration_hours)
    _validate_interval = field_validator("default_interval_days", mode="before")(parse_optional_interval_days)
    _validate_reminders = field_validator("default_reminder_minutes", mode="before")(
        lambda cls, v: parse_reminder_minutes(v, allow_none=True))


class EventAllianceIn(BaseModel):
    """One audience row. The optional override is the alliance's own wording:
    left out or null means "use the event's message" (spec §67.4). Where the
    message goes is chosen with audiences, not here."""
    tenant_slug:             str
    message_override:        Optional[str] = Field(default=None, max_length=MAX_MESSAGE_CHARS)


class AudienceChangeIn(BaseModel):
    """Spec §68: included=true adds an Audience that is not on by default,
    included=false opts out of one that is."""
    audience_id: int
    included:    bool


class EventIn(BaseModel):
    type_id:          int
    name:             str = Field(min_length=1, max_length=120)
    scope:            str = "alliance"
    leadership_only:  bool = False
    start_time_utc:   str
    anchor_date:      str
    location:         str = Field(default="", max_length=200)
    cover_image_data: Optional[str] = None
    alliances:        list[EventAllianceIn] = []
    audience_changes: list[AudienceChangeIn] = []
    # The next five fall back to the event type's defaults when omitted.
    # An explicit null for duration_hours or interval_days is meaningful
    # (a plain message with no calendar entry; a one-off event).
    message:          Optional[str] = Field(default=None, max_length=MAX_MESSAGE_CHARS)
    duration_hours:   Optional[float] = None
    recurrence_kind:  Optional[str] = None
    interval_days:    Optional[int] = None
    until_date:       Optional[str] = None
    mention_role:     Optional[bool] = None
    clean_up_reminders: Optional[bool] = None  # spec §83
    reminder_minutes: Optional[list[int]] = None
    reminder_messages: Optional[dict[int, str]] = None

    _validate_scope = field_validator("scope", mode="before")(parse_scope)
    _validate_time = field_validator("start_time_utc", mode="before")(parse_start_time_utc)
    _validate_anchor = field_validator("anchor_date", mode="before")(parse_anchor_date)
    _validate_cover_image = field_validator("cover_image_data", mode="before")(parse_cover_image_data)
    _validate_duration = field_validator("duration_hours", mode="before")(parse_optional_duration_hours)
    _validate_interval = field_validator("interval_days", mode="before")(parse_optional_interval_days)
    _validate_recurrence = field_validator("recurrence_kind", mode="before")(
        lambda cls, v: parse_recurrence_kind(v, allow_none=True))
    _validate_until = field_validator("until_date", mode="before")(
        lambda cls, v: parse_anchor_date(v, allow_none=True))
    _validate_reminders = field_validator("reminder_minutes", mode="before")(
        lambda cls, v: parse_reminder_minutes(v, allow_none=True))
    _validate_reminder_messages = field_validator("reminder_messages", mode="before")(
        lambda cls, v: parse_reminder_messages(v, allow_none=True))

    @field_validator("name", mode="after")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Name cannot be blank")
        return v


class EventPatch(BaseModel):
    """Partial update. Only fields present in the request are applied;
    duration_hours, interval_days, until_date and cover_image_data may be
    sent as null/"" to clear them."""
    type_id:          Optional[int] = None
    name:             Optional[str] = Field(default=None, min_length=1, max_length=120)
    scope:            Optional[str] = None
    leadership_only:  Optional[bool] = None
    active:           Optional[bool] = None
    start_time_utc:   Optional[str] = None
    anchor_date:      Optional[str] = None
    location:         Optional[str] = Field(default=None, max_length=200)
    cover_image_data: Optional[str] = None
    message:          Optional[str] = Field(default=None, max_length=MAX_MESSAGE_CHARS)
    duration_hours:   Optional[float] = None
    recurrence_kind:  Optional[str] = None
    interval_days:    Optional[int] = None
    until_date:       Optional[str] = None
    mention_role:     Optional[bool] = None
    clean_up_reminders: Optional[bool] = None  # spec §83
    reminder_minutes: Optional[list[int]] = None
    reminder_messages: Optional[dict[int, str]] = None
    alliances:        Optional[list[EventAllianceIn]] = None
    audience_changes: Optional[list[AudienceChangeIn]] = None

    _validate_scope = field_validator("scope", mode="before")(
        lambda cls, v: parse_scope(v, allow_none=True))
    _validate_time = field_validator("start_time_utc", mode="before")(
        lambda cls, v: parse_start_time_utc(v, allow_none=True))
    _validate_anchor = field_validator("anchor_date", mode="before")(
        lambda cls, v: parse_anchor_date(v, allow_none=True))
    _validate_cover_image = field_validator("cover_image_data", mode="before")(
        lambda cls, v: parse_cover_image_data(v, allow_none=True))
    _validate_duration = field_validator("duration_hours", mode="before")(parse_optional_duration_hours)
    _validate_interval = field_validator("interval_days", mode="before")(parse_optional_interval_days)
    _validate_recurrence = field_validator("recurrence_kind", mode="before")(
        lambda cls, v: parse_recurrence_kind(v, allow_none=True))
    _validate_until = field_validator("until_date", mode="before")(
        lambda cls, v: parse_anchor_date(v, allow_none=True))
    _validate_reminders = field_validator("reminder_minutes", mode="before")(
        lambda cls, v: parse_reminder_minutes(v, allow_none=True))
    _validate_reminder_messages = field_validator("reminder_messages", mode="before")(
        lambda cls, v: parse_reminder_messages(v, allow_none=True))


class EventPreviewIn(BaseModel):
    """What the Events form sends to see where an event would post (spec §67.6)."""
    scope:            str = "alliance"
    leadership_only:  bool = False
    message:          str = Field(default="", max_length=MAX_MESSAGE_CHARS)
    alliances:        list[EventAllianceIn] = []
    audience_changes: list[AudienceChangeIn] = []

    _validate_scope = field_validator("scope", mode="before")(parse_scope)
