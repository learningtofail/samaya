from sqlalchemy import (
    Boolean, CheckConstraint, Column, Date, DateTime,
    ForeignKey, Integer, Numeric, String, Text, Time,
    UniqueConstraint, func
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class EventDefinition(Base):
    __tablename__ = "event_definitions"

    id                       = Column(Integer, primary_key=True)
    name                     = Column(Text, nullable=False)
    interval_days            = Column(Integer, nullable=False)
    start_time_utc           = Column(Time, nullable=False)
    duration_hours           = Column(Numeric(4, 1), nullable=False)
    discord_channel          = Column(Text, nullable=False, default="")
    description              = Column(Text, nullable=False, default="")
    category                 = Column(Text, nullable=False, default="Other")
    active                   = Column(Boolean, nullable=False, default=True)
    anchor_date              = Column(Date, nullable=False)
    notification_channel_id  = Column(Text, nullable=False, default="")
    notification_role_id     = Column(Text, nullable=False, default="")
    notify_minutes_before    = Column(Integer, nullable=True)
    created_at               = Column(DateTime(timezone=True), server_default=func.now())
    updated_at               = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    occurrences = relationship("Occurrence", back_populates="event", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("interval_days > 0",    name="ck_interval_positive"),
        CheckConstraint("duration_hours > 0",   name="ck_duration_positive"),
    )


class Occurrence(Base):
    __tablename__ = "occurrences"

    id                  = Column(Integer, primary_key=True)
    event_id            = Column(Integer, ForeignKey("event_definitions.id", ondelete="CASCADE"), nullable=False)
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
    __tablename__ = "post_log"

    id               = Column(Integer, primary_key=True)
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
        UniqueConstraint("event_name", "occurrence_date", name="uq_post_log"),
    )


class DiscordConfig(Base):
    __tablename__ = "discord_config"

    id         = Column(Integer, primary_key=True, default=1)
    bot_token  = Column(Text, nullable=False)
    guild_id   = Column(Text, nullable=False)
    public_key = Column(Text, nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    updated_by = Column(Text, nullable=False, default="system")

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_single_row"),
    )


class SchedulerState(Base):
    __tablename__ = "scheduler_state"

    job_name     = Column(Text, primary_key=True)
    last_run_utc = Column(DateTime(timezone=True))
    last_result  = Column(Text)
    last_detail  = Column(Text)
    next_run_utc = Column(DateTime(timezone=True))
