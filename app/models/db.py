"""The full data model, grouped roughly in the order each layer was
built:

  Kingdom, Tenant                        — multi-tenancy (Phase 1)
  User, UserTenant, UserKingdom, Invite,
  AuditLog                               — real auth (Phase 4)
  Ticket, TicketVote, TicketResponse     — the public feedback board
  EventType, Event, EventAlliance,
  EventReminder, EventOccurrence,
  Delivery                               — events and Discord delivery (spec §66)
  Audience, AudienceDestination,
  AllianceAudience, EventAudience,
  TenantSecondaryServer                  — where messages go (spec §68)
  Player, RedemptionRun, RedemptionResult — the player registry and gift codes (spec §79, §80)

Every table has its own docstring explaining what it's for and why it's
shaped the way it is — this header is just the map."""
import uuid

from sqlalchemy import (
    JSON, Boolean, CheckConstraint, Column, Date, DateTime,
    ForeignKey, Index, Integer, LargeBinary, Numeric, String, Text, Time,
    UniqueConstraint, func, text
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

    # Spec §63: the Kingdom's brand color, a 6-digit hex string. It colors the
    # "All alliances" chip and Kingdom-wide events on the public page. NULL
    # means the built-in Kingdom gold.
    color = Column(Text, nullable=True)

    # Spec §72.2: interface languages for the public pages. NULL default_locale
    # means English; NULL enabled_locales means English only. Values are tags of
    # shipped catalogues (app/i18n/<tag>.json); the admin API validates them and
    # keeps default_locale inside enabled_locales.
    default_locale  = Column(Text, nullable=True)
    enabled_locales = Column(JSON, nullable=True)

    # Spec §71.5: the theme shown when no scheduled window is active. NULL means
    # the shipped defaults. use_alter breaks the kingdoms <-> themes FK cycle.
    default_theme_id = Column(
        Integer, ForeignKey("themes.id", ondelete="RESTRICT", name="fk_kingdom_default_theme", use_alter=True),
        nullable=True,
    )

    # Spec §79.2: the number the game uses for this Kingdom (138 for K138). Gift
    # code redemption sends it as `kid` for every player without their own.
    game_number = Column(Integer, nullable=True)

    tenants = relationship("Tenant", back_populates="kingdom")


class DiscordServer(Base):
    """A Discord guild this app posts to — infrastructure, not an alliance.
    Introduced in spec §25 to make explicit what Tenant.guild_id already
    allowed implicitly: multiple alliances (Tenants) can share one Discord
    server.

    Spec §67: a server belongs to one Kingdom (kingdom_id). That replaces
    the earlier "not scoped to a Kingdom" rule: an alliance's primary and
    secondary servers, and every destination's server, must be in the
    alliance's own Kingdom, so one Kingdom can never reach another's
    servers.

    bot_token/public_key are nullable = falls back to the platform-wide
    bot (see routers.admin.deps.PLATFORM_BOT_TOKEN and
    routers.webhooks.PLATFORM_PUBLIC_KEY). Once alliances share a server,
    they share that server's single bot_token/public_key — there is no
    per-alliance override once sharing is in effect (spec §25, "one bot
    per server")."""
    __tablename__ = "discord_servers"

    id         = Column(Integer, primary_key=True)
    kingdom_id = Column(Integer, ForeignKey("kingdoms.id"), nullable=False)
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
    # The PRIMARY server (spec §67): where this alliance's Discord Scheduled
    # Events are created. Secondary servers live in TenantSecondaryServer.
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
    secondary_servers = relationship("TenantSecondaryServer", lazy="selectin")


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
    # Spec §76: order within its status column on the admin board. NULL means
    # never placed by hand (sorts after placed tickets, by votes). Admin only;
    # no public endpoint returns it.
    position                 = Column(Integer, nullable=True)
    # Spec §76.5: private Markdown notes for the team. Admin only; never in a public payload.
    internal_notes           = Column(Text, nullable=True)
    created_at               = Column(DateTime(timezone=True), server_default=func.now())
    updated_at               = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    votes = relationship("TicketVote", back_populates="ticket", cascade="all, delete-orphan")
    checklist = relationship(
        "TicketChecklistItem", back_populates="ticket", cascade="all, delete-orphan",
        order_by="TicketChecklistItem.position, TicketChecklistItem.id",
    )
    tags = relationship("TicketTag", secondary="ticket_tag_links", order_by="TicketTag.name")
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


class TicketChecklistItem(Base):
    """One line of a ticket's checklist (spec §76.5). One level only, no
    nesting. Admin only: no public endpoint returns these."""
    __tablename__ = "ticket_checklist_items"

    id        = Column(Integer, primary_key=True)
    ticket_id = Column(Integer, ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False, index=True)
    body      = Column(Text, nullable=False)
    done      = Column(Boolean, nullable=False, default=False)
    position  = Column(Integer, nullable=False, default=0)

    ticket = relationship("Ticket", back_populates="checklist")

    __table_args__ = (
        CheckConstraint("length(body) > 0 AND length(body) <= 200", name="ck_ticket_checklist_length"),
    )


class TicketTag(Base):
    """An internal, color-coded label shared by every admin (spec §76.5).
    Tickets are Kingdom-wide, so tags are too. Not the public `kind`."""
    __tablename__ = "ticket_tags"

    id         = Column(Integer, primary_key=True)
    name       = Column(Text, nullable=False)
    color      = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("name", name="uq_ticket_tag_name"),
        CheckConstraint("length(name) > 0 AND length(name) <= 24", name="ck_ticket_tag_name_length"),
    )


