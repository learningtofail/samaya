"""The full data model, grouped roughly in the order each layer was
built:

  Kingdom, Tenant                        — multi-tenancy (Phase 1)
  User, UserTenant, UserKingdom, Invite,
  AuditLog                               — real auth (Phase 4)
  Ticket, TicketVote, TicketResponse     — the public feedback board
  EventType, Event, EventAlliance,
  EventReminder, EventOccurrence,
  Delivery                               — events and Discord delivery (spec §66)

Every table has its own docstring explaining what it's for and why it's
shaped the way it is — this header is just the map."""
import uuid

from sqlalchemy import (
    JSON, Boolean, CheckConstraint, Column, Date, DateTime,
    ForeignKey, Index, Integer, Numeric, Text, Time,
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

    # Spec §38.7 — kingdom-wide branding, edited from the admin console's
    # merged Access & Platform page (superadmin-only, since this instance
    # is one Kingdom deployment and branding isn't an individual alliance's
    # call to make). NULL means "use the hardcoded default" everywhere
    # these are read, so a deployment predating this field renders exactly
    # as it always has.
    public_site_title   = Column(Text, nullable=True)
    admin_console_title = Column(Text, nullable=True)

    tenants = relationship("Tenant", back_populates="kingdom")


class DiscordServer(Base):
    """A Discord guild this app posts to — infrastructure, not an alliance.
    Introduced in spec §25 to make explicit what Tenant.guild_id already
    allowed implicitly: multiple alliances (Tenants) can share one Discord
    server. Not scoped to a Kingdom — a server (e.g. HTD, a de facto
    Kingshot-wide hub per spec §19) can host alliances spanning Kingdoms,
    or none at all beyond the ones actually created there.

    bot_token/public_key are nullable = falls back to the platform-wide
    bot (see routers.admin.deps.PLATFORM_BOT_TOKEN and
    routers.webhooks.PLATFORM_PUBLIC_KEY). Once alliances share a server,
    they share that server's single bot_token/public_key — there is no
    per-alliance override once sharing is in effect (spec §25, "one bot
    per server")."""
    __tablename__ = "discord_servers"

    id         = Column(Integer, primary_key=True)
    name       = Column(Text, nullable=False)
    guild_id   = Column(Text, nullable=False, unique=True)
    bot_token  = Column(Text, nullable=True)
    public_key = Column(Text, nullable=True)
    # NOT NULL since spec §66.8: production's hand-written scripts always
    # created this column NOT NULL, so the model now says the same.
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    tenants = relationship("Tenant", back_populates="server")


class Tenant(Base):
    """An alliance. Replaces the old singleton DiscordConfig — every alliance
    is its own row. Its Discord identity (guild/bot credentials) lives on
    the DiscordServer it references (spec §25), not on the Tenant itself,
    since multiple alliances routinely share one Discord server (see
    DiscordServer's own docstring) — that sharing used to be implicit
    (repeated guild_id values) and is now a first-class relationship."""
    __tablename__ = "tenants"

    id         = Column(Integer, primary_key=True)
    kingdom_id = Column(Integer, ForeignKey("kingdoms.id"), nullable=False)
    server_id  = Column(Integer, ForeignKey("discord_servers.id"), nullable=False)
    name       = Column(Text, nullable=False)
    slug       = Column(Text, nullable=False, unique=True)
    color      = Column(Text, nullable=False, default="#475569")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Spec §38 (new feature request alongside the admin consolidation) — an
    # optional 1:1 icon/logo, stored as the full data URI exactly like
    # EventDefinition.cover_image_data (spec §35): whatever a browser's
    # FileReader.readAsDataURL() produces, validated by the same
    # parse_cover_image_data() (the function is about "an image data URI,"
    # not specifically events). NULL/empty means no icon — every place
    # that renders one (public masthead, tenant badges, the admin filter
    # dropdowns, the alliance switcher) falls back to the existing plain
    # color-pill/text treatment, so a tenant predating this field is
    # unaffected.
    icon_image_data = Column(Text, nullable=True)

    # Spec §66.1 — the alliance's one named Notifications destination: where
    # reminder deliveries go unless an event overrides it. Empty means "no
    # destination configured" (the delivery is recorded as an error rather
    # than silently skipped).
    notification_channel_id = Column(Text, nullable=False, default="", server_default="")
    notification_role_id    = Column(Text, nullable=False, default="", server_default="")

    # Eager by default (lazy="joined") on both: a single cheap FK join
    # each, needed wherever a Tenant's Discord credentials (server) or
    # Kingdom name (kingdom, for the {kingdom_name} announcement-template
    # placeholder — spec §27) matter, in places that fetch a Tenant via
    # session.get() rather than a hand-written eager-loading query
    # (deps.py, discord_sync.py, occurrences.py, the announcement/reminder
    # schedulers, ...). Eager-loading here avoids auditing every one of
    # those call sites individually, and avoids the MissingGreenlet class
    # of bug a missed spot would cause (see spec §25.2).
    kingdom    = relationship("Kingdom", back_populates="tenants", lazy="joined")
    server     = relationship("DiscordServer", back_populates="tenants", lazy="joined")


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
    # Spec §66.1 — the name shown publicly next to a coordinator's feedback
    # responses; NULL/empty falls back to discord_username.
    display_name   = Column(Text, nullable=True)


class UserTenant(Base):
    """Grants a user owner/coordinator/viewer access to one Tenant.
    Independent per tenant — a coordinator of MOD is not automatically
    anything to NSR, even if the two share a Discord guild. 'viewer'
    (spec §31) is read-only: passes every GET-only dependency the same
    as 'coordinator' does, but is rejected by require_not_viewer, which
    every mutating admin route depends on instead of plain
    get_current_tenant."""
    __tablename__ = "user_tenants"

    id         = Column(Integer, primary_key=True)
    user_id    = Column(Integer, ForeignKey("users.id"), nullable=False)
    tenant_id  = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    role       = Column(Text, nullable=False)  # owner | coordinator | viewer
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "tenant_id", name="uq_user_tenant"),
        CheckConstraint("role IN ('owner', 'coordinator', 'viewer')", name="ck_user_tenant_role"),
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
    role          = Column(Text, nullable=False)  # owner | coordinator | viewer | kingdom_coordinator
    created_by    = Column(Integer, ForeignKey("users.id"), nullable=True)
    expires_at    = Column(DateTime(timezone=True), nullable=False)
    used_at       = Column(DateTime(timezone=True))
    used_by       = Column(Integer, ForeignKey("users.id"))
    revoked_at    = Column(DateTime(timezone=True))
    created_at    = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "role IN ('owner', 'coordinator', 'viewer', 'kingdom_coordinator')",
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


