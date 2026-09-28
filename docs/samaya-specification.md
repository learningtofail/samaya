# Samaya — Technical Specification

**Repository:** github.com/learningtofail/samaya
**Version:** 1.0.0 · **Deployment:** `ks138.taraka.dev` (LXC `lxc-taraka`, `/opt/taraka`)

## 1. Purpose and Scope

Samaya is a self-hosted, multi-tenant event scheduler for the Kingshot game community (Kingdom 138). Each in-game alliance is a tenant with its own admin access and its own Discord guild; some events are scoped to a single alliance while others fan out across every alliance in a Kingdom. The system generates recurring event occurrences, posts them as Discord Scheduled Events, sends pre-event reminders, publishes public ICS calendar feeds, and delivers ad hoc scheduled text announcements.

## 2. Architecture

### 2.1 Stack

| Layer | Technology |
|---|---|
| API | FastAPI + Uvicorn (single worker) |
| ORM | SQLAlchemy 2.0 (async), asyncpg driver |
| Database | PostgreSQL 16 |
| Scheduling | APScheduler, in-process, UTC |
| Frontend | Static HTML/CSS/JS, no build step, no framework, script tags loaded in dependency order |
| Auth | Discord OAuth2, signed session cookies (`itsdangerous`) |
| Deployment | Docker Compose (app + db); Caddy reverse proxy and Cloudflare Tunnel/Access sit in front, outside this repo |
| Calendar export | `icalendar` (ICS feeds) |
| Discord signature verification | `pynacl` |
| Migrations | Alembic is a listed dependency but has never actually been used; the one production schema migration (`migrate_to_multitenant.py`) was hand-written and run once |

### 2.2 Process model

A single Uvicorn worker is mandatory. APScheduler runs in-process inside the FastAPI lifespan; a second worker would double-run the daily regeneration job and both per-minute jobs.

### 2.3 Startup behavior (`app/main.py`)

The app refuses to boot if `SECRET_KEY`, `DISCORD_OAUTH_CLIENT_ID`, or `DISCORD_OAUTH_CLIENT_SECRET` are unset (fail-closed, replacing a prior shared `X-Admin-Key` scheme). On startup the lifespan handler creates all tables, then for every tenant checks whether occurrence regeneration is overdue (no state row, or last run >25 hours ago) and runs a catch-up regeneration across all tenants if so. It then registers three APScheduler jobs and starts the scheduler.

## 3. Multi-Tenancy Model