class TicketTagLink(Base):
    """Which tags a ticket carries (many to many)."""
    __tablename__ = "ticket_tag_links"

    ticket_id = Column(Integer, ForeignKey("tickets.id", ondelete="CASCADE"), primary_key=True)
    tag_id    = Column(Integer, ForeignKey("ticket_tags.id", ondelete="CASCADE"), primary_key=True)


class TicketColumnLimit(Base):
    """Work-in-progress limit for one ticket board column (spec §76.6). No row means no limit."""
    __tablename__ = "ticket_column_limits"

    status    = Column(Text, primary_key=True)
    wip_limit = Column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint("wip_limit >= 1 AND wip_limit <= 999", name="ck_ticket_column_limit_range"),
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
    # Spec §83: delete earlier reminders once a later one posts, and all of them after the event.
    clean_up_reminders = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    # Spec §87: the Notify me and I'm in buttons. On for a new event, off for events that predate the feature.
    signup_enabled   = Column(Boolean, nullable=False, default=True, server_default=text("false"))
    signup_mention   = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    rsvp_enabled     = Column(Boolean, nullable=False, default=True, server_default=text("false"))
    signup_group_id  = Column(Integer, ForeignKey("signup_groups.id", ondelete="SET NULL"), nullable=True)
    active           = Column(Boolean, nullable=False, default=True)
    cover_image_data = Column(Text, nullable=True)
    created_at       = Column(DateTime(timezone=True), server_default=func.now())
    updated_at       = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    event_type = relationship("EventType", lazy="joined")
    alliances  = relationship("EventAlliance", back_populates="event", cascade="all, delete-orphan")
    reminders  = relationship("EventReminder", back_populates="event", cascade="all, delete-orphan")
    occurrences = relationship("EventOccurrence", back_populates="event", cascade="all, delete-orphan")
    # Spec §68.1: the Audiences chosen for the event (adds and opt-outs of the alliances' defaults).
    audience_links = relationship("EventAudience", cascade="all, delete-orphan")

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
    # Legacy (spec §67.8): replaced by destinations. No longer read or
    # written; the closing revision drops both columns.
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
    # This reminder's own text (spec §77). NULL means "use the event's message".
    message        = Column(Text, nullable=True)

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
    # Spec §67.1. destination_id is null for discord_event rows. guild_id and
    # channel_id snapshot where a reminder went, so history survives edits to
    # (or deletion of) the destination. merged_into_id points at the delivery
    # that carried the real send when several destinations share one channel.
    destination_id     = Column(Integer, ForeignKey("audience_destinations.id", ondelete="SET NULL"), nullable=True)
    guild_id           = Column(Text, nullable=False, default="", server_default="")
    channel_id         = Column(Text, nullable=False, default="", server_default="")
    merged_into_id     = Column(Integer, ForeignKey("deliveries.id", ondelete="SET NULL"), nullable=True)
    # Spec §83: when Samaya deleted this reminder's Discord message, or why it could not.
    message_deleted_at = Column(DateTime(timezone=True), nullable=True)
    cleanup_error      = Column(Text, nullable=True)
    # Spec §87.10: the "I'm in" count last written onto this reminder, and when (or when it failed).
    rsvp_count_shown   = Column(Integer, nullable=True)
    rsvp_base_content  = Column(Text, nullable=True)  # the reminder text before the count line, so an edit can rebuild it
    rsvp_edited_at     = Column(DateTime(timezone=True), nullable=True)
    rsvp_edit_error_at = Column(DateTime(timezone=True), nullable=True)

    occurrence = relationship("EventOccurrence", back_populates="deliveries")
    destination = relationship("AudienceDestination", lazy="joined")
    tenant = relationship("Tenant", lazy="joined")

    __table_args__ = (
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
        # Spec §68.1: a reminder is unique per destination and alliance, a Discord event per alliance.
        Index("uq_delivery_reminder", "occurrence_id", "destination_id", "tenant_id", "reminder_minutes", unique=True,
              postgresql_where=text("kind = 'reminder'"), sqlite_where=text("kind = 'reminder'")),
        Index("uq_delivery_event", "occurrence_id", "tenant_id", unique=True,
              postgresql_where=text("kind = 'discord_event'"), sqlite_where=text("kind = 'discord_event'")),
    )