class Ticket(Base):
    """Community-submitted input from the public /feedback board (spec
    §40–43): general feedback, a request for a new event/announcement, or
    an error report flagged from a specific row on the public events page.
    One shared table for all three kinds (plus 'feedback') rather than
    three separate tables, since they share the same public listing,
    upvoting, and admin triage surface — the only real differences are
    which of error_type/related_occurrence_id/related_announcement_id are
    populated, which the CheckConstraints below document rather than
    splitting into subclasses SQLAlchemy has no clean single-table story
    for anyway.

    No User FK anywhere on this table or TicketVote below — the public
    board is deliberately unauthenticated (same trust level as the events
    page itself, spec §43.2), so there is no user to attribute a ticket or
    vote to, only an optional free-text submitter_contact for a human to
    follow up with manually.
    """
    __tablename__ = "tickets"

    id                       = Column(Integer, primary_key=True)
    kind                     = Column(Text, nullable=False)  # feedback | event_request | announcement_request | error
    # feedback: Bug/Suggestion/Other category. error: Wrong date or time /
    # Wrong channel / Duplicate posting / Didn't happen as scheduled / Other.
    # Unused (NULL) for event_request/announcement_request.
    error_type               = Column(Text, nullable=True)
    title                    = Column(Text, nullable=False)
    description              = Column(Text, nullable=False)
    tenant_id                = Column(Integer, ForeignKey("tenants.id"), nullable=True)
    # Spec §66.11: an error report references an occurrence only. SET NULL
    # so deleting an event keeps the report (its text still says what was wrong).
    related_occurrence_id    = Column(
        Integer, ForeignKey("event_occurrences.id", ondelete="SET NULL", name="fk_ticket_related_occurrence"),
        nullable=True,
    )
    submitter_contact        = Column(Text, nullable=True)
    status                   = Column(Text, nullable=False, default="open")
    upvote_count             = Column(Integer, nullable=False, default=1)
    created_at               = Column(DateTime(timezone=True), server_default=func.now())
    updated_at               = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    votes = relationship("TicketVote", back_populates="ticket", cascade="all, delete-orphan")
    responses = relationship(
        "TicketResponse", back_populates="ticket", cascade="all, delete-orphan",
        order_by="TicketResponse.created_at, TicketResponse.id",
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('feedback', 'event_request', 'announcement_request', 'error')",
            name="ck_ticket_kind",
        ),
        # Spec §66.10: 'dismissed' hides a ticket from the public board
        # without deleting it; 'done' and 'declined' are the archive.
        CheckConstraint(
            "status IN ('open', 'planned', 'in_progress', 'done', 'declined', 'dismissed')",
            name="ck_ticket_status",
        ),
    )


