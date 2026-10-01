"""Pydantic request models for the admin setup API: kingdoms, alliances
(tenants), Discord servers, access and invites, users. Event, event type and
occurrence models live in unified_schemas.py."""
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from services.validators import parse_cover_image_data


class TenantIn(BaseModel):
    kingdom_id: int
    name:       str
    slug:       str
    server_id:  int
    secondary_server_ids: list[int] = []
    color:      str = "#475569"
    icon_image_data: Optional[str] = None

    _validate_icon = field_validator("icon_image_data", mode="before")(parse_cover_image_data)


class TenantPatch(BaseModel):
    name:       Optional[str] = None
    slug:       Optional[str] = None
    server_id:  Optional[int] = None
    secondary_server_ids: Optional[list[int]] = None
    color:      Optional[str] = None
    icon_image_data: Optional[str] = None

    # Same nullable-field PATCH semantics as EventPatch.cover_image_data
    # (spec §35): omitted/null leaves the icon unchanged, "" clears it.
    _validate_icon = field_validator("icon_image_data", mode="before")(
        lambda cls, v: parse_cover_image_data(v, allow_none=True))


class DiscordServerIn(BaseModel):
    """spec §25 — a Discord guild this app posts to, independent of any
    one alliance. bot_token/public_key are optional: left unset, the
    server (and every Tenant referencing it) falls back to the
    platform-wide bot."""
    kingdom_id: int
    name:       str
    guild_id:   str
    bot_token:  Optional[str] = None
    public_key: Optional[str] = None


class DiscordServerPatch(BaseModel):
    kingdom_id: Optional[int] = None
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
    # Spec §38.7 — kingdom-wide branding. Optional[str] with no validator
    # beyond "it's a string": these are plain display titles, not
    # structured data, so free text (including emoji) is fine. An empty
    # string explicitly resets to the hardcoded default (same
    # omitted/null-vs-"" convention as every other nullable PATCH field
    # in this app), since NULL is what every reader already treats as
    # "no override set."
    public_site_title:   Optional[str] = None
    admin_console_title: Optional[str] = None


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
    """Superadmin-only: toggles a User's platform-wide is_superadmin flag
    and/or sets the display name shown on public feedback responses (spec
    §66.10). Only fields present are applied. discord_id/discord_username
    come from Discord OAuth and aren't ours to change."""
    is_superadmin: Optional[bool] = None
    display_name:  Optional[str] = Field(default=None, max_length=60)


class DisplayNameIn(BaseModel):
    """The caller's own display name. Blank clears it."""
    display_name: str = Field(max_length=60)


class KingdomInviteIn(BaseModel):
    kingdom_id: int


