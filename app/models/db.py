"""The full data model, grouped roughly in the order each layer was
built:

  Kingdom, Tenant                        — multi-tenancy (Phase 1)
  User, UserTenant, UserKingdom, Invite,
  AuditLog                               — real auth (Phase 4)
  EventDefinition, EventTenantNotification,
  Occurrence, PostLog                    — events and Discord posting
  Announcement, AnnouncementTarget       — scheduled announcements (Phase 5)
  SchedulerState                         — background job status, per tenant

Every table has its own docstring explaining what it's for and why it's
shaped the way it is — this header is just the map."""
from sqlalchemy import (
    Boolean, CheckConstraint, Column, Date, DateTime,
    ForeignKey, Integer, Numeric, Text, Time,
    UniqueConstraint, func
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class Kingdom(Base):
    """A Kingshot game server (e.g. 'Kingdom 138') — distinct from a Discord
    server. Multiple Tenants (alliances), each on their own Discord guild,
    can belong to the same Kingdom."""
    __tablename__ = "kingdoms"

    id   = Column(Integer, primary_key=True)
    name = Column(Text, nullable=False)
    slug = Column(Text, nullable=False, unique=True)

    tenants = relationship("Tenant", back_populates="kingdom")


class Tenant(Base):
    """An alliance. Replaces the old singleton DiscordConfig — every alliance
    is its own row, including ones that share a Discord guild (guild_id can
    repeat across rows) or share a bot token (bot_token/public_key nullable
    = falls back to the platform-wide bot, see routers.admin.deps.PLATFORM_BOT_TOKEN
    and routers.webhooks.PLATFORM_PUBLIC_KEY)."""
    __tablename__ = "tenants"

    id         = Column(Integer, primary_key=True)
    kingdom_id = Column(Integer, ForeignKey("kingdoms.id"), nullable=False)
    name       = Column(Text, nullable=False)
    slug       = Column(Text, nullable=False, unique=True)
    guild_id   = Column(Text, nullable=False)
    bot_token  = Column(Text, nullable=True)
    public_key = Column(Text, nullable=True)
    color      = Column(Text, nullable=False, default="#475569")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    kingdom = relationship("Kingdom", back_populates="tenants")


class User(Base):
    """A person, identified by their Discord account. Created only when
    someone authenticates via Discord OAuth — never on invite creation
    itself (see Invite below); an invite grants access to a discord_id
    that may or may not have a User row yet."""
    __tablename__ = "users"

    id             = Column(Integer, primary_key=True)
    discord_id     = Column(Text, nullable=False, unique=True)
    discord_username = Column(Text, nullable=False)
    is_superadmin  = Column(Boolean, nullable=False, default=False)
    created_at     = Column(DateTime(timezone=True), server_default=func.now())
    last_login_at  = Column(DateTime(timezone=True))


class UserTenant(Base):
    """Grants a user owner/coordinator access to one Tenant. Independent
    per tenant — a coordinator of MOD is not automatically anything to
    NSR, even if the two share a Discord guild."""
    __tablename__ = "user_tenants"

    id         = Column(Integer, primary_key=True)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=False)
    tenant_id  = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    role       = Column(Text, nullable=False)  # owner | coordinator
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "tenant_id", name="uq_user_tenant"),
        CheckConstraint("role IN ('owner', 'coordinator')", name="ck_user_tenant_role"),
    )


class UserKingdom(Base):
    """Grants a user the right to create/edit/cancel kingdom-wide events
    for one Kingdom. Deliberately separate from UserTenant — being an
    owner of one alliance in a Kingdom does not imply this; it is not a
    role with sub-tiers, just a flag (see the multi-tenant design doc,
    'Kingdom-wide events')."""
    __tablename__ = "user_kingdoms"

    id         = Column(Integer, primary_key=True)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=False)
    kingdom_id = Column(Integer, ForeignKey("kingdoms.id"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "kingdom_id", name="uq_user_kingdom"),
    )