class TenantSecondaryServer(Base):
    """Spec §67: a server an alliance may post notifications to besides its
    primary one (Tenant.server_id). Informational plus the allowed list for
    that alliance's destinations; it never creates a Scheduled Event.
    Sharing is declared per alliance and never inferred."""
    __tablename__ = "tenant_secondary_servers"

    tenant_id = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True)
    server_id = Column(Integer, ForeignKey("discord_servers.id"), primary_key=True)

    server = relationship("DiscordServer", lazy="joined")


class Audience(Base):
    """Spec §68.1: a named, reusable list of destinations owned by the Kingdom.
    An alliance uses it through an AllianceAudience link; an event selects
    one or more. leadership_only reserves it for leadership events (and
    leadership events use nothing else)."""
    __tablename__ = "audiences"

    id              = Column(Integer, primary_key=True)
    kingdom_id      = Column(Integer, ForeignKey("kingdoms.id"), nullable=False)
    label           = Column(Text, nullable=False)
    leadership_only = Column(Boolean, nullable=False, default=False)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())

    destinations = relationship(
        "AudienceDestination", lazy="selectin", cascade="all, delete-orphan", order_by="AudienceDestination.id",
        back_populates="audience",
    )
    links = relationship("AllianceAudience", lazy="selectin", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("kingdom_id", "label", name="uq_audience_label"),
    )