- **Kingdom**: a Kingshot game server (e.g. "Kingdom 138"), distinct from a Discord server. Holds multiple Tenants.
- **Tenant**: an alliance. Each has its own Discord guild, admin access, and optionally its own bot token/public key (nullable — falls back to the platform-wide bot/public key if unset). Tenants can share a guild or a bot token.
- **Scope**: every `EventDefinition` is either `alliance` (single tenant) or `kingdom-wide` (fans out to every tenant in the owning tenant's Kingdom). A kingdom-wide event has exactly one `Occurrence` row but produces one independent `PostLog` row per tenant when posted — each tenant's Discord post succeeds, fails, or is cancelled independently.
- Being an `owner` of one alliance does **not** grant kingdom-wide event rights. That is a separate `UserKingdom` grant, issued only by a superadmin.

### 3.1 Tenant scoping conventions

Public routes scope by URL path segment (`/t/{tenant_slug}/...`) because calendar apps consuming ICS feeds cannot send custom headers. Admin API routes scope by request header (`X-Tenant-Slug`) instead — a bridge-period choice from before real per-user access checks existed, kept so route paths didn't need to change again.

## 4. Data Model

| Table | Purpose |
|---|---|
| `kingdoms` | Game server; `name`, unique `slug` |
| `tenants` | Alliance; `kingdom_id`, `slug`, `guild_id`, optional `bot_token`/`public_key`, `color` |
| `users` | Discord-identified person; created only on first OAuth login, never on invite creation; `is_superadmin` flag |
| `user_tenants` | Per-user, per-tenant grant; `role` ∈ {`owner`, `coordinator`} |
| `user_kingdoms` | Per-user, per-kingdom grant of kingdom-wide event rights (flag only, no sub-tiers) |
| `invites` | The only path to creating a `user_tenants`/`user_kingdoms` grant, and the only way a first-time visitor completes OAuth at all; exactly one of `tenant_id`/`kingdom_id` set per `role` ∈ {`owner`, `coordinator`, `kingdom_coordinator`}; supports expiry and revocation |
| `audit_log` | Append-only; `action` ∈ {`create`, `update`, `delete`}; `before`/`after` stored as JSON text, not ORM references, so schema changes don't break old rows read back later. Explicitly not designed for rollback — many logged actions also reach Discord, and restoring a DB snapshot doesn't undo that |
| `event_definitions` | The recurring event template: `owning_tenant_id`, `scope`, `interval_days`, `start_time_utc`, `duration_hours`, `anchor_date`, Discord channel/notification fields, `leadership_only`, `active` |
| `event_tenant_notifications` | Per-tenant notification-channel/role override for a kingdom-wide event; a tenant with no row here still receives the Discord Scheduled Event, just no channel ping |
| `occurrences` | Generated instances of an `event_definitions` row; `post_status`, `reminder_sent`; unique per `(event_id, occurrence_date)` |
| `post_log` | One row per (tenant, occurrence) posted or attempted to Discord; unique per `(tenant_id, event_name, occurrence_date)` |
| `announcements` | Ad hoc scheduled markdown text, not built on the event/occurrence model since the shape differs (no start/end, no recurrence); `status` ∈ {`draft`, `scheduled`, `posted`, `failed`, `cancelled`} |
| `announcement_targets` | One row per (announcement, tenant); independent post per target guild/bot; `post_status` ∈ {`pending`, `posted`, `error`} |
| `scheduler_state` | One row per (tenant, job_name) tracking last run/result/next run, so one tenant's regeneration failure doesn't mask another's |

Every timestamp is UTC. `services/time_utils.ensure_utc()` normalizes `tzinfo` because asyncpg and aiosqlite round-trip `DateTime(timezone=True)` columns differently — a real cross-driver bug that surfaced independently in three files before being consolidated.

## 5. API Surface

### 5.1 Public (unauthenticated, path-scoped)

- `GET /t/{tenant_slug}/api/events`
- `GET /t/{tenant_slug}/events` — renders `static/events.html`
- `GET /t/{tenant_slug}/ics/events.ics` — public ICS feed
- `GET /health`

### 5.2 Auth

- `GET /invite/{token}` — invite claim
- Discord OAuth login and callback (the only path to real access)
- Logout
- `/invite-invalid`, `/auth/login-failed` — static failure pages

### 5.3 Inbound Discord

- `POST /webhooks/discord` — Discord interaction webhook, signature-verified against `PLATFORM_PUBLIC_KEY`. **Known gap:** a tenant with its own custom bot and its own Discord "Interactions Endpoint URL" is not supported by this endpoint.

### 5.4 Admin (`/admin`, session-authenticated via `get_current_user`/`get_current_tenant`)

| Router | Responsibility |
|---|---|
| `ui.py` | Serves `static/admin.html`; deliberately has no auth dependency |
| `me.py` | `GET /api/me` — identity, tenant roles, superadmin flag; drives frontend tab visibility |
| `status.py` | `GET /api/status` |
| `events.py` | Event CRUD; `PUT /api/events/{id}/notification-override` for a non-owning tenant's own ping channel on a kingdom-wide event |
| `occurrences.py` | Occurrence list/patch; Discord post/delete endpoints; kingdom-wide fan-out logic (`_post_to_one_tenant`) |
| `scheduler_control.py` | Manual `preview` and per-tenant `regenerate` |
| `post_log.py` | PostLog list and CSV export, tenant-scoped |
| `discord_config.py` | Per-tenant channel/role listing (bot token/guild config now lives in `tenants.py`) |
| `discord_sync.py` | Discord ↔ PostLog reconciliation |
| `tenants.py` | Kingdom/Tenant CRUD — the "onboard a new alliance" surface; create/update is superadmin-only |
| `invites.py` | Tenant invites (owner-issued) and kingdom-coordinator invites (superadmin-issued), with revocation |
| `announcements.py` | Scheduled-announcement create/list/cancel (delivery logic lives in `scheduler/announcements.py`) |

Manually triggering a regeneration requires a logged-in session and an `X-Tenant-Slug` header — done from the admin UI's Schedule tab, not a bare `curl`. The endpoint is `POST /admin/api/scheduler/regenerate`.

## 6. Background Jobs (`app/scheduler/`)

Three independent jobs, each in its own file (split from an earlier monolithic `jobs.py`). Every public function accepts an optional `session_factory` (default `models.AsyncSessionLocal`) since jobs run outside any HTTP request and can't use FastAPI's `get_db` dependency — tests must inject a test session factory or the job will hit the real database.

| Job | Schedule | Function |
|---|---|---|
| Occurrence regeneration | Daily, UTC 00:00, plus on-demand per tenant | Rebuilds `occurrences` from `event_definitions` + recurrence rules |
| Pre-event reminders | Every minute | `send_pre_event_reminders` |
| Scheduled announcements | Every minute | `send_scheduled_announcements`; independent per-`AnnouncementTarget` delivery |

## 7. Services Layer (`app/services/`)

- `discord_api.py` — bot-token-authenticated Discord REST client (scheduled events, channels, roles, token verify, message send)
- `discord_oauth.py` — app-level OAuth2, kept separate from `discord_api.py` as a genuinely distinct Discord auth mechanism
- `sessions.py` — signed session cookies; `SameSite=Strict` is the CSRF defense (no separate CSRF token scheme); also short-lived OAuth-flow state cookies
- `audit.py` — `log_change`, invoked at mutation points across admin routers
- `time_utils.py` — `ensure_utc` (see §4)
- `recurrence.py` — interval-in-days + anchor-date occurrence math; no named recurrence types by deliberate design
- `validators.py` — shared Pydantic field validators for `EventIn`/`EventPatch`
- `notifications.py` — SMTP email, separate from Discord notifications

## 8. Frontend

Plain HTML/CSS/JS under `app/static/`, served directly by `routers/admin/ui.py` (`admin.html`) and `routers/events.py` (`events.html`). Classic `<script src>` tags loaded in order (not ES modules), so `admin.html`'s script order is the dependency order:

`theme.js` (head, prevents flash of wrong theme) → `common.js` (`api()`, `toast()`, `showView()`, `loadMe()`, `loadTenants()` — load-bearing for everything after it) → one file per admin view (`dashboard.js`, `events.js`, `schedule.js`, `gantt.js`, `postlog.js`, `config.js`, `sync.js`, `access.js`, `platform.js`) → `init.js` (orchestrates initial load and the "logged in, no tenant access yet" empty state).

`access.js` (owner-only invite management) and `platform.js` (superadmin-only Kingdom/Tenant creation) are conditionally shown per `applyRoleVisibility()`.

## 9. Authentication and Authorization

- Discord OAuth2 is the only authentication mechanism and the only way a new user is created.
- Access is granted exclusively through `Invite` consumption — there is no self-service signup.
- Roles: tenant-level `owner`/`coordinator` (via `user_tenants`), and a separate kingdom-level `kingdom_coordinator` flag (via `user_kingdoms`) that does not derive from tenant ownership.
- Superadmins are bootstrapped exclusively from the `SUPERADMIN_DISCORD_IDS` environment variable (comma-separated Discord user IDs) — there is no other path to platform admin access.
- Sessions are signed cookies (`itsdangerous`), not bearer tokens; CSRF protection relies on `SameSite=Strict`.

## 10. Configuration (Environment Variables)

| Variable | Purpose |
|---|---|
| `DB_PASSWORD` | PostgreSQL password |
| `SECRET_KEY` | Session-cookie signing key; boot fails if unset |
| `DISCORD_OAUTH_CLIENT_ID` / `_SECRET` / `_REDIRECT_URI` | Discord OAuth2 app credentials; boot fails if client ID/secret unset |
| `SUPERADMIN_DISCORD_IDS` | Comma-separated Discord user IDs granted superadmin on first login |
| `PLATFORM_BOT_TOKEN` | Fallback bot token for tenants without their own |
| `PLATFORM_PUBLIC_KEY` | Fallback Ed25519 public key for webhook signature verification |
| `NOTIFY_EMAIL`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS` | Outbound email (SMTP, port 587 default) |

`DATABASE_URL` is not in `.env` itself; `docker-compose.yml` constructs it as `postgresql+asyncpg://taraka:${DB_PASSWORD}@db:5432/kingshot_scheduler`.

## 11. Deployment

Docker Compose defines two services:

- **`app`** — built from `./app`, binds `127.0.0.1:8000:8000` only (no direct external exposure), depends on `db` being healthy, mounts `./app` as a volume.
- **`db`** — `postgres:16-alpine`, database `kingshot_scheduler`, user `taraka`, data persisted to `./postgres`, health-checked via `pg_isready`, bound to `127.0.0.1:5432:5432`.

Caddy (reverse proxy) and Cloudflare Tunnel/Access sit in front of the app, outside this repository. The admin UI (`/admin`) is Cloudflare Access–gated at the network edge, with Discord OAuth required within. Public calendar routes are not gated at the edge.

Backup: `/opt/taraka/postgres/` is included in `restic` backups with a `pg_dump` pre-hook.

## 12. Testing (`app/tests/`)

In-memory SQLite via `aiosqlite`, orchestrated through `conftest.py` fixtures; never touches production Postgres.

Key fixtures: `tenant`/`second_tenant` (two tenants in one Kingdom), `test_user` (a superadmin by design, so business-logic tests aren't implicitly testing the permission model too), `client` (authenticated as `test_user`), `make_user_and_client` (factory for a specific non-superadmin user with specific grants, for permission tests).

| Test file | Coverage |
|---|---|
| `test_routes_public.py` | Unauthenticated public routes, cross-tenant isolation |
| `test_routes_events.py` | Admin event CRUD, `scope` validation, kingdom-wide cross-tenant read visibility |
| `test_post_occurrence.py` | Discord-posting race conditions/error paths; kingdom-wide fan-out |
| `test_auth.py` | Session token round-trip/tamper rejection, invite-claim race guard, full HTTP invite-claim flow |
| `test_auth_permissions.py` | Permission model: no-session → 401, no-tenant-access → 403, coordinator vs. owner, kingdom-coordinator independence, superadmin-only CRUD |
| `test_announcements.py` | Creation validation, permission scoping, delivery (partial-failure isolation), cancellation |
| `test_recurrence.py` | Recurrence math, no HTTP |
| `test_validation.py` | Schema/field validation including `scope` |

Run via: `pip install -r app/requirements-dev.txt && cd app && pytest`. `requirements.txt` is runtime-only (what ships in the Docker image); `requirements-dev.txt` layers pytest and the in-memory SQLite driver on top for local/CI use.

## 13. Admin UI — Announcements (Planned)

**Status:** backend complete (`routers/admin/announcements.py`, `scheduler/announcements.py`, `announcements`/`announcement_targets` tables), no admin UI yet. This section specs the missing frontend: a new `js/announcements.js` plus an `<!-- ANNOUNCEMENTS -->` section in `admin.html`, following the same one-file-per-view convention as `events.js`, `schedule.js`, etc.

### 13.1 Relationship to Events

An announcement is deliberately not an event: no start/end time, no recurrence, no Discord Scheduled Event object, no entry on the public calendar or ICS feed. It behaves the same way the Events form's **Leadership Only** checkbox already does for events — a channel post and nothing else — so the Announcements form should read as a sibling of that pattern rather than a new concept: "if Leadership Only turns an event into a channel-only ping, an Announcement is that same channel-only ping without an underlying event at all."

Concretely, this means the Announcements form has **no** fields for: interval, start time, duration, anchor date, notification role/minutes-before, or a scope-to-Discord-Scheduled-Event toggle. It has a title, a body, a send time, and one or more targets.

### 13.2 Form Fields

| Field | Maps to | Notes |
|---|---|---|
| Title | `title` | Plain text, required |
| Body (Markdown) | `body_markdown` | Multi-line, required; hard 2000-character cap enforced client-side and server-side (Discord's own message limit) — show a live character counter, matching the Event form's plain-textarea style for **Description** |
| Send at (UTC) | `scheduled_for` | Date + time input, stored/submitted as ISO 8601 UTC, same "times are always entered and stored in UTC" convention as the Event form's Start Time field; display the user's detected local timezone underneath as the Event form already does |
| Targets | `targets[]` | One or more `{tenant_slug, discord_channel_id}` pairs (see 13.3) |

No `scope` field exists for announcements — targeting is handled entirely by the Targets list, not by an alliance/kingdom-wide toggle.

### 13.3 Target Selection

Each target is a `(tenant, Discord channel)` pair, added one at a time:

- A tenant picker limited to tenants the current user has `UserTenant` access to (owner or coordinator) — mirrors `_check_target_access`'s server-side enforcement, so the UI should not even list tenants the API would reject with 403.
- A Discord channel ID field per selected tenant (digits only, same input pattern as the Event form's Notification Channel field). No live channel-name lookup is required for v1; a coordinator is expected to know their own tenant's channel IDs, consistent with how the Event form's Discord Channel/Notification Channel fields work today.
- "Add another target" lets one announcement fan out to multiple alliances (e.g. a kingdom-wide leadership notice) in a single create call, each delivered and tracked independently per §9's per-target resilience model.
- Superadmins may additionally see and pick any tenant, not just their own.

### 13.4 List / Status View

A table below the form, scoped to `GET /api/announcements` (announcements this tenant is either the owner or a target of):

| Column | Source |
|---|---|
| Title | `title` |
| Scheduled for | `scheduled_for` |
| Status | `status` (`draft` / `scheduled` / `posted` / `failed` / `cancelled`) |
| Targets | one row or badge per target, showing `post_status` (`pending` / `posted` / `error`) and `status_detail` on hover for errors |
| Actions | **Cancel** button, enabled only while `status == "scheduled"`, calling `POST /api/announcements/{id}/cancel` |

Because delivery is independent per target, an announcement with mixed target statuses (e.g. posted to one alliance, errored on another) is a valid, expected state and should render per-target status rather than a single rolled-up badge.

### 13.5 Out of Scope

- Draft/save-without-scheduling workflow. Not needed: creation always goes straight to `status="scheduled"`, and the `draft` value in the schema stays unused.
- Editing a scheduled announcement's body/time before it fires. Not needed: cancel-and-recreate is the intended workflow, matching the API's cancel-only surface (no update endpoint).
- Rich Discord embeds (the API sends plain markdown text via the same `send_channel_message` path used for occurrence pings)

## 14. Admin UI — Combined Multi-Tenant View (Planned)

**Status:** not built. Specs a fix for real friction: a user with access to more than one tenant (e.g. NSR and MOD) must currently select one tenant in the `tenantPicker` and every admin view reloads scoped to just that tenant (`onTenantChange()` in `common.js`) — managing both alliances means repeatedly switching the picker. Public-facing pages are unaffected by this section and keep their existing per-tenant scoping (`/t/{tenant_slug}/events`, `/t/{tenant_slug}/ics/events.ics`); that separation stays, since different alliances' members should keep seeing their own calendar. This mirrors, on the admin side, the reasoning already documented for the public bare `/events`/`/api/events` route: "some members want one shared view rather than switching between alliance pages."

### 14.1 Approach

Add a **combined** mode alongside per-tenant selection, not instead of it — a coordinator who owns only one tenant sees no change, and Config/Access/Platform stay single-tenant by necessity (each is tied to one tenant's own Discord bot, guild, and invite grants, which don't have a meaningful "combined" form).

- `tenantPicker` gets one additional option, **"All my alliances"**, alongside the existing per-tenant entries — selectable only when the user has access to more than one tenant.
- Selecting it sets a combined-mode flag (alongside the existing `getCurrentTenantSlug()`/`setCurrentTenantSlug()` pair) rather than a real tenant slug, since no `X-Tenant-Slug` header value can mean "several."

### 14.2 Views affected

| View | Combined mode behavior |
|---|---|
| Dashboard | Aggregate counts/upcoming items across all accessible tenants |
| Events | List shows every tenant's `event_definitions`, each row tagged with tenant name/color (same `tenant_color` pattern the public combined `/api/events` already returns) |
| Schedule / Gantt | Occurrences from all accessible tenants on one timeline, color-coded by tenant |
| PostLog | Combined list/CSV export across tenants, each row still carrying its own `tenant_id` |
| Config, Access, Platform, Announcements | **No change** — these stay scoped to one explicitly-selected tenant; "All my alliances" is not selectable while one of these views is open, and switching to one of them while in combined mode requires picking a specific tenant first. Announcements is included here (not just Config/Access/Platform) because an announcement is authored by exactly one tenant (`owning_tenant_id`) even when it targets several — the same single-owner shape as an event definition's `owning_tenant_id`, and its list/cancel endpoints were built single-tenant since §13 predates this section |

### 14.3 API changes required

Existing admin list endpoints (`GET /admin/api/events`, `/api/schedule`, `/api/post-log`, etc.) resolve the tenant from `get_current_tenant` (a single `X-Tenant-Slug` header, §5.4/§9). This needs a second mode, not a breaking change to the existing one:

- A reserved header value (e.g. `X-Tenant-Slug: *`) or a separate `all=true` query flag signals combined mode.
- The dependency resolves to "every tenant this user holds `UserTenant` access to" (or every tenant, for a superadmin) instead of one `Tenant` row, and each affected router adds the tenant's `id`/`name`/`color` to its response rows, matching the shape `routers/events.py`'s public combined endpoint already uses.
- **Write endpoints are unaffected and stay single-tenant.** Creating or editing an event, occurrence action, or announcement still requires one explicit owning tenant — combined mode is a read/list convenience, not a way to bulk-create across alliances. The Add Event form's owning-tenant selection (currently implicit, taken from `X-Tenant-Slug`) needs an explicit tenant dropdown when the picker is in combined mode, since there's no longer a single ambient tenant to default to.

### 14.4 Explicitly out of scope

- Merging NSR and MOD into a single `Tenant` row. Rejected: it would collapse their independent Discord guild/bot config, independent `UserTenant` grants, independent `PostLog`/audit isolation, and the kingdom-wide fan-out model (§3) that depends on tenants being distinct rows in the same Kingdom. Combined *viewing* solves the stated problem without that cost.
- Bulk-create/bulk-edit actions across multiple tenants in one submission — each write still targets one tenant, per §14.3.

## 15. Known Gaps and Design Notes

- No per-tenant custom Discord Interactions Endpoint support — all inbound webhooks go through one platform-level endpoint verified against `PLATFORM_PUBLIC_KEY`.
- No named recurrence types (weekly, monthly, etc.) — recurrence is purely interval-in-days from an anchor date.
- Alembic is declared as a dependency but unused; schema changes to date have been hand-written, one-off migration scripts.
- Audit log is append-only and explicitly not a rollback mechanism, since many logged actions have already reached Discord by the time they're logged.
- A separate architecture document ("Samaya Self-Hosted Architecture PRD v2.0", Google Drive) predates the multi-tenant/auth rework and is superseded by the repository's own `CLAUDE.md`, which this specification is derived from alongside direct inspection of the source.