class Invite(Base):
    """The only path to creating a UserTenant or UserKingdom grant (and,
    incidentally, the only path to a first-time visitor ever completing
    Discord OAuth at all — see routers/auth.py). Exactly one of
    tenant_id/kingdom_id is set, matching role."""
    __tablename__ = "invites"

    id            = Column(Integer, primary_key=True)
    token         = Column(Text, nullable=False, unique=True)
    tenant_id     = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    kingdom_id    = Column(Integer, ForeignKey("kingdoms.id"), nullable=True)
    role          = Column(Text, nullable=False)  # owner | coordinator | kingdom_coordinator
    created_by    = Column(Integer, ForeignKey("users.id"), nullable=True)
    expires_at    = Column(DateTime(timezone=True), nullable=False)
    used_at       = Column(DateTime(timezone=True))
    used_by       = Column(Integer, ForeignKey("users.id"))
    revoked_at    = Column(DateTime(timezone=True))
    created_at    = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "role IN ('owner', 'coordinator', 'kingdom_coordinator')",
            name="ck_invite_role",
        ),
        CheckConstraint(
            "(tenant_id IS NOT NULL AND kingdom_id IS NULL) OR "
            "(tenant_id IS NULL AND kingdom_id IS NOT NULL)",
            name="ck_invite_exactly_one_target",
        ),
    )


class AuditLog(Base):
    """Append-only. No rollback — see the multi-tenant design doc, 'Audit
    log — recorded, not reversible': many of the actions this covers also
    reach Discord, and restoring a DB snapshot doesn't undo that. before/
    after are JSON-serialized dicts, not ORM objects, so a future schema
    change doesn't retroactively break reading old log rows."""
    __tablename__ = "audit_log"

    id          = Column(Integer, primary_key=True)
    tenant_id   = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    user_id     = Column(Integer, ForeignKey("users.id"), nullable=True)
    table_name  = Column(Text, nullable=False)
    row_id      = Column(Integer)
    action      = Column(Text, nullable=False)  # create | update | delete
    before      = Column(Text)  # JSON, null on create
    after       = Column(Text)  # JSON, null on delete
    timestamp   = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("action IN ('create', 'update', 'delete')", name="ck_audit_action"),
    )


class EventDefinition(Base):
    __tablename__ = "event_definitions"

    id                       = Column(Integer, primary_key=True)
    owning_tenant_id         = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    scope                    = Column(Text, nullable=False, default="alliance")
    name                     = Column(Text, nullable=False)
    interval_days            = Column(Integer, nullable=False)
    start_time_utc           = Column(Time, nullable=False)
    duration_hours           = Column(Numeric(4, 1), nullable=False)
    discord_channel          = Column(Text, nullable=False, default="")
    description              = Column(Text, nullable=False, default="")
    leadership_only          = Column(Boolean, nullable=False, default=False)
    active                   = Column(Boolean, nullable=False, default=True)
    anchor_date              = Column(Date, nullable=False)
    notification_channel_id  = Column(Text, nullable=False, default="")
    notification_role_id     = Column(Text, nullable=False, default="")
    notify_minutes_before    = Column(Integer, nullable=True)
    created_at               = Column(DateTime(timezone=True), server_default=func.now())
    updated_at               = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    occurrences = relationship("Occurrence", back_populates="event", cascade="all, delete-orphan")
    tenant_notifications = relationship(
        "EventTenantNotification", back_populates="event", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("interval_days > 0",    name="ck_interval_positive"),
        CheckConstraint("duration_hours > 0",   name="ck_duration_positive"),
        CheckConstraint("scope IN ('alliance', 'kingdom-wide')", name="ck_scope_valid"),
    )


class EventTenantNotification(Base):
    """Per-tenant notification-channel override for a kingdom-wide event.

    notification_channel_id/notification_role_id on EventDefinition are real
    Discord snowflake IDs, not free text — meaningless (or actively wrong)
    if reused across a different guild than the one they were set in. An
    alliance-scoped event has exactly one guild, so its bare columns above
    are fine as-is. A kingdom-wide event needs one of these rows per target
    tenant that wants a ping; a tenant with no row here still gets the
    Discord Scheduled Event itself, just no channel announcement/reminder.
    """
    __tablename__ = "event_tenant_notifications"

    id                       = Column(Integer, primary_key=True)
    event_id                 = Column(Integer, ForeignKey("event_definitions.id", ondelete="CASCADE"), nullable=False)
    tenant_id                = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    notification_channel_id  = Column(Text, nullable=False, default="")
    notification_role_id     = Column(Text, nullable=False, default="")

    event = relationship("EventDefinition", back_populates="tenant_notifications")

    __table_args__ = (
        UniqueConstraint("event_id", "tenant_id", name="uq_event_tenant_notification"),
    )