class AudienceDestination(Base):
    """Spec §68.1: one place an Audience posts to: a server, a channel and an
    optional role to mention."""
    __tablename__ = "audience_destinations"

    id          = Column(Integer, primary_key=True)
    audience_id = Column(Integer, ForeignKey("audiences.id", ondelete="CASCADE"), nullable=False)
    server_id   = Column(Integer, ForeignKey("discord_servers.id"), nullable=False)
    channel_id  = Column(Text, nullable=False)
    role_id     = Column(Text, nullable=False, default="", server_default="")
    # Spec §81: a destination that keeps answering 403/404 is paused until a
    # coordinator resumes it.
    consecutive_failures = Column(Integer, nullable=False, default=0, server_default="0")
    paused_at            = Column(DateTime(timezone=True))
    pause_reason         = Column(Text)
    # Spec §82: an optional schedule board, one Discord message edited in place.
    board_scope        = Column(Text)  # null (off), "kingdom" or "alliance"
    board_tenant_id    = Column(Integer, ForeignKey("tenants.id", ondelete="SET NULL"))
    board_message_id   = Column(Text)
    board_hash         = Column(String(64))
    board_refreshed_at = Column(DateTime(timezone=True))
    board_error        = Column(Text)

    server   = relationship("DiscordServer", lazy="joined")
    audience = relationship("Audience", lazy="joined", back_populates="destinations")

    __table_args__ = (
        UniqueConstraint("audience_id", "server_id", "channel_id", "role_id", name="uq_audience_destination"),
        CheckConstraint("channel_id <> ''", name="ck_audience_destination_channel_set"),
        CheckConstraint("board_scope IS NULL OR board_scope IN ('kingdom', 'alliance')",
                        name="ck_audience_destination_board_scope"),
    )


class AllianceAudience(Base):
    """Spec §68.1: an alliance may post to an Audience. post_by_default
    decides whether the alliance's events post there without being asked."""
    __tablename__ = "alliance_audiences"

    tenant_id       = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True)
    audience_id     = Column(Integer, ForeignKey("audiences.id", ondelete="CASCADE"), primary_key=True)
    post_by_default = Column(Boolean, nullable=False, default=True)


class EventAudience(Base):
    """Spec §68.1: a per-event change to one Audience's default.
    included=True adds an Audience that is not on by default;
    included=False opts out of one that is."""
    __tablename__ = "event_audiences"

    event_id    = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    audience_id = Column(Integer, ForeignKey("audiences.id", ondelete="RESTRICT"), primary_key=True)
    included    = Column(Boolean, nullable=False)


class ThemeAsset(Base):
    """Spec §71.4: a normalised banner image, stored once per content hash and
    served at /theme-assets/{sha256}.webp. Rows hold bytes; audit rows never do."""
    __tablename__ = "theme_assets"

    id         = Column(Integer, primary_key=True)
    kingdom_id = Column(Integer, ForeignKey("kingdoms.id"), nullable=False)
    sha256     = Column(Text, nullable=False)
    content    = Column(LargeBinary, nullable=False)
    width      = Column(Integer, nullable=False)
    height     = Column(Integer, nullable=False)
    # Where the image came from and its license (spec §71.15); required on upload.
    credit     = Column(Text, nullable=False, default="", server_default="")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("kingdom_id", "sha256", name="uq_theme_asset_hash"),
    )


class Theme(Base):
    """Spec §71.1: a named set of validated colors, catalogue fonts, an optional
    banner and optional text overrides. Data, never CSS. Contrast rules are
    enforced on save (services/theme_rules.py)."""
    __tablename__ = "themes"

    id              = Column(Integer, primary_key=True)
    kingdom_id      = Column(Integer, ForeignKey("kingdoms.id"), nullable=False)
    name            = Column(Text, nullable=False)
    bg              = Column(Text, nullable=False)
    accent          = Column(Text, nullable=False)
    accent_text     = Column(Text, nullable=False)
    primary         = Column(Text, nullable=False)
    font_heading    = Column(Text, nullable=True)
    font_body       = Column(Text, nullable=True)
    font_numerals   = Column(Text, nullable=True)
    banner_asset_id = Column(Integer, ForeignKey("theme_assets.id", ondelete="RESTRICT"), nullable=True)
    banner_overlay  = Column(Numeric(3, 2), nullable=False, default=0.96, server_default=text("0.96"))
    # Spec §71.15: header art, the wash color over the hero banner (banner_overlay is its
    # strength), a hover tint, corner radius and art band height. NULL means the shipped value.
    header_asset_id        = Column(Integer, ForeignKey("theme_assets.id", ondelete="RESTRICT", name="fk_theme_header_asset"), nullable=True)
    header_mobile_asset_id = Column(Integer, ForeignKey("theme_assets.id", ondelete="RESTRICT", name="fk_theme_header_mobile_asset"), nullable=True)
    hero_wash       = Column(Text, nullable=False, default="#FFFFFF", server_default="#FFFFFF")
    tint            = Column(Text, nullable=True)
    radius          = Column(Integer, nullable=True)
    art_height      = Column(Integer, nullable=True)
    copy            = Column(JSON, nullable=True)
    archived        = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_at      = Column(DateTime(timezone=True), server_default=func.now())
    updated_at      = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("kingdom_id", "name", name="uq_theme_name"),
        CheckConstraint("banner_overlay >= 0.90 AND banner_overlay <= 1.00", name="ck_theme_overlay"),
        CheckConstraint("radius IS NULL OR (radius >= 4 AND radius <= 14)", name="ck_theme_radius"),
        CheckConstraint("art_height IS NULL OR (art_height >= 240 AND art_height <= 360)", name="ck_theme_art_height"),
    )