class TicketVote(Base):
    """One row per (ticket, voter) — see Ticket's docstring and spec §43.2
    for how voter_key is derived (a hash of the client's self-generated,
    localStorage-persisted X-Voter-Id header plus a server-side pepper, so
    the raw client token is never stored verbatim). Voting again for the
    same (ticket, voter) pair deletes the row instead of inserting a
    second one — a toggle, not an accumulator — which is why there is no
    separate 'direction' column."""
    __tablename__ = "ticket_votes"

    id         = Column(Integer, primary_key=True)
    ticket_id  = Column(Integer, ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False)
    voter_key  = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    ticket = relationship("Ticket", back_populates="votes")

    __table_args__ = (
        UniqueConstraint("ticket_id", "voter_key", name="uq_ticket_vote"),
    )


class TicketResponse(Base):
    """A public reply from the team on a ticket (spec §66.10). The author's
    name is not stored: it is read from users.display_name when the board is
    shown, so renaming someone updates their earlier replies. author_user_id
    is SET NULL if the user is ever removed, and the reply then shows as "Team"."""
    __tablename__ = "ticket_responses"

    id             = Column(Integer, primary_key=True)
    ticket_id      = Column(Integer, ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False)
    author_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    body           = Column(Text, nullable=False)
    created_at     = Column(DateTime(timezone=True), server_default=func.now())
    updated_at     = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    ticket = relationship("Ticket", back_populates="responses")
    author = relationship("User", lazy="joined")

    __table_args__ = (
        CheckConstraint("length(body) > 0 AND length(body) <= 2000", name="ck_ticket_response_length"),
    )


# ---------------------------------------------------------------------------
# Events (spec §66): types, events, per-alliance overrides, reminders,
# generated occurrences and the deliveries the engine sends.
# ---------------------------------------------------------------------------