class Occurrence(Base):
    __tablename__ = "occurrences"

    id                  = Column(Integer, primary_key=True)
    event_id            = Column(Integer, ForeignKey("event_definitions.id", ondelete="CASCADE"), nullable=False)
    tenant_id           = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    occurrence_date     = Column(Date, nullable=False)
    start_datetime_utc  = Column(DateTime(timezone=True), nullable=False)
    end_datetime_utc    = Column(DateTime(timezone=True), nullable=False)
    post_to_discord     = Column(Boolean, nullable=False, default=False)
    post_status         = Column(Text, nullable=False, default="pending")
    status_detail       = Column(Text)
    reminder_sent       = Column(Boolean, nullable=False, default=False)
    window_generated_at = Column(DateTime(timezone=True), server_default=func.now())

    event = relationship("EventDefinition", back_populates="occurrences")

    __table_args__ = (
        UniqueConstraint("event_id", "occurrence_date", name="uq_occurrence"),
    )


class PostLog(Base):
    """One row per (tenant, occurrence) that was posted or attempted. A
    kingdom-wide event's single Occurrence produces one PostLog row per
    Tenant in its Kingdom — each an independent Discord post, tracked and
    cancellable independently (see routers/admin/occurrences.py)."""
    __tablename__ = "post_log"

    id               = Column(Integer, primary_key=True)
    tenant_id        = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    event_id         = Column(Integer, ForeignKey("event_definitions.id"), nullable=True)
    event_name       = Column(Text, nullable=False)
    occurrence_date  = Column(Date, nullable=False)
    discord_event_id = Column(Text)
    discord_guild_id = Column(Text, nullable=False)
    posted_at_utc    = Column(DateTime(timezone=True))
    posted_by        = Column(Text, nullable=False, default="system")
    status           = Column(Text, nullable=False)
    status_detail    = Column(Text)

    __table_args__ = (
        UniqueConstraint("tenant_id", "event_name", "occurrence_date", name="uq_post_log"),
    )


class Announcement(Base):
    """A scheduled markdown-text announcement — not built on
    EventDefinition/Occurrence since the shape is fundamentally
    different (no start/end time, no recurrence). Delivery fans out to
    one or more AnnouncementTargets, each an independent post, since
    targets may be different Discord guilds with different bot tokens."""
    __tablename__ = "announcements"

    id               = Column(Integer, primary_key=True)
    owning_tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    title            = Column(Text, nullable=False)
    body_markdown    = Column(Text, nullable=False)
    scheduled_for    = Column(DateTime(timezone=True), nullable=False)
    status           = Column(Text, nullable=False, default="scheduled")
    created_by       = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at       = Column(DateTime(timezone=True), server_default=func.now())
    posted_at        = Column(DateTime(timezone=True))

    targets = relationship("AnnouncementTarget", back_populates="announcement", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'scheduled', 'posted', 'failed', 'cancelled')",
            name="ck_announcement_status",
        ),
    )


class AnnouncementTarget(Base):
    """One row per (announcement, tenant) — independent success/failure
    per target, same resilience principle as PostLog's kingdom-wide
    fan-out (see PostLog's docstring): one tenant's Discord API hiccup
    never blocks or corrupts another's delivery."""
    __tablename__ = "announcement_targets"

    id                 = Column(Integer, primary_key=True)
    announcement_id    = Column(Integer, ForeignKey("announcements.id", ondelete="CASCADE"), nullable=False)
    tenant_id          = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    discord_channel_id = Column(Text, nullable=False)
    discord_message_id = Column(Text)
    post_status        = Column(Text, nullable=False, default="pending")
    status_detail      = Column(Text)

    announcement = relationship("Announcement", back_populates="targets")

    __table_args__ = (
        UniqueConstraint("announcement_id", "tenant_id", name="uq_announcement_target"),
        CheckConstraint("post_status IN ('pending', 'posted', 'error')", name="ck_announcement_target_status"),
    )


class SchedulerState(Base):
    """One row per (tenant, job) — a regeneration failure for one alliance
    no longer masks another's status."""
    __tablename__ = "scheduler_state"

    tenant_id    = Column(Integer, ForeignKey("tenants.id"), primary_key=True)
    job_name     = Column(Text, primary_key=True)
    last_run_utc = Column(DateTime(timezone=True))
    last_result  = Column(Text)
    last_detail  = Column(Text)
    next_run_utc = Column(DateTime(timezone=True))