class ScheduledTheme(Base):
    """Spec §71.5: a theme shown over a UTC window. Highest priority wins,
    then the latest start."""
    __tablename__ = "scheduled_themes"

    id             = Column(Integer, primary_key=True)
    kingdom_id     = Column(Integer, ForeignKey("kingdoms.id"), nullable=False)
    theme_id       = Column(Integer, ForeignKey("themes.id", ondelete="RESTRICT"), nullable=False)
    start_utc      = Column(DateTime(timezone=True), nullable=False)
    end_utc        = Column(DateTime(timezone=True), nullable=False)
    priority_level = Column(Integer, nullable=False, default=50, server_default=text("50"))

    __table_args__ = (
        CheckConstraint("start_utc < end_utc", name="ck_scheduled_theme_window"),
        CheckConstraint("priority_level >= 0 AND priority_level <= 1000", name="ck_scheduled_theme_priority"),
        Index("ix_scheduled_themes_window", "kingdom_id", "start_utc", "end_utc"),
    )


class Player(Base):
    """Spec §80.3: the player registry every player-facing feature reads. A
    player is an in-game ID (`fid`) in one alliance. Samaya cannot verify who
    owns an ID, so only leaders write this table. `kid` overrides the Kingdom's
    game_number for a transferred player. `name` and `note` are typed by a
    leader and optional; the game no longer lets Samaya fetch them."""
    __tablename__ = "players"

    id         = Column(Integer, primary_key=True)
    kingdom_id = Column(Integer, ForeignKey("kingdoms.id", ondelete="CASCADE"), nullable=False)
    tenant_id  = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    fid        = Column(String(20), nullable=False)
    kid        = Column(Integer, nullable=True)
    name       = Column(String(60), nullable=True)
    note       = Column(String(40), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("kingdom_id", "fid", name="uq_player_kingdom_fid"),
        CheckConstraint("fid <> ''", name="ck_player_fid_set"),
        Index("ix_players_tenant", "tenant_id"),
    )


class RedemptionRun(Base):
    """Spec §79.3: one gift code being redeemed for the players of one or more
    alliances. The tick job advances it; state lives here so a restart loses
    nothing. A Kingdom has at most one active run at a time."""
    __tablename__ = "redemption_runs"

    id          = Column(Integer, primary_key=True)
    kingdom_id  = Column(Integer, ForeignKey("kingdoms.id", ondelete="CASCADE"), nullable=False)
    code        = Column(Text, nullable=False)
    status      = Column(Text, nullable=False, default="queued")
    stop_reason = Column(Text, nullable=True)
    created_by  = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at  = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    started_at  = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    results = relationship("RedemptionResult", cascade="all, delete-orphan", back_populates="run")

    __table_args__ = (
        CheckConstraint("status IN ('queued', 'running', 'done', 'stopped', 'cancelled')", name="ck_redemption_run_status"),
    )