class EventType(Base):
    """A reusable template for events: the one label that replaces any
    separate importance/frequency/category (spec §66 decisions). The
    default_* columns are copied onto a new Event when its own fields are
    left out of the create request; editing a type later never changes
    events that already exist."""
    __tablename__ = "event_types"

    id                       = Column(Integer, primary_key=True)
    kingdom_id               = Column(Integer, ForeignKey("kingdoms.id"), nullable=False)
    name                     = Column(Text, nullable=False)
    color                    = Column(Text, nullable=False, default="#475569")
    default_duration_hours   = Column(Numeric(4, 1), nullable=True)
    default_interval_days    = Column(Integer, nullable=True)
    default_message          = Column(Text, nullable=False, default="")
    default_reminder_minutes = Column(JSON, nullable=False, default=list)
    default_mention_role     = Column(Boolean, nullable=False, default=False)
    sort_order               = Column(Integer, nullable=False, default=0)
    created_at               = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("kingdom_id", "name", name="uq_event_type_name"),
        CheckConstraint(
            "default_duration_hours IS NULL OR default_duration_hours > 0",
            name="ck_event_type_duration_positive",
        ),
        CheckConstraint(
            "default_interval_days IS NULL OR default_interval_days > 0",
            name="ck_event_type_interval_positive",
        ),
    )


class Event(Base):
    """The definition of something that happens once or repeats. An
    announcement is an Event with no duration (spec §66): a calendar entry
    exists exactly when duration_hours is set. series_id is shared by every
    part of a series that was split by a "this and following" edit (§66.4a)
    so the admin list can show them as one."""
    __tablename__ = "events"

    id               = Column(Integer, primary_key=True)
    owning_tenant_id = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    type_id          = Column(Integer, ForeignKey("event_types.id"), nullable=False)
    series_id        = Column(Text, nullable=False, default=lambda: uuid.uuid4().hex)
    name             = Column(Text, nullable=False)
    scope            = Column(Text, nullable=False, default="alliance")
    leadership_only  = Column(Boolean, nullable=False, default=False)
    message          = Column(Text, nullable=False, default="")
    location         = Column(Text, nullable=False, default="")
    start_time_utc   = Column(Time, nullable=False)
    duration_hours   = Column(Numeric(4, 1), nullable=True)
    recurrence_kind  = Column(Text, nullable=False, default="none")
    interval_days    = Column(Integer, nullable=True)
    anchor_date      = Column(Date, nullable=False)
    until_date       = Column(Date, nullable=True)
    mention_role     = Column(Boolean, nullable=False, default=False)
    active           = Column(Boolean, nullable=False, default=True)
    cover_image_data = Column(Text, nullable=True)
    created_at       = Column(DateTime(timezone=True), server_default=func.now())
    updated_at       = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    event_type = relationship("EventType", lazy="joined")
    alliances  = relationship("EventAlliance", back_populates="event", cascade="all, delete-orphan")
    reminders  = relationship("EventReminder", back_populates="event", cascade="all, delete-orphan")
    occurrences = relationship("EventOccurrence", back_populates="event", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("scope IN ('alliance', 'kingdom-wide')", name="ck_event_scope_valid"),
        CheckConstraint("recurrence_kind IN ('none', 'interval_days')", name="ck_event_recurrence_kind"),
        CheckConstraint(
            "(recurrence_kind = 'none' AND interval_days IS NULL) OR "
            "(recurrence_kind = 'interval_days' AND interval_days IS NOT NULL AND interval_days > 0)",
            name="ck_event_recurrence_consistent",
        ),
        CheckConstraint("duration_hours IS NULL OR duration_hours > 0", name="ck_event_duration_positive"),
        CheckConstraint("until_date IS NULL OR until_date >= anchor_date", name="ck_event_until_after_anchor"),
    )


class EventAlliance(Base):
    """Audience. For an 'alliance' event, one row per participating
    alliance (the owner included). For a 'kingdom-wide' event every
    alliance in the Kingdom takes part and rows exist only to carry an
    override. message_override and the destination columns are NULL to
    mean "use the event's message / the alliance's own default"."""
    __tablename__ = "event_alliances"

    id                      = Column(Integer, primary_key=True)
    event_id                = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    tenant_id               = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    message_override        = Column(Text, nullable=True)
    notification_channel_id = Column(Text, nullable=True)
    notification_role_id    = Column(Text, nullable=True)

    event = relationship("Event", back_populates="alliances")

    __table_args__ = (
        UniqueConstraint("event_id", "tenant_id", name="uq_event_alliance"),
    )


