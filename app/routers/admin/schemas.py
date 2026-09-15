"""Pydantic request models for the admin event/config API.

EventIn (create) and EventPatch (update) share every field validator via
services/validators.py — the only difference is EventPatch's `allow_none`
flag, since a partial update uses None to mean "leave this field
unchanged" rather than "invalid".
"""
from typing import Optional

from pydantic import BaseModel, field_validator

from services.validators import (
    parse_interval_days, parse_duration_hours, parse_scope,
    parse_start_time_utc, parse_anchor_date, parse_notify_minutes_before,
)


class EventIn(BaseModel):
    name:                    str
    interval_days:           int
    start_time_utc:          str
    duration_hours:          float
    discord_channel:         str = ""
    description:             str = ""
    scope:                   str = "alliance"
    leadership_only:         bool = False
    anchor_date:             str
    notification_channel_id: str = ""
    notification_role_id:    str = ""
    notify_minutes_before:   Optional[int] = None

    _validate_interval = field_validator("interval_days", mode="before")(parse_interval_days)
    _validate_duration = field_validator("duration_hours", mode="before")(parse_duration_hours)
    _validate_scope     = field_validator("scope", mode="before")(parse_scope)
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
    scope:                   Optional[str]  = None
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
    _validate_scope     = field_validator("scope", mode="before")(
        lambda cls, v: parse_scope(v, allow_none=True))
    _validate_time     = field_validator("start_time_utc", mode="before")(
        lambda cls, v: parse_start_time_utc(v, allow_none=True))
    _validate_anchor   = field_validator("anchor_date", mode="before")(
        lambda cls, v: parse_anchor_date(v, allow_none=True))
    _validate_notify   = field_validator("notify_minutes_before", mode="before")(
        lambda cls, v: parse_notify_minutes_before(v, allow_none=True))


class EventTenantNotificationIn(BaseModel):
    """Per-tenant notification override for a kingdom-wide event — see
    models.db.EventTenantNotification for why this can't just reuse
    EventDefinition's own notification_channel_id/notification_role_id.
    No tenant_id field: the endpoint that uses this always applies it to
    the caller's own current tenant (X-Tenant-Slug), never one supplied
    in the body, so one tenant can't set another's notification channel."""
    notification_channel_id:  str = ""
    notification_role_id:     str = ""


class TenantIn(BaseModel):
    kingdom_id: int
    name:       str
    slug:       str
    guild_id:   str
    bot_token:  Optional[str] = None
    public_key: Optional[str] = None
    color:      str = "#475569"


class TenantPatch(BaseModel):
    name:       Optional[str] = None
    slug:       Optional[str] = None
    guild_id:   Optional[str] = None
    bot_token:  Optional[str] = None
    public_key: Optional[str] = None
    color:      Optional[str] = None


class KingdomIn(BaseModel):
    name: str
    slug: str


class TenantInviteIn(BaseModel):
    role: str  # owner | coordinator

    @field_validator("role")
    @classmethod
    def _validate_role(cls, v):
        if v not in ("owner", "coordinator"):
            raise ValueError("role must be 'owner' or 'coordinator'")
        return v


class KingdomInviteIn(BaseModel):
    kingdom_id: int


class AnnouncementTargetIn(BaseModel):
    tenant_slug: str
    discord_channel_id: str


class AnnouncementIn(BaseModel):
    title: str
    body_markdown: str
    scheduled_for: str  # ISO datetime, UTC
    targets: list[AnnouncementTargetIn]

    @field_validator("body_markdown")
    @classmethod
    def _validate_length(cls, v):
        # Discord's hard cap on message content — reject here, at
        # creation, rather than let the scheduler discover it at send
        # time when it's too late to fix.
        if len(v) > 2000:
            raise ValueError(f"Announcement body is {len(v)} characters; Discord's limit is 2000")
        return v

    @field_validator("targets")
    @classmethod
    def _validate_at_least_one_target(cls, v):
        if not v:
            raise ValueError("At least one target is required")
        return v


class OccurrencePatch(BaseModel):
    post_to_discord: Optional[bool] = None
    post_status:     Optional[str]  = None