class RedemptionResult(Base):
    """Spec §79.3: one distinct player ID within one run. `status` is
    pending, in_flight or cooling while the run is working, then the final
    outcome key (services/giftcode_client.py)."""
    __tablename__ = "redemption_results"

    id              = Column(Integer, primary_key=True)
    run_id          = Column(Integer, ForeignKey("redemption_runs.id", ondelete="CASCADE"), nullable=False)
    tenant_id       = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    player_id       = Column(Integer, ForeignKey("players.id", ondelete="CASCADE"), nullable=True)
    fid             = Column(String(20), nullable=False)
    kid             = Column(Integer, nullable=False)
    status          = Column(Text, nullable=False, default="pending")
    message         = Column(Text, nullable=True)
    attempts        = Column(Integer, nullable=False, default=0)
    cooldowns       = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    updated_at      = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    run = relationship("RedemptionRun", back_populates="results")

    __table_args__ = (
        UniqueConstraint("run_id", "fid", name="uq_redemption_result_fid"),
        Index("ix_redemption_results_run_status", "run_id", "status"),
    )


class TimePoll(Base):
    """Spec §84: a leader proposes 2 to 6 UTC time slots, players tap the ones
    that work in Discord, Samaya tallies. Nothing is applied automatically.
    tenant_id is null for a Kingdom-wide poll."""
    __tablename__ = "time_polls"

    id             = Column(Integer, primary_key=True)
    kingdom_id     = Column(Integer, ForeignKey("kingdoms.id", ondelete="CASCADE"), nullable=False)
    tenant_id      = Column(Integer, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True)
    title          = Column(String(80), nullable=False)
    occurrence_id  = Column(Integer, ForeignKey("event_occurrences.id", ondelete="SET NULL"), nullable=True)
    status         = Column(Text, nullable=False, default="open")
    closes_at      = Column(DateTime(timezone=True), nullable=False)
    closed_at      = Column(DateTime(timezone=True), nullable=True)
    winner_slot_id = Column(Integer, ForeignKey("time_poll_slots.id", ondelete="SET NULL", use_alter=True,
                                                name="fk_time_polls_winner_slot"), nullable=True)
    created_by     = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at     = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    slots = relationship("TimePollSlot", lazy="selectin", order_by="TimePollSlot.position",
                         primaryjoin="TimePoll.id == TimePollSlot.poll_id", cascade="all, delete-orphan",
                         foreign_keys="TimePollSlot.poll_id", back_populates="poll")
    messages = relationship("TimePollMessage", lazy="selectin", order_by="TimePollMessage.id",
                            cascade="all, delete-orphan", back_populates="poll")

    __table_args__ = (
        CheckConstraint("status IN ('open', 'closed', 'cancelled')", name="ck_time_poll_status"),
        Index("ix_time_polls_status_closes", "status", "closes_at"),
    )


class TimePollSlot(Base):
    __tablename__ = "time_poll_slots"

    id        = Column(Integer, primary_key=True)
    poll_id   = Column(Integer, ForeignKey("time_polls.id", ondelete="CASCADE"), nullable=False)
    starts_at = Column(DateTime(timezone=True), nullable=False)
    position  = Column(Integer, nullable=False, default=0)

    poll = relationship("TimePoll", back_populates="slots", foreign_keys=[poll_id])

    __table_args__ = (UniqueConstraint("poll_id", "starts_at", name="uq_time_poll_slot"),)


class TimePollVote(Base):
    """One vote. voter_hash is HMAC-SHA256(SECRET_KEY, "poll_id:discord_user_id"):
    enough to toggle and count a person once, not enough to list or contact them."""
    __tablename__ = "time_poll_votes"

    slot_id    = Column(Integer, ForeignKey("time_poll_slots.id", ondelete="CASCADE"), primary_key=True)
    voter_hash = Column(String(64), primary_key=True)


class TimePollMessage(Base):
    """One posted Discord message of a poll (one per channel)."""
    __tablename__ = "time_poll_messages"

    id             = Column(Integer, primary_key=True)
    poll_id        = Column(Integer, ForeignKey("time_polls.id", ondelete="CASCADE"), nullable=False)
    destination_id = Column(Integer, ForeignKey("audience_destinations.id", ondelete="SET NULL"), nullable=True)
    guild_id       = Column(Text, nullable=False)
    channel_id     = Column(Text, nullable=False)
    message_id     = Column(Text, nullable=False)
    content_hash   = Column(String(64), nullable=True)
    error          = Column(Text, nullable=True)
    refreshed_at   = Column(DateTime(timezone=True), nullable=True)

    poll = relationship("TimePoll", back_populates="messages")

    __table_args__ = (UniqueConstraint("poll_id", "channel_id", name="uq_time_poll_message_channel"),)