class EventReminder(Base):
    """A delivery rule: send a reminder this many minutes before the event
    starts (0 means at the start). Copied from the type's defaults when the
    event is created, so editing a type later does not silently change
    existing events."""
    __tablename__ = "event_reminders"

    id             = Column(Integer, primary_key=True)
    event_id       = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    minutes_before = Column(Integer, nullable=False)

    event = relationship("Event", back_populates="reminders")

    __table_args__ = (
        UniqueConstraint("event_id", "minutes_before", name="uq_event_reminder"),
        CheckConstraint("minutes_before >= 0", name="ck_event_reminder_minutes"),
    )


class EventOccurrence(Base):
    """One dated instance of an Event. The two *_override columns and the
    'cancelled' status are the per-occurrence edits of spec §66.4a;
    regeneration never overwrites an occurrence that has an override or is
    cancelled."""
    __tablename__ = "event_occurrences"

    id                           = Column(Integer, primary_key=True)
    event_id                     = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=False)
    occurrence_date              = Column(Date, nullable=False)
    start_datetime_utc           = Column(DateTime(timezone=True), nullable=False)
    end_datetime_utc             = Column(DateTime(timezone=True), nullable=True)
    status                       = Column(Text, nullable=False, default="scheduled")
    start_datetime_utc_override  = Column(DateTime(timezone=True), nullable=True)
    message_override             = Column(Text, nullable=True)
    generated_at                 = Column(DateTime(timezone=True), server_default=func.now())

    event = relationship("Event", back_populates="occurrences")
    deliveries = relationship("Delivery", back_populates="occurrence", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("event_id", "occurrence_date", name="uq_event_occurrence"),
        CheckConstraint("status IN ('scheduled', 'cancelled')", name="ck_event_occurrence_status"),
    )


class Delivery(Base):
    """One thing the engine must send for one (occurrence, alliance): either
    the Discord Scheduled Event itself or one timed reminder. Replaces
    post_log, announcement target status and reminder_sent (spec §66.4).
    reminder_minutes is a snapshot of the rule that produced the row (-1 for
    a discord_event delivery) rather than a foreign key, so the unique
    constraint works without NULLs and later edits to an event's reminders
    never rewrite delivery history."""
    __tablename__ = "deliveries"

    id                 = Column(Integer, primary_key=True)
    occurrence_id      = Column(Integer, ForeignKey("event_occurrences.id", ondelete="CASCADE"), nullable=False)
    tenant_id          = Column(Integer, ForeignKey("tenants.id"), nullable=False)
    kind               = Column(Text, nullable=False)
    reminder_minutes   = Column(Integer, nullable=False, default=-1)
    due_at_utc         = Column(DateTime(timezone=True), nullable=False)
    status             = Column(Text, nullable=False, default="pending")
    claimed_at_utc     = Column(DateTime(timezone=True), nullable=True)
    discord_event_id   = Column(Text, nullable=True)
    discord_message_id = Column(Text, nullable=True)
    detail             = Column(Text, nullable=True)
    posted_at_utc      = Column(DateTime(timezone=True), nullable=True)

    occurrence = relationship("EventOccurrence", back_populates="deliveries")

    __table_args__ = (
        UniqueConstraint("occurrence_id", "tenant_id", "kind", "reminder_minutes", name="uq_delivery"),
        CheckConstraint("kind IN ('discord_event', 'reminder')", name="ck_delivery_kind"),
        CheckConstraint(
            "status IN ('pending', 'sending', 'posted', 'error', 'cancelled')",
            name="ck_delivery_status",
        ),
        CheckConstraint(
            "(kind = 'discord_event' AND reminder_minutes = -1) OR (kind = 'reminder' AND reminder_minutes >= 0)",
            name="ck_delivery_reminder_minutes",
        ),
        Index("ix_deliveries_status_due", "status", "due_at_utc"),
    )
