"""Pydantic request models for the admin event/config API.

EventIn (create) and EventPatch (update) share every field validator via
services/validators.py — the only difference is EventPatch's `allow_none`
flag, since a partial update uses None to mean "leave this field
unchanged" rather than "invalid".
"""
from typing import Optional

from pydantic import BaseModel, field_validator, model_validator

from services.validators import (
    parse_interval_days, parse_duration_hours, parse_scope,
    parse_start_time_utc, parse_anchor_date, parse_notify_minutes_before,
    parse_cover_image_data,
)


class EventTargetIn(BaseModel):
    """One explicit extra (tenant, notification channel, notification
    role) destination for an event — see spec §20 and models.db.EventTarget.
    Distinct from AnnouncementTargetIn: an announcement target's channel is
    where the message itself is posted, whereas an event target's channel/
    role is purely for the pre-event ping — the event's own Discord
    Scheduled Event post goes to that target tenant's guild automatically,
    using the event's own discord_channel as the location text (same as
    kingdom-wide fan-out already does)."""
    tenant_slug:              str
    notification_channel_id:  str = ""
    notification_role_id:     str = ""


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
    cover_image_data:        Optional[str] = None
    targets:                 list[EventTargetIn] = []

    _validate_interval = field_validator("interval_days", mode="before")(parse_interval_days)
    _validate_duration = field_validator("duration_hours", mode="before")(parse_duration_hours)
    _validate_scope     = field_validator("scope", mode="before")(parse_scope)
    _validate_time     = field_validator("start_time_utc", mode="before")(parse_start_time_utc)
    _validate_anchor   = field_validator("anchor_date", mode="before")(parse_anchor_date)
    _validate_notify   = field_validator("notify_minutes_before", mode="before")(parse_notify_minutes_before)
    _validate_cover_image = field_validator("cover_image_data", mode="before")(parse_cover_image_data)


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
    cover_image_data:        Optional[str]  = None
    # None = leave the current target list alone; [] = explicitly clear it.
    # The admin UI always sends one or the other (never omits the key), so
    # in practice this is always a full replace when the modal saves — see
    # events.js's saveEvent().
    targets:                 Optional[list[EventTargetIn]] = None

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
    _validate_cover_image = field_validator("cover_image_data", mode="before")(
        lambda cls, v: parse_cover_image_data(v, allow_none=True))


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
    server_id:  int
    color:      str = "#475569"


class TenantPatch(BaseModel):
    name:       Optional[str] = None
    slug:       Optional[str] = None
    server_id:  Optional[int] = None
    color:      Optional[str] = None


class DiscordServerIn(BaseModel):
    """spec §25 — a Discord guild this app posts to, independent of any
    one alliance. bot_token/public_key are optional: left unset, the
    server (and every Tenant referencing it) falls back to the
    platform-wide bot."""
    name:       str
    guild_id:   str
    bot_token:  Optional[str] = None
    public_key: Optional[str] = None


class DiscordServerPatch(BaseModel):
    name:       Optional[str] = None
    guild_id:   Optional[str] = None
    bot_token:  Optional[str] = None
    public_key: Optional[str] = None


class KingdomIn(BaseModel):
    name: str
    slug: str


class KingdomPatch(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None


class TenantInviteIn(BaseModel):
    role: str  # owner | coordinator | viewer

    @field_validator("role")
    @classmethod
    def _validate_role(cls, v):
        if v not in ("owner", "coordinator", "viewer"):
            raise ValueError("role must be 'owner', 'coordinator', or 'viewer'")
        return v


class UserTenantPatch(BaseModel):
    """Changes an existing UserTenant grant's role — e.g. promoting a
    coordinator to owner. Distinct from TenantInviteIn: this edits a grant
    that already exists, rather than creating a new pending invite."""
    role: str  # owner | coordinator | viewer

    @field_validator("role")
    @classmethod
    def _validate_role(cls, v):
        if v not in ("owner", "coordinator", "viewer"):
            raise ValueError("role must be 'owner', 'coordinator', or 'viewer'")
        return v


class UserPatch(BaseModel):
    """Superadmin-only: toggles a User's platform-wide is_superadmin flag.
    No other User fields are editable here — discord_id/discord_username
    come from Discord OAuth and aren't ours to change."""
    is_superadmin: bool


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
    leadership_only: bool = False
    recurring: bool = False
    interval_days: Optional[int] = None
    event_offset_minutes: int = 0

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

    @model_validator(mode="after")
    def _validate_recurring_interval(self):
        # Mirrors EventDefinition's ck_interval_positive constraint —
        # required when recurring, meaningless (and left null) otherwise.
        if self.recurring and (self.interval_days is None or self.interval_days <= 0):
            raise ValueError("interval_days must be a positive integer when recurring is true")
        if not self.recurring:
            self.interval_days = None
        return self


class AnnouncementTemplateIn(BaseModel):
    """spec §27 — a reusable starting point for AnnouncementIn.title/
    body_markdown/leadership_only/event_offset_minutes. title_template/
    body_template may contain services/templates.py's placeholders; unlike
    AnnouncementIn.body_markdown, there's no 2000-char Discord limit
    enforced here, since a placeholder like {event_time} expands at
    delivery time to something longer than it is in the template text —
    the real limit is enforced on the announcement created from it."""
    name:                  str
    title_template:        str
    body_template:         str
    leadership_only:       bool = False
    event_offset_minutes:  int = 0


class AnnouncementTemplatePatch(BaseModel):
    name:                  Optional[str] = None
    title_template:        Optional[str] = None
    body_template:         Optional[str] = None
    leadership_only:       Optional[bool] = None
    event_offset_minutes:  Optional[int] = None


class OccurrencePatch(BaseModel):
    post_to_discord: Optional[bool] = None
    post_status:     Optional[str]  = None
