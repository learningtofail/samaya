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