# ---------------------------------------------------------------------------
# Notify me and I'm in (spec §87)
# ---------------------------------------------------------------------------

class SignupGroup(Base):
    """A Kingdom's set of events a player can only join one of. `exclusive_roles`
    keeps a player to one Notify me role at a time; `attendance_window` limits "I'm in" to
    one occurrence per day or per Monday-start UTC week across the group."""
    __tablename__ = "signup_groups"

    id              = Column(Integer, primary_key=True)
    kingdom_id      = Column(Integer, ForeignKey("kingdoms.id", ondelete="CASCADE"), nullable=False)
    name            = Column(String(80), nullable=False)
    exclusive_roles = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    attendance_window = Column(String(8), nullable=False, default="none", server_default="none")

    __table_args__ = (
        UniqueConstraint("kingdom_id", "name", name="uq_signup_group_name"),
        CheckConstraint("attendance_window IN ('none', 'day', 'week')", name="ck_signup_group_window"),
    )


class SignupRole(Base):
    """The Discord role behind Notify me for one server: mapped to a recurring
    event, or to an event type as the fallback (exactly one of the two)."""
    __tablename__ = "signup_roles"

    id                 = Column(Integer, primary_key=True)
    event_id           = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), nullable=True)
    type_id            = Column(Integer, ForeignKey("event_types.id", ondelete="CASCADE"), nullable=True)
    server_id          = Column(Integer, ForeignKey("discord_servers.id", ondelete="CASCADE"), nullable=False)
    role_id            = Column(Text, nullable=False)
    role_name          = Column(Text, nullable=False, default="", server_default="")
    created_by_samaya  = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    last_error         = Column(Text, nullable=True)
    last_error_at      = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "(event_id IS NOT NULL AND type_id IS NULL) OR (event_id IS NULL AND type_id IS NOT NULL)",
            name="ck_signup_role_one_scope",
        ),
        CheckConstraint("role_id <> ''", name="ck_signup_role_id_set"),
        Index("uq_signup_role_event", "event_id", "server_id", unique=True,
              postgresql_where=text("event_id IS NOT NULL"), sqlite_where=text("event_id IS NOT NULL")),
        Index("uq_signup_role_type", "type_id", "server_id", unique=True,
              postgresql_where=text("type_id IS NOT NULL"), sqlite_where=text("type_id IS NOT NULL")),
    )


class EventSubscription(Base):
    """A player who tapped Notify me for a Discord role. Keyed by the role, not
    the mapping row, so a split series or a relinked role keeps its subscribers.
    voter_hash is HMAC-SHA256(SECRET_KEY, "sub:server_id:role_id:discord_user_id")."""
    __tablename__ = "event_subscriptions"

    server_id     = Column(Integer, ForeignKey("discord_servers.id", ondelete="CASCADE"), primary_key=True)
    role_id       = Column(Text, primary_key=True)
    voter_hash    = Column(String(64), primary_key=True)
    subscribed_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


class OccurrenceRsvp(Base):
    """"I'm in" for one occurrence, keyed by the occurrence's nominal date so it
    survives occurrences being recreated or moved (spec §87.10). voter_hash scope
    is the attendance group when there is one, else the event."""
    __tablename__ = "occurrence_rsvps"

    event_id        = Column(Integer, ForeignKey("events.id", ondelete="CASCADE"), primary_key=True)
    occurrence_date = Column(Date, primary_key=True)
    voter_hash      = Column(String(64), primary_key=True)
    created_at      = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (Index("ix_occurrence_rsvps_voter", "voter_hash", "occurrence_date"),)
