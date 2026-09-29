# Samaya — Technical Specification

**Repository:** github.com/learningtofail/samaya
**Version:** 1.26.0 · **Deployment:** `ks138.taraka.dev` (LXC `lxc-taraka`, `/opt/taraka`)

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
| `user_tenants` | Per-user, per-tenant grant; `role` ∈ {`owner`, `coordinator`, `viewer`} — see §31 for `viewer` |
| `user_kingdoms` | Per-user, per-kingdom grant of kingdom-wide event rights (flag only, no sub-tiers) |
| `invites` | The only path to creating a `user_tenants`/`user_kingdoms` grant, and the only way a first-time visitor completes OAuth at all; exactly one of `tenant_id`/`kingdom_id` set per `role` ∈ {`owner`, `coordinator`, `viewer`, `kingdom_coordinator`}; supports expiry and revocation |
| `audit_log` | Append-only; `action` ∈ {`create`, `update`, `delete`}; `before`/`after` stored as JSON text, not ORM references, so schema changes don't break old rows read back later. Explicitly not designed for rollback — many logged actions also reach Discord, and restoring a DB snapshot doesn't undo that |
| `event_definitions` | The recurring event template: `owning_tenant_id`, `scope`, `interval_days`, `start_time_utc`, `duration_hours`, `anchor_date`, Discord channel/notification fields, `leadership_only`, `active` |
| `event_tenant_notifications` | Per-tenant notification-channel/role override for a kingdom-wide event; a tenant with no row here still receives the Discord Scheduled Event, just no channel ping |
| `occurrences` | Generated instances of an `event_definitions` row; `post_status`, `reminder_sent`; unique per `(event_id, occurrence_date)` |
| `post_log` | One row per (tenant, occurrence) posted or attempted to Discord; unique per `(tenant_id, event_name, occurrence_date)` |
| `announcements` | Ad hoc scheduled markdown text, not built on the event/occurrence model since the shape differs (no start/end); `status` ∈ {`draft`, `scheduled`, `posted`, `failed`, `cancelled`}; `leadership_only` (categorization/display flag, mirrors `event_definitions`); `recurring` + `interval_days` (nullable, only meaningful when `recurring` is true) — see §13.5 |
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
| `status.py` | `GET /api/status`; `GET /api/delivery-health` (§31.2) |
| `events.py` | Event CRUD; `PUT /api/events/{id}/notification-override` for a non-owning tenant's own ping channel on a kingdom-wide event; `GET /api/events/export.csv` / `POST /api/events/import.csv` for bulk alliance-scope events (§32.1) |
| `occurrences.py` | Occurrence list/patch; Discord post/delete endpoints; kingdom-wide fan-out logic (`_post_to_one_tenant`) |
| `scheduler_control.py` | Manual `preview` and per-tenant `regenerate` |
| `post_log.py` | PostLog list and CSV export, tenant-scoped |
| `discord_config.py` | Per-tenant channel/role listing (bot token/guild config now lives in `tenants.py`) |
| `discord_sync.py` | Discord ↔ PostLog reconciliation |
| `tenants.py` | Kingdom/Tenant CRUD — the "onboard a new alliance" surface; create/update is superadmin-only |
| `invites.py` | Tenant invites (owner-issued) and kingdom-coordinator invites (superadmin-issued), with revocation |
| `announcements.py` | Scheduled-announcement create/list/cancel/retry-failed-targets (§30.1; delivery logic lives in `scheduler/announcements.py`) |
| `audit_log.py` | `GET /api/audit-log`, owner-only (§31.1) |

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

## 13. Admin UI — Announcements

**Status:** Implemented and deployed. All three revisions specced below (recurring delivery, the leadership/general distinction, live Discord channel dropdowns) are live: `routers/admin/announcements.py`, `scheduler/announcements.py`, the `leadership_only`/`recurring`/`interval_days` columns on `announcements` (§4), `admin.html`'s Announcements tab, and `js/announcements.js`.

### 13.1 Relationship to Events

An announcement is deliberately not an event: no start/end time, no Discord Scheduled Event object, no entry on the public calendar or ICS feed. It behaves the same way the Events form's **Leadership Only** checkbox already does for events — a channel post and nothing else — so the Announcements form reads as a sibling of that pattern rather than a new concept: "if Leadership Only turns an event into a channel-only ping, an Announcement is that same channel-only ping without an underlying event at all."

**Single creation process.** Events was revised (see the admin.html/events.js changelog) to drop its second "+ Add Leadership Event" button in favor of one "+ Add Event" button with a **Leadership Only** checkbox inside the modal — both buttons always called the same `openEventModal()` function, so the second button was a convenience shortcut, not a separate process. Announcements follows the same one-button-plus-checkbox pattern from the start: a single "+ New Announcement" button, with **Leadership Only** as a checkbox inside the one modal, not a second button.

The Announcements form still has **no** fields for: duration, notification role/minutes-before, or a scope-to-Discord-Scheduled-Event toggle. It has a title, a body, a send time, a recurrence toggle, a leadership/general flag, and one or more targets.

### 13.2 Form Fields

| Field | Maps to | Notes |
|---|---|---|
| Title | `title` | Plain text, required |
| Body (Markdown) | `body_markdown` | Multi-line, required; hard 2000-character cap enforced client-side and server-side (Discord's own message limit) — show a live character counter, matching the Event form's plain-textarea style for **Description** |
| Send Date (UTC) + Send Time (UTC) | `scheduled_for` | Two separate inputs — a `type="date"` field and a plain-text `HH:MM` field (24-hour, validated client-side with the same regex as the Event form's Start Time field) — joined client-side into one ISO 8601 UTC string before submit. Originally a single native `datetime-local` input; split apart because that control's on-screen display (12-hour AM/PM vs. 24-hour) follows the browser's OS/locale setting with no reliable cross-browser way to force 24-hour, whereas a plain text field is fully under the app's control — same reasoning already applied to the Event form's Start Time field, which never used a native time input for this reason. Stays UTC-entry regardless of §15's header time zone control — see §15.2 |
| Recurring | `recurring` | Checkbox. When checked, reveals **Repeat every (days)** |
| Repeat every (days) | `interval_days` | Integer, required only when Recurring is checked; hidden and cleared otherwise, mirroring how the Event form's fields behave conditionally (e.g. Leadership Only's note block) |
| Leadership Only | `leadership_only` | Checkbox, mirrors `event_definitions.leadership_only` exactly. Categorization/display flag only (see 13.3) — it does not restrict who can create one or gate which channels are pickable |
| Targets | `targets[]` | One or more `{tenant_slug, discord_channel_id}` pairs (see 13.3) |

No `scope` field exists for announcements — targeting is handled entirely by the Targets list, not by an alliance/kingdom-wide toggle.

**Past-date rejection.** `POST /api/announcements` rejects a `scheduled_for` at or before the current time with 422 (`routers/admin/announcements.py`), checked both server-side and client-side (`saveAnnouncement()` in `js/announcements.js`, immediately after the HH:MM format check, using the same joined UTC timestamp the payload will submit). There is no equivalent check on `EventDefinition.anchor_date`: an event's anchor date is legitimately allowed to be in the past — that's how a recurring event's occurrences are generated forward from it — and `post_occurrence`'s existing "can't post an occurrence starting less than 15 minutes from now" guard already prevents a stale/past occurrence from actually going out, which is the equivalent protection for events.

### 13.3 Target Selection

Each target is a `(tenant, Discord channel)` pair, added one at a time:

- A tenant picker limited to tenants the current user has `UserTenant` access to (owner or coordinator) — mirrors `check_target_access`'s server-side enforcement (`routers/admin/deps.py`, shared with Events' target validation — see §20), so the UI should not even list tenants the API would reject with 403. Superadmins may additionally see and pick any tenant, not just their own.
- **Live Discord channel dropdown**, replacing the earlier free-text channel ID field. No backend change is needed: `GET /api/discord/channels` already exists and already scopes to whichever tenant is named in the request's `X-Tenant-Slug` header — exactly how the Event form's own channel dropdowns work today. Each target row, on choosing or changing its tenant, fetches that tenant's channel list using the same per-request tenant-header-override mechanism already built for combined-mode writes (`api()`'s `tenantOverride` parameter in `common.js`) and populates a real `<select>`, falling back to the manual text field only if the fetch fails (matching `populateDiscordFields()`'s existing fallback behavior in the Event modal).
- `leadership_only` does not change which channels are offered or which tenants are selectable — the same channel list is shown either way. It is purely how the announcement is categorized once created (13.4), not how it is targeted.
- "Add another target" lets one announcement fan out to multiple alliances (e.g. a kingdom-wide leadership notice) in a single create call, each delivered and tracked independently per §9's per-target resilience model.

### 13.4 List / Status View

A table below the form, scoped to `GET /api/announcements` (announcements this tenant is either the owner or a target of):

| Column | Source |
|---|---|
| Title | `title`, with a 👑/🛡️ badge from `leadership_only`, matching the icon convention already used on the Events, Schedule, and Post Log tables |
| Scheduled for | `scheduled_for` (next fire time; for a recurring announcement this advances after each send — see 13.5), shown in the same dual UTC + local-time format as events (§15.2) |
| Recurring | A badge or icon when `recurring` is true, showing the `interval_days` value (e.g. "every 7 days") |
| Status | `status` (`draft` / `scheduled` / `posted` / `failed` / `cancelled`) |
| Targets | one row or badge per target, showing `post_status` (`pending` / `posted` / `error`) and `status_detail` on hover for errors |
| Actions | **Cancel** button, enabled only while `status == "scheduled"`, calling `POST /api/announcements/{id}/cancel`. For a recurring announcement this cancels the whole series, not just the next occurrence — see 13.6 |

Because delivery is independent per target, an announcement with mixed target statuses (e.g. posted to one alliance, errored on another) is a valid, expected state and should render per-target status rather than a single rolled-up badge.

### 13.5 Recurring Delivery Semantics

The smallest change that fits the existing pattern, rather than a parallel occurrence-generation system: `Announcement` gains `recurring` (bool, default false) and `interval_days` (int, nullable), reusing the same "interval in days from an anchor" concept `EventDefinition` already uses for events.

In `scheduler/announcements.py`'s per-minute job, when a due announcement has `recurring = true`:

- Instead of going terminal (`status` → `posted`/`failed`) once every target has been attempted, the job resets all of that announcement's `AnnouncementTarget` rows back to `post_status = "pending"`, advances `scheduled_for` by `interval_days`, and leaves `status` as `"scheduled"` — so it fires again on its next due cycle, the same way it fired the first time.
- A non-recurring announcement (`recurring = false`, the default, matching every announcement created before this change) behaves exactly as today: terminal `posted`/`failed` after the one send.
- Cancelling a recurring announcement (13.4's Cancel button) ends the series outright (`status → "cancelled"`), not just the next occurrence — there is no "cancel just this one instance and keep the series running" in v1.

**Out of scope for v1:** a fixed occurrence count or an end date for a recurring series (e.g. "repeat 10 times" or "repeat until March"). The interval-only model matches what was asked for; a bounded series is a natural follow-up if needed later, but adds another field and another piece of scheduler logic that wasn't requested.

### 13.6 Out of Scope

- Draft/save-without-scheduling workflow. Not needed: creation always goes straight to `status="scheduled"`, and the `draft` value in the schema stays unused.
- Editing a scheduled announcement's body/time/recurrence before it fires. Not needed: cancel-and-recreate is the intended workflow, matching the API's cancel-only surface (no update endpoint).
- Rich Discord embeds (the API sends plain markdown text via the same `send_channel_message` path used for occurrence pings).
- Gating `leadership_only` behind a permission check (e.g. restricting it to owners or kingdom coordinators). It is a display/categorization flag only, same as it is for events.

## 14. Admin UI — Combined Multi-Tenant View

**Status:** Implemented and deployed. Fixed a real friction: a user with access to more than one tenant (e.g. NSR and MOD) must currently select one tenant in the `tenantPicker` and every admin view reloads scoped to just that tenant (`onTenantChange()` in `common.js`) — managing both alliances means repeatedly switching the picker. Public-facing pages are unaffected by this section and keep their existing per-tenant scoping (`/t/{tenant_slug}/events`, `/t/{tenant_slug}/ics/events.ics`); that separation stays, since different alliances' members should keep seeing their own calendar. This mirrors, on the admin side, the reasoning already documented for the public bare `/events`/`/api/events` route: "some members want one shared view rather than switching between alliance pages."

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

## 15. Time Zone Display — Header Control (Admin) and Public Events Page

**Status:** Implemented and deployed. Fixed a real usability gap that existed before this: the only display-timezone control in the app today is the **Display Timezone (admin UI)** dropdown (`mTimezone`) buried inside the Add/Edit Event modal — a place a user would only ever open to create or edit an event. Yet `getDisplayTz()` (`events.js`), which that dropdown writes to, is read globally: Dashboard's "Today's Events" times, Schedule's occurrence times, Gantt's chart axis, and Post Log's "Posted At" column all format through it. Worse, `getDisplayTz()`'s fallback when nothing is stored yet is a hardcoded `'UTC'`, not the browser-detected zone — even though "Your detected timezone: ..." is shown as informational text right next to the control. A new user sees their own zone named and still gets UTC everywhere until they happen to open the Event modal and pick it themselves. On top of relocating that control, this section also changes *what* is displayed: everywhere an event time is shown, in the admin area or on the public events page, both the canonical UTC time and the viewer's detected/selected local time are shown together — not one replacing the other, as the original draft of this section specced.

### 15.1 Approach (Admin)

Relocate the control to the masthead header (`admin.html`, alongside the tenant picker and the Log out button), so it is visible and reachable from every tab, not just from inside the Event modal. Two behavior changes alongside the move:

- **Default changes from hardcoded UTC to browser-detected.** The first time a user visits with nothing in `localStorage`, the control defaults to `Intl.DateTimeFormat().resolvedOptions().timeZone` (the same detection already used for the Event and Announcement modals' "Your detected timezone" helper text), not `'UTC'`.
- **Persistence is unchanged.** The override still lives in `localStorage` under the existing `samaya_display_tz` key — an existing stored value from a previous session keeps working with no migration, since it's the same key just read/written from the header instead of the modal.

The Event modal's own `mTimezone`/`mDetectedTz` fields are removed once the header control exists — they become redundant, not a second source of truth.

### 15.2 Dual-Time Display Format

Every place an event, occurrence, or announcement time is shown — Dashboard, Schedule, Gantt (with the exception in 15.5), Post Log, Announcements (§13.4's "Scheduled for" column), and the public events page (15.4) — shows **both** the UTC time and the viewer's detected/selected local time, not one or the other. Concretely, `fmtTime`/`fmtDateTime` (`events.js`) change from swapping the displayed value to concatenating both:

- `19:00 UTC` when the selected/detected zone *is* UTC (no duplication — showing "19:00 UTC · 19:00 UTC" would be noise, not information).
- `19:00 UTC · 3:00 PM EDT` when it differs — UTC stays in its existing 24-hour `HH:MM` form (unchanged, still the canonical value everything else in the app already keys off), and the local rendering uses 12-hour clock plus the zone's abbreviation (via `Intl.DateTimeFormat(..., { timeZoneName: 'short' })`) so the two are visually distinct at a glance rather than two bare numbers that only differ by an hour count.

**Dates can differ, not just times, near midnight UTC** — an event at `23:30 UTC` on the 28th is `7:30 PM EDT` on the *same* calendar day, but one at `01:00 UTC` on the 28th is `9:00 PM EDT` on the *27th*. `fmtDateTime` (used by Post Log's "Posted At" column and anywhere else a date accompanies the time) must render each half against its own correct calendar date rather than assuming both halves share the date already shown once — this is a real correctness detail, not just a formatting choice, and worth testing explicitly against a near-midnight occurrence.

**Entry fields are unaffected and stay UTC-only**, per instruction — events are still created in UTC: the Event form's **Start Time (UTC)** field and the Announcement form's **Send at (UTC)** field (§13.2) keep their UTC labeling and single value, and are not given a dual-time treatment. Dual-display is for showing *existing* timestamps, never for entering new ones.

### 15.3 Header Control (Admin)

Originally a `<select>` dropdown carrying a visible **"🕐 Time zone:"** label. Superseded by §24: the header now shows a live UTC/local clock as a clickable text link, and the same IANA zone list this section originally specced for the dropdown moved into the Time Zone modal §24 opens instead — the underlying zone list, default-to-detected behavior, and `localStorage` key are all unchanged from 15.1, only the control's shape changed.

### 15.4 Public Events Page

**Superseded by §26.2**: originally specced as a `<select>` dropdown in the page's own masthead; the header clock/modal pattern replaced it, matching the admin header's §24 shape rather than a labeled dropdown. The underlying behavior — detect-by-default, override via the shared `samaya_display_tz` `localStorage` key, dual-format display per 15.2 — is unchanged, only the control's shape moved. See §26.2 for the current implementation.

### 15.5 Gantt Exception

Gantt's day cells (`static/js/gantt.js`) are small, fixed-width grid cells with room for one short time label (`fmtTimeShort()` today), not a full sentence — cramming a dual-time string into every cell would make the grid unreadable. Gantt keeps a single short label inline (the local/selected time, since that is what a viewer scanning their own week most wants at a glance), and puts the full dual-time string in the cell's existing `title` tooltip attribute instead (already used today for the event name and date, extended to include the dual time on hover). This is the one view where "shown" means "on hover" rather than "inline," because of real space constraints, not an oversight.

### 15.6 Out of Scope

- Per-view timezone overrides (e.g. Schedule shown in one zone, Post Log in another). One global setting is what's being asked for.
- A dedicated "reset to detected" action. Switching back to the detected zone is just picking it from the same dropdown like any other zone; no separate control is needed.
- Applying the dual-time treatment to the Event/Announcement UTC-only **entry** fields (Start Time, Send At). Display and entry stay distinct per 15.2 — Announcements' **display** column (§13.4's "Scheduled for") is in scope and covered above.

## 17. Admin UI — Tab Order

**Status:** Implemented and deployed. The tab bar is reordered so the tabs a coordinator actually works in day to day come first, and the tabs that configure or administer the system come last:

`Dashboard → Events → Announcements → Schedule → Gantt → Post Log → Sync → Config → Access → Platform`

Rationale: Events and Announcements are grouped adjacently since both are content a coordinator creates and schedules (and, per §20, share the same target-validation logic and a similar per-tenant-dropdown target-row UI). Schedule/Gantt/Post Log follow as the "what's actually happening" occurrence-tracking group. Sync (Discord↔PostLog reconciliation) and Config (per-tenant Discord channel/role reference) come next as tenant-level admin. Access (owner-only invite management) and Platform (superadmin-only Kingdom/Tenant CRUD) are last, since they're the least-frequently-used and highest-privilege tabs. No functional change — `showView()`/`applyRoleVisibility()` in `common.js` look up tabs by `id`, not position, so reordering the `<li>` markup in `admin.html` was the only change needed.

## 18. Platform — Tenant Editing

**Status:** Implemented and deployed. The backend has supported editing a Tenant's `name`, `slug`, `guild_id`, `bot_token`, `public_key`, and `color` since Phase 4 (`PATCH /api/tenants/{id}` in `routers/admin/tenants.py`), but the Platform tab's UI only ever exposed *creating* a tenant (`createTenant()`) — the table's action column was rendered but left empty. Fixed by wiring up the existing endpoint:

- An **Edit** button on each row of the Tenants table (Platform tab, superadmin-only) opens a sequence of `prompt()` dialogs pre-filled with that tenant's current `name`/`slug`/`guild_id`, matching this file's existing `prompt()`-based convention for platform actions (`createKingdom`/`createTenant` already work this way — no modal dialog was introduced for consistency with the rest of this view).
- Pressing Cancel on any individual field leaves that field unchanged rather than aborting the whole edit, since re-entering every other field just to fix one is unnecessary friction.
- `bot_token` can't be pre-filled (it's write-only — the API never returns an existing token, same reasoning `createTenant` already follows for a brand-new one). Its prompt explains the two states: leave blank to keep the current token (or the platform-bot fallback) unchanged, or type `CLEAR` to remove an existing token and fall back to the shared platform bot, or paste a new token to set one.
- Only fields that actually changed are sent in the `PATCH` body — an edit where every prompt is cancelled or left at its current value sends nothing and shows "No changes made" rather than an empty successful request.

## 19. Config — Server Name Column

**Status:** Implemented and deployed. The Config tab's Channels table only ever showed a channel's name and Discord ID, with no indication of which physical Discord server it belongs to — a gap that matters because a **Tenant** (alliance) and a **Discord guild** are not one-to-one: MOD and NSR, for example, share one `guild_id`, and HTD is described as the de facto Kingshot-wide server. Added:

- `services/discord_api.get_guild_info(token, guild_id)` — calls `GET /guilds/{guild_id}`, returning the guild's own Discord display name (distinct from `Tenant.name`, which is the alliance's name in our system, not the Discord server's).
- `GET /admin/api/discord/guild` (`routers/admin/discord_config.py`) — exposes it, scoped to the current tenant the same way `/api/discord/channels`/`/api/discord/roles` already are.
- The Channels table gains a **Server** column between Name and ID, populated from this new endpoint. Since Config is single-tenant (§14.2), the same guild name repeats on every row — that's expected, not a bug; the point is showing *which* server, not varying it per row.
- If the guild-info fetch fails (502 — most commonly because the bot hasn't been invited to that guild yet, Discord returning 403), `config.js` shows an em dash in the Server column rather than failing the whole channel list, since the channel list itself may still load successfully or fail independently.

## 20. Events — Multi-Server Notification Targets

**Status:** Implemented and deployed. Originally specced as a Discord-*posting* multi-target mechanism mirroring Announcements' (`{tenant_slug, discord_channel_id}`); revised during implementation once it became clear the actual complaint driving this (event notifications reaching only one channel/role) is about the **pre-event ping**, not the Discord Scheduled Event post itself — the ping is the only piece that's genuinely per-guild-channel-scoped. The event's own Discord Scheduled Event `location` field (`discord_channel`) is free text reused identically across every target's guild already (see kingdom-wide fan-out, unchanged), so there was nothing to generalize there.

### 20.1 Why this is additive, not a replacement

`scope` (`alliance`/`kingdom-wide`) and its automatic fan-out to same-Kingdom tenants (§3, `occurrences.py`'s `_post_to_one_tenant`) stays exactly as it is — it's live production behavior serving real kingdom-wide events today, and collapsing it into a manually-maintained target list would mean every existing kingdom-wide event silently stops fanning out to a *new* alliance that joins the Kingdom later (the automatic version re-evaluates Kingdom membership every time occurrences generate; a stored target list would be a one-time snapshot). Instead, this adds a **second, independent** targeting mechanism on top:

- A new `event_targets` table (`models.db.EventTarget`): `id`, `event_id` (FK → `event_definitions`, `ON DELETE CASCADE`), `tenant_id` (FK → `tenants`), `notification_channel_id`, `notification_role_id` (both `TEXT NOT NULL DEFAULT ''`). Unique on `(event_id, tenant_id)`. Migration: `migrate_add_event_targets.py`.
- These are **explicit extra destinations**, independent of `scope` and of Kingdom membership entirely — this is the HTD case: HTD is a community-wide server that isn't necessarily a Tenant inside the same Kingdom grouping as the alliances whose events should also reach it, so it needs to be addressable regardless of Kingdom.
- **Posting** (`post_occurrence` in `occurrences.py`): the target list for a given occurrence is now `[owning tenant]` (alliance-scope) or `[every same-Kingdom tenant]` (kingdom-wide, automatic, unchanged) **plus** every tenant with an `EventTarget` row for that event, deduplicated by tenant id before posting — a tenant that's both a kingdom-wide fan-out recipient and an explicit target gets exactly one Discord Scheduled Event and one `PostLog` row, not two. When there's exactly one resulting target (the common case: an alliance-scope event with no explicit targets), the endpoint keeps its original single-target response shape (`{discord_event_id, status}` with a 409/502 on failure) rather than always returning the multi-target `{status, targets: [...]}` shape — no response-shape change for the overwhelmingly common case.
- **Notification resolution** (`_resolve_notification` in `occurrences.py`): the owning tenant always uses the event's own bare `notification_channel_id`/`notification_role_id`. A kingdom-wide fan-out tenant checks `EventTenantNotification` first (existing behavior, unchanged), then falls through to check `EventTarget` — so a tenant can be reached via *either* mechanism, or both, with `EventTarget` as the catch-all for anything `EventTenantNotification` doesn't cover (any tenant that isn't the owner and isn't a kingdom-wide fan-out recipient goes straight to `EventTarget`). No row in either place for a target = no ping for that target, same as before — the Scheduled Event post itself still happens regardless.

### 20.2 UI changes

- The Event modal gains an **Extra Notification Targets** section: a tenant picker plus a live per-tenant channel dropdown *and* a live per-tenant role dropdown (`addEventTargetRow`/`populateEventTargetFields` in `events.js`) — one more field than Announcements' target row (§13.3), since an event's ping carries a role mention where an announcement's plain channel post doesn't need one.
- `EventIn` gains `targets: list[{tenant_slug, notification_channel_id, notification_role_id}] = []`; `EventPatch` gains the same field as `Optional[...] = None` (`None` = leave the current list alone, `[]` = explicitly clear it — the admin UI always sends the full current list on save, so in practice every save is a full replace, never a partial merge).
- Both Announcements' and Events' server-side target validation (resolve tenant slugs, check the caller holds `UserTenant` access to each) now share one implementation — `resolve_target_tenants`/`check_target_access` in `routers/admin/deps.py` — rather than each endpoint keeping its own copy of the same check.

### 20.3 Out of scope

- A separate `discord_channel_id`-per-target field for the Scheduled Event post itself — not needed, since that field is decorative location text already reused identically across every guild (see above).
- `notify_minutes_before` per target — every target uses the event's own single lead time; only the destination (channel/role) varies per target, not the timing.
- Migrating existing kingdom-wide events into `EventTarget` rows. Not needed — §20.1 keeps the automatic fan-out mechanism intact rather than replacing it, so there's nothing to migrate.

## 21. Access Management — Editing Existing Grants, Kingdoms, and Superadmin Status

**Status:** Implemented and deployed. Prior to this, editing "configuration data" stopped at the Tenant level (§18): once someone claimed an invite, there was no way to change their role, remove their access, edit a Kingdom's own name/slug, or manage the platform-wide superadmin flag — only the invite *lifecycle* (create/revoke a still-pending invite) was exposed anywhere. Closed four separate gaps:

### 21.1 Tenant members (UserTenant grants)

- `GET /admin/api/members` — everyone with standing access to the current tenant (owner-scoped, same `require_tenant_owner` dependency as invite management), returning each grant's role alongside the user's Discord username. The counterpart to `GET /api/invites`, which only ever showed pending/used/revoked invite *records*, never the resulting grant.
- `PATCH /admin/api/members/{id}` — change a grant's role between `owner`/`coordinator`.
- `DELETE /admin/api/members/{id}` — remove access outright (the person needs a fresh invite to get back in).
- Guard: a tenant can't be left with zero owners — demoting or removing the *only* remaining owner is rejected with 400, since that would leave no one able to invite, edit tenant config, or manage access at all. Promote a second owner first.
- Admin UI: the Access tab (owner-only) gains a **Members** table above the existing Invites table, with "Change role" and "Remove" buttons per row (`access.js`).

### 21.2 Kingdom coordinators (UserKingdom grants)

- `GET /admin/api/kingdom-coordinators?kingdom_id=` and `DELETE /admin/api/kingdom-coordinators/{id}` (`routers/admin/invites.py`, superadmin-only) — the standing-grant counterpart to the existing kingdom-invite endpoints, mirroring §21.1's tenant-member pattern.
- Admin UI: a **Kingdom Coordinators** table in the Platform tab (`platform.js`'s `loadPlatformKingdomCoordinators()`), sitting between the Kingdoms and Tenants tables. Since the list endpoint is scoped to one kingdom at a time, the loader fans out one `GET /api/kingdom-coordinators` call per kingdom (from the already-loaded kingdom list) and flattens the results into one table with a Kingdom column, rather than adding a per-kingdom expandable row. Each row's **Remove** button calls `removeKingdomCoordinator()`, gated behind a `confirm()` naming the user and kingdom, matching §21.1's Members table and §21.4's Users table conventions.

### 21.3 Kingdom editing

- `PATCH /admin/api/kingdoms/{id}` (`routers/admin/tenants.py`, new `KingdomPatch` schema, superadmin-only) — Kingdoms previously supported creation only. Now editable the same `name`/`slug` fields as creation.
- Admin UI: an **Edit** button on each Kingdoms row in the Platform tab (`editKingdom()` in `platform.js`), same `prompt()`-based, Cancel-leaves-unchanged convention as `editTenant()` (§18).

### 21.4 Platform-wide user management and superadmin flag

- New `routers/admin/users.py` (superadmin-only): `GET /api/users` lists every `User` row platform-wide (Discord username, superadmin flag, last login); `PATCH /api/users/{id}` toggles `is_superadmin`.
- Kept separate from `invites.py` deliberately — this edits the `User` row itself, not a per-tenant/per-kingdom grant, and its effect crosses every tenant/kingdom boundary at once rather than being scoped to one.
- Guard: a superadmin cannot revoke their own superadmin flag (400) — since the very next request would fail `require_superadmin` with no UI path back in short of direct database access. Another superadmin can still demote them.
- Admin UI: a **Users** table in the Platform tab (`platform.js`), each row showing Discord username, a Superadmin label when set, last login, and a "Make superadmin"/"Revoke superadmin" button — gated behind a `confirm()` describing the scope of what's being granted, since it's the single highest-privilege action in the system. The current user's own row shows `(you)` instead of a revoke button rather than letting them hit the server-side guard.

### 21.5 What's still out of scope

- No bulk operations (e.g. removing several members at once) — every action here is single-row, matching the rest of the admin UI's `prompt()`/`confirm()`-based conventions rather than introducing a new selection/bulk-action pattern for this alone.
- `discord_id`/`discord_username` on `User` remain read-only everywhere — they come from Discord OAuth and aren't meant to be hand-edited.

## 22. Known Gaps and Design Notes

- No per-tenant custom Discord Interactions Endpoint support — all inbound webhooks go through one platform-level endpoint verified against `PLATFORM_PUBLIC_KEY`.
- No named recurrence types (weekly, monthly, etc.) — recurrence is purely interval-in-days from an anchor date.
- Alembic is declared as a dependency but unused; schema changes to date have been hand-written, one-off migration scripts.
- Audit log is append-only and explicitly not a rollback mechanism, since many logged actions have already reached Discord by the time they're logged.
- A separate architecture document ("Samaya Self-Hosted Architecture PRD v2.0", Google Drive) predates the multi-tenant/auth rework and is superseded by the repository's own `CLAUDE.md`, which this specification is derived from alongside direct inspection of the source.

## 23. Deployment — Static Asset Cache-Busting

**Status:** Implemented and deployed. `ks138.taraka.dev` sits behind Cloudflare, which caches `/static/*.js` and `/static/*.css` at the edge by file extension for hours (`Cache-Control: max-age=14400` observed) independent of how fresh the origin's copy actually is — a redeployed JS file can keep serving its pre-deploy content to every visitor until that edge cache entry expires or is purged, and a client-side `fetch(..., {cache: "no-store"})` does not bypass it (that header only controls the browser's own cache, not an intermediary's). `/` itself wasn't affected, since Cloudflare's default rules don't cache HTML by file-extension the way they do JS/CSS, and `admin_home()` reads and returns `admin.html` fresh on every request anyway.

Fixed at the origin rather than relying on a manual Cloudflare purge after every deploy: `routers/admin/ui.py` now rewrites every `/static/...` `src`/`href` reference in `admin.html` to append `?v={STATIC_ASSET_VERSION}` before serving it (`_bust_static_cache()`, a small regex substitution — skips any reference that already carries a query string, and only touches local `/static/` paths, not external vendor CDNs). `STATIC_ASSET_VERSION` is a constant in that file, bumped whenever a static asset actually changes; because it's baked into the URL rather than the file itself, a version bump makes Cloudflare (and browsers) treat every asset as a new object and fetch it from origin, with no dashboard purge required. It's tracked independently of the spec/API version number — it only needs to move when `/static` content changes, not on every release.

Covered by `tests/test_ui.py` (`_bust_static_cache()` unit tests — an HTTP-level test of `GET /` isn't practical since `admin_home()` reads a hardcoded `/app/static/admin.html` path that only exists inside the built container).

**Out of scope:** the public events page (`/events`, `/t/{slug}/events`) doesn't reference any of its own `/static/js/*.js` files (only pinned vendor CSS), so it isn't exposed to this problem and wasn't touched.

## 24. Admin UI — Header Controls as Modals, and Announcement Cleanup

**Status:** Implemented and deployed. Two changes bundled together since both landed in the same pass:

### 24.1 Alliance and Time Zone — Text Link + Modal, Replacing the Header `<select>`s

The masthead's two `<select>` dropdowns (the tenant picker and, per §15.3, the time zone selector) are replaced with two plain text links (`#tenantLink`, `#headerClock` in `admin.html`), each showing the current state at a glance and opening a small modal to change it:

- **`#tenantLink`** shows the current alliance's name (or "— All my alliances —" in combined mode, §14) followed by "▾", and opens the **Switch Alliance** modal (`#tenantModal`) — a plain list of buttons, one per tenant the user has access to plus the combined option when there's more than one, mirroring the old dropdown's option list exactly. Selecting one calls the same `onTenantChange()` the old `<select>`'s `onchange` called, so every downstream behavior (role-visibility, single-tenant-only view correction, view reload) is unchanged — only the picker UI moved.
- **`#headerClock`** shows a live clock in the form `🕐 HH:MM UTC` (UTC only when the selected/detected zone *is* UTC) or `🕐 HH:MM UTC · HH:MM (Zone/Name)` otherwise, ticking on a 30-second interval — plenty for a minute-resolution display without needless re-renders. Clicking it opens the **Time Zone** modal (`#timezoneModal`), the same IANA zone list §15.3 originally put in a dropdown, now as a list of buttons; selecting one calls the same reload logic `onDisplayTzChange()` used to (renamed `selectTimezone()`), re-rendering every dual-time view exactly as before.
- Both modals follow the existing `#eventModal`/`#announcementModal` show/hide convention (a per-ID `display:none` / `.open{display:block}` CSS pair, since PatternFly's own backdrop has no built-in show/hide state) rather than introducing a new modal mechanism.
- `startHeaderClock()` runs immediately on page load, independent of login/tenant state (same reasoning §15.1 gave for the old picker: a user waiting on a delayed login redirect still sees a correctly-running clock), while `loadTenants()` still waits on `loadMe()` since it needs to know which alliances this user can see.

### 24.2 Announcements — Cleanup and Duplicate

Addresses a real gap: once an announcement reaches a terminal state (`posted`/`failed`/`cancelled`), it had no way to leave the list, and a cancelled announcement's Targets column kept showing each target's stale pre-cancellation `post_status` (usually `pending`), which read as if the cancellation hadn't taken effect.

- **`DELETE /admin/api/announcements/{id}`** (`routers/admin/announcements.py`) removes an announcement outright — allowed only when `status` is `posted`, `failed`, or `cancelled` (400 otherwise, telling the caller to cancel it first); a still-`scheduled` announcement keeps using the existing Cancel action, not this one. `AnnouncementTarget` rows cascade via the FK's existing `ondelete="CASCADE"`.
- Admin UI: a **Delete** button in the Actions column for any announcement in one of those three terminal states, alongside the existing **Cancel** (scheduled only) and a new **Duplicate** (always available).
- **Duplicate** opens the New Announcement modal pre-filled with the source announcement's title, body, leadership flag, recurrence settings, and full target list (each target's tenant + Discord channel, re-resolved through the same live-channel-dropdown mechanism §13.3 already uses) — except the send date/time, which is always left blank. A duplicate's whole point is scheduling the same content again; carrying forward the original's date/time would either be in the past already or reproduce whatever got it cancelled/failed in the first place.
- The Targets column now shows `cancelled` for every target of a cancelled announcement, rather than each target's individually-tracked `post_status` — once the parent is cancelled, the per-target delivery attempt is moot regardless of what it was before that.

### 24.3 Out of Scope

- Bulk delete/duplicate across multiple announcements at once — every action here is single-row, matching the rest of the admin UI's established convention (§21.5 made the same call for Access Management).
- Deleting a `scheduled` announcement directly (skipping Cancel) — kept as a two-step action deliberately, so a live announcement is never removed without the explicit cancellation step first.

## 25. Discord Server as a First-Class Entity

**Status:** Implemented, pending deployment. All bullets below describe what was built; §25.1's migration script must run against production before the new application code is deployed (same order as every other schema-changing migration in this repo).

**Problem.** `Tenant` (alliance) has carried `guild_id`/`bot_token`/`public_key` directly since Phase 4, and its own docstring already notes these can repeat across rows — MOD and NSR sharing one Discord guild is real and already happens (§19). But nothing makes that sharing *explicit*: each alliance sharing a guild still carries its own separate copy of the guild's bot credentials, there's no single place that lists "these three alliances live on this one Discord server," and conceptually the app conflates two things that are only loosely related — **which alliance(s) a Discord server hosts** (infrastructure) and **who can create/manage events and announcements** (an alliance-scoped concern that should reference a server, not double as its own copy of one).

**Design (confirmed):**

- New `discord_servers` table: `id`, `name` (display name), `guild_id` (unique), `bot_token` (nullable → platform-bot fallback, unchanged fallback behavior from today), `public_key` (nullable → platform fallback), `created_at`. Not scoped to a `Kingdom` — a server (HTD is the running example, described in §19 as a de facto Kingshot-wide hub) can host alliances that span Kingdoms, or none at all beyond the ones actually created there.
- `tenants` loses `guild_id`/`bot_token`/`public_key`; gains `server_id` (FK to `discord_servers`, not null). Every other Tenant field (`kingdom_id`, `name`, `slug`, `color`) is unchanged — a Tenant is still the alliance/access-control unit, it just references its Discord identity instead of carrying a copy of it.
- **One bot per server** (confirmed): once alliances share a `server_id`, they share that server's single `bot_token`/`public_key` — no per-alliance override once sharing is in effect. This is enforced structurally by the fields moving off `Tenant` entirely, not by a runtime check.
- **Access model** (confirmed, with one narrowing worth confirming explicitly): being a coordinator/owner of any alliance on a server continues to grant exactly the operational access a coordinator already has today — selecting that server's channels/roles when targeting an event or announcement, and seeing them in the Config tab (§19) — no new grant required, matching current behavior since this is unchanged from how Tenant-scoped access already works. **Narrower than a literal reading of "server access," by design**: editing the *server's own* `bot_token`/`public_key`/`name` stays superadmin-only, the same privilege level `PATCH /api/tenants/{id}`'s `bot_token` field already requires today (§18) — a credential is not day-to-day alliance config, and loosening that to "any coordinator on a shared server" would be a real privilege expansion beyond what was asked. Flagging this interpretation for explicit sign-off before building, since it's the one place a judgment call was needed beyond the three confirmed answers above.
- `EventTarget`/`AnnouncementTarget` schemas are **unchanged** — targeting stays keyed by `tenant_slug` (resolved through `resolve_target_tenants`/`check_target_access`, §13.3/§20), since that's also the access-control anchor: a user can only ever pick tenants they hold `UserTenant` access to. The actual Discord destination those resolve to now comes from `tenant.server` instead of the tenant row directly, but that's invisible to the targeting mechanism itself.

### 25.1 Migration Plan

Hand-written, idempotent script (no Alembic in this repo, per established convention — §22), run once before the app restarts on that deploy:

1. For each distinct `guild_id` currently on `tenants`, create one `discord_servers` row. Name it after the first tenant found with that `guild_id` (a human can rename it afterward via the new Platform UI).
2. **Bot token conflict resolution**: if multiple tenants share a `guild_id` and have *different* non-null `bot_token` values today (a real possibility, since nothing enforced consistency before this), the migration cannot silently pick one — it logs every such conflict (guild_id, the differing tenant slugs, and which token it chose) to stdout for manual review, and defaults to the **first non-null token found**, ordered by `tenant.id`. This is a real data-quality question that needs eyes on the actual production values, not something to guess safely from here.
3. Set every `tenant.server_id` to its resolved server's id.
4. Drop `tenants.guild_id`, `tenants.bot_token`, `tenants.public_key`.

### 25.2 Affected Call Sites

The pattern `tenant.bot_token or PLATFORM_BOT_TOKEN` / `tenant.guild_id` is repeated across `routers/admin/deps.py` (`get_discord_config`), `routers/admin/discord_sync.py`, `routers/admin/discord_config.py`, `routers/admin/status.py`, `routers/admin/occurrences.py`, `scheduler/announcements.py`, and `scheduler/reminders.py` — roughly 15 call sites across 7 files. Rather than auditing and modifying every individual query that fetches a `Tenant` to eager-load its new `server` relationship (real risk of a missed spot reintroducing the `MissingGreenlet` class of bug this session already hit twice building §20), `Tenant.server` is mapped `lazy="joined"` — eager by default on every fetch, everywhere, with no per-query changes required. The relationship is a single cheap FK join with no N+1 concern, so there's no real cost to always including it. Each call site's own change is then mechanical: `tenant.bot_token` → `tenant.server.bot_token`, `tenant.guild_id` → `tenant.server.guild_id`.

### 25.3 Admin UI Changes

- **Platform tab**: a new **Discord Servers** table (superadmin-only, same tier as Kingdoms/Tenants/Users today) — create/edit a server's `name`/`guild_id`/`bot_token`, listing which tenants currently reference it (read-only membership list; reassigning a tenant to a different server happens via editing the *tenant*, not the server, to keep "who owns this relationship" unambiguous).
- **Tenant create/edit** (`createTenant()`/`editTenant()` in `platform.js`): the inline "Discord guild (server) ID" and "Bot token" prompts are replaced with a single "Discord Server" selection — pick an existing server from the list, or create a new one inline (same flow `createTenant()` already uses for picking a Kingdom). `has_own_bot_token` in `_tenant_dict()` is retired along with the field it described; the Tenant row no longer has an opinion on bot credentials at all.
- **Config tab** (§19): unchanged in appearance — still shows that tenant's server's channels/roles/Server-name column — since it already reads through `get_discord_config`, which becomes the one place that changes underneath it.

### 25.4 Out of Scope

- Letting a non-superadmin move a tenant to a different server, or edit a server's credentials directly — both stay superadmin-gated per 25's access-model note above.
- Any change to how `EventTarget`/`AnnouncementTarget` resolve access or store their destination — unchanged, per the Design section above.
- Retiring `has_own_bot_token`'s underlying concept everywhere at once — this section covers `Tenant`/`DiscordServer` only; if some other part of the codebase reads that flag beyond `_tenant_dict()`/`platform.js`, it surfaces during implementation, not guessed at here.

### 25.5 Implementation Notes

Built exactly as designed above, with one addition: `GET/POST/PATCH /admin/api/discord-servers` (`routers/admin/tenants.py`) — superadmin-only, mirroring the existing Kingdom/Tenant endpoint shape, backing the new Platform-tab table. `TenantIn`/`TenantPatch` now take `server_id` in place of `guild_id`/`bot_token`/`public_key`; `_tenant_dict()` returns `server_id`/`server_name`/`guild_id` (read-only convenience, sourced from the joined `DiscordServer` row) and no longer exposes `has_own_bot_token` or any credential field.

Covered by `tests/test_discord_servers.py`: CRUD + superadmin gating on the new endpoints, Tenant create/update now going through `server_id` (including the 404 on an unknown server), and — the actual behavior this redesign exists for — two tenants sharing one `DiscordServer` and a bot-token update on that server being visible to both, with no per-tenant override to go stale. `tests/conftest.py`'s `tenant`/`second_tenant` fixtures and `tests/test_event_targets.py`'s third-tenant helper were updated to create a `DiscordServer` per tenant (each still gets its own server, preserving every existing test's assumption of separate guilds/tokens per tenant — sharing is exercised only by the new tests above).

One SQLAlchemy async wrinkle worth flagging for future call sites: `Tenant.server` is `lazy="joined"`, which means it's already populated the moment a `Tenant` is first loaded in a session — but if that same in-session `Tenant` object's `server_id` is then changed and committed, a follow-up `select(Tenant)...` for the same row returns the *same identity-mapped Python object* with its now-stale `.server` still attached (a plain re-select only populates attributes that were never loaded). `update_tenant` handles this with an explicit `await db.refresh(tenant, attribute_names=["server"])` after changing `server_id`; any future code path that reassigns `Tenant.server_id` mid-session needs the same explicit refresh, not just a re-query.

## 26. Admin Logout Contrast, Public Events Page Header Parity, and Alliance Switcher

**Status:** Implemented and deployed.

### 26.1 Admin — Log Out Button Contrast

The masthead's Log Out button uses PatternFly's `pf-m-secondary` button variant, whose default color tokens (`--pf-v6-c-button--m-secondary--Color`/`--BorderColor` and hover variants) are tuned for a light background — on the dark masthead it rendered as a low-contrast blue-outlined button, hard to read at a glance. Fixed with a scoped override (`.pf-v6-c-masthead .pf-v6-c-button.pf-m-secondary` in `admin.html`) that repoints those same custom properties to `var(--pf-t--global--text--color--inverse)`, matching the masthead's other text/link colors (§24.1's `#tenantLink`/`#headerClock`) instead of fighting PatternFly's button coloring with ad hoc `!important` declarations.

### 26.2 Public Events Page — Header Parity with Admin

`static/events.html`'s header is brought to the same shape as admin's §24.1 header, rather than the `<select>` dropdown §15.4 originally specced:

- The time zone `<select>` is replaced with the same `#headerClock` text-link-plus-modal pattern as admin: a live `🕐 HH:MM UTC` / `🕐 HH:MM UTC · HH:MM (Zone/Name)` clock (`formatHeaderClock()`, ticking every 30s via `startHeaderClock()`), opening a `#timezoneModal` listing the same IANA zone set as a button list. Selecting one calls `selectTimezone()`, which persists to the same shared `samaya_display_tz` `localStorage` key (§15.4) and reloads the event list — a preference set in the admin area still carries over automatically in the same browser.
- Both pages now use visually and behaviorally identical time controls; the only difference is that `events.html` has no shared JS with `admin.html` (per its established standalone-file convention — §22), so the clock/modal logic is a duplicate implementation of the same behavior, not a shared import.

### 26.3 Public Events Page — Alliance Switcher

Previously, moving between the combined `/events` view and a specific alliance's `/t/{slug}/events` page required knowing or being given the target URL directly — there was no in-page way to discover or switch between alliances.

- New public, unauthenticated endpoint **`GET /api/alliances`** (`routers/events.py`) returns every tenant's public-page identity — `{name, slug, color}` — sorted by name. This exposes nothing not already public: a tenant's name/slug/color already appear in the combined `/api/events` payload's tenant badge and are reachable by guessing/following a `/t/{slug}/events` link; this endpoint just makes the existing roster listable without already knowing one.
- `events.html`'s header gains an `#allianceLink` text link (showing the current alliance's name, or "— All Alliances —" in combined mode, followed by "▾"), opening an `#allianceModal` listing every alliance from `GET /api/alliances` plus the combined option, each as a plain link to its `/t/{slug}/events` (or bare `/events`) page — a full navigation, not an AJAX swap, since each destination is its own page with its own event set.
- Both new modals (`#allianceModal`, `#timezoneModal`) follow the same per-ID `display:none`/`.open{display:block}` convention as every other modal in this app (§24.1), not a new mechanism.

### 26.4 Out of Scope

- Making the alliance switcher's list respect any access control — it's public and unauthenticated by nature (matching the pages it links between), so it lists every tenant regardless of who's viewing, same as the combined `/events` page already does.
- Deduplicating the clock/modal implementation between `admin.html` and `events.html` into a shared file — out of scope per §22's established standalone-page convention for the public events page.

## 27. Announcement Templates

**Status:** Implemented, pending deployment. §27.1's migration script must run against production before the new application code is deployed, same order as every other schema-changing migration in this repo.

**Problem.** Composing an announcement from scratch every time is repetitive for recurring situations — a daily reset warning, a weekend event reminder — and typing the send time into the message body by hand means it's wrong the moment the reader is in a different time zone, or the announcement gets rescheduled. Two related gaps: no way to save a wording pattern for reuse, and no way to reference "when this is happening" in a way that resolves correctly for every viewer and every recurrence.

**Design:**

- New `AnnouncementTemplate` (table `announcement_templates`): `id`, `owning_tenant_id` (FK, same scoping as `Announcement` itself — a template is managed by whoever can create announcements for that alliance), `name` (unique per tenant), `title_template`, `body_template`, `leadership_only`, `event_offset_minutes` (all default 0/false), `created_at`. Picking a template in the admin UI copies its four content fields into the New Announcement modal — a one-time copy, the same relationship **Duplicate** (§24.2) already has to the announcement it copied from, not a live link back to the template.
- `Announcement` gains `event_offset_minutes` (int, default 0) — independent of templates; a hand-written announcement can use placeholders too, with no template involved at all.
- **Six placeholders**, resolved by `services/templates.py`'s `render_placeholders()`: `{alliance_name}`, `{kingdom_name}`, `{send_time}`, `{send_time_relative}`, `{event_time}`, `{event_time_relative}`. The `event_*` pair is the announcement's own `scheduled_for` shifted by `event_offset_minutes` — the "warn ahead of an event" pattern: a reset warning sent at 23:45 UTC for a 00:00 UTC reset uses `event_offset_minutes: 15` and writes `{event_time_relative}` rather than hand-typing "in 15 minutes" (which goes stale the moment the schedule changes, and reads wrong for a viewer outside UTC). `send_time`/`event_time` render as Discord's own dynamic timestamp markdown (`<t:UNIX:F>`, `<t:UNIX:R>`) — Discord itself renders these client-side, in each viewer's own locale and time zone, with no app code involved once posted; this app has never needed to compute a "your local time" string for a Discord message before, because Discord already does it natively once the raw timestamp is there.
- **Resolution happens per-target, at delivery time** (`scheduler/announcements.py`), not once when the announcement is created or when a template is applied. This is a hard requirement, not a style choice: a single announcement can target multiple alliances (§13.3), each with its own name, so `{alliance_name}` can only be resolved once the specific target being posted to is known; and a recurring announcement's `scheduled_for` advances by `interval_days` on every re-arm (§13.5), so a timestamp resolved once at creation would show the *first* occurrence's time forever instead of each occurrence's own. An announcement's stored `body_markdown` therefore may contain literal, unresolved `{placeholder}` text right up until the moment it's actually sent — this is expected, and is what the admin list shows for an unsent announcement, same as a template's own stored text.
- Unrecognized `{whatever}` text is left untouched rather than raising an error or vanishing (`render_placeholders`' regex substitution only replaces the six known names) — friendlier for a template being drafted incrementally, and safe against Discord markdown ever using brace syntax for something else.
- `Tenant.kingdom` is now `lazy="joined"` (previously default lazy-load), matching `Tenant.server`'s existing eager-loading (§25.2) — needed so `{kingdom_name}` resolves in the scheduler's `session.get(Tenant, ...)` call path without a separate query or a `MissingGreenlet` risk.

### 27.1 Migration Plan

`migrate_add_announcement_templates.py` — idempotent, hand-written (§22's established convention): adds `announcements.event_offset_minutes INTEGER NOT NULL DEFAULT 0` (every existing announcement gets 0, matching its actual pre-migration behavior since no placeholder syntax existed for it to apply to), then creates `announcement_templates` with a `(owning_tenant_id, name)` unique constraint. No rows are seeded — every tenant starts with zero templates. Verified end-to-end against a real Postgres instance (fresh create, idempotent re-run) before this section's status changed to Implemented.

### 27.2 Admin UI Changes

- **Announcements tab**: a new **Templates** table above the existing Announcements table — name, title, event offset, and **Use**/**Edit**/**Delete** actions. **Use** opens the New Announcement modal pre-filled from the template (title/body/leadership/event offset; targets and schedule are always left for the coordinator to fill in fresh, same reasoning §24.2 gives for Duplicate never carrying forward a schedule).
- **New Announcement modal**: gains an **Event Offset (minutes)** field next to Send Date/Time, and its Body field's helper text lists the six placeholders and a short Discord-markdown reminder (bold/italic/underline/strikethrough/spoiler/quote/code-block syntax) inline, rather than a separate reference page — the modal is where someone actually needs it. A new **Save as Template** button captures the modal's current title/body/leadership/event-offset as a new named template (prompted for a name), without scheduling anything — the reverse direction of **Use**.
- **Template modal**: a small dedicated create/edit form (name, title, body, event offset, leadership flag) for managing templates directly, independent of composing an announcement from one.

### 27.3 Out of Scope

- Kingdom-wide or platform-wide shared templates — a template stays scoped to the tenant that created it, same as Announcements themselves; two alliances wanting the identical "Daily Reset Warning" text each create their own copy. Revisiting this is a natural companion to §25's `DiscordServer` sharing model if it comes up again, but isn't part of this change.
- A live preview of the rendered message (showing what `{event_time}` will actually look like) before saving — the admin list already shows the literal template text, same as it always has for a `body_markdown` field; a true preview would need to fabricate a fake target/kingdom, which risks being misleading rather than clarifying.
- Placeholders anywhere outside Announcement/AnnouncementTemplate bodies — Event names/descriptions, the pre-event Discord ping (§15's `announce_msg` in `occurrences.py`), and Announcement *titles* are all still literal text with no substitution. Titles in particular are admin-list-only and never posted to Discord, so there's no viewer-facing reason to resolve placeholders in them.
- Any new markdown *capability* beyond what Discord already supports — this section documents existing Discord markdown for the admin's benefit and adds timestamp placeholders, but doesn't add formatting Discord itself can't render.

## 28. Announcement Composer — Markdown Toolbar, Live Preview, and Emoji Picker

**Status:** Implemented, pending deployment. Client-side only — no migration, no new backend tables.

**Problem.** §27 gave the admin markdown syntax and six placeholders to work with, but composing a body was still typed blind: the coordinator had to already know Discord's markdown syntax by hand, and had no way to see what `{event_time_relative}` or a role mention would actually look like before sending. §27.3 explicitly deferred a live preview, on the reasoning that faking a target/kingdom risked being more misleading than helpful — this section resolves that by using the modal's own live target rows as the preview's data source instead of fabricating anything, which removes the original objection.

**Design:**

- **Markdown toolbar.** A row of buttons above both the announcement Body field (`#aBody`) and the Template Body field (`#tBody`): Bold, Italic, Underline, Strikethrough, Spoiler, Quote, Inline Code, Code Block. Each wraps the textarea's current selection with the matching Discord markdown syntax (`**`, `*`, `__`, `~~`, `||`, `` ` ``, `` ``` `` on their own lines) via a shared `wrapSelection(textareaId, before, after)` helper in `static/js/announcements.js`; Quote instead prefixes every selected line with `> ` via `prefixSelectedLines()`, since it's a line-level marker rather than a wrap. With no selection, the wrap markers are inserted at the cursor with placeholder text selected between them (e.g. bolding nothing selects the word "bold" between the `**`s) so the coordinator can type over it immediately. Every toolbar action re-focuses the textarea and fires its `input` event, so the char counter (announcement body only) and the live preview (below) update immediately.
- **Live preview.** A preview pane below each Body field, styled as an approximate dark-theme Discord message bubble (bot avatar circle, "Samaya" name with a "BOT" tag, and the rendered message), updating on every keystroke. It resolves the same six §27 placeholders client-side (`clientRenderPlaceholders()` in `announcements.js`, a small JS port of `services/templates.py`'s `render_placeholders()` — kept as a second implementation rather than shared code, since one runs in the browser and one on the server and this app has no shared-code mechanism between them per §22) using:
  - **Alliance/kingdom name** — from a small **Preview As** dropdown next to the pane, populated from the modal's own target rows' tenant selections (so it only ever shows alliances actually targeted, never a fabricated one) and re-populated whenever a target row is added, removed, or changed. The template modal has no targets, so its preview always uses the current tenant (`getCurrentTenantSlug()`).
  - **Send time** — the Send Date/Time fields if both are filled; otherwise the current time, with a small note ("using current time — no send time set yet") so the coordinator isn't misled into thinking a real schedule was read.
  - **Event time** — Send time shifted by the Event Offset field, same as the server does.

  Unlike the real Discord message (which uses `<t:UNIX:F>`/`<t:UNIX:R>` and lets each viewer's own Discord client render it in their own locale), the preview computes a human-readable approximation once, in the *admin's own browser locale and time zone* (`formatDiscordAbsolutePreview()`/`formatDiscordRelativePreview()`) — there is no way to show "every viewer's own time zone" in a single static preview, so it shows one representative rendering and is labeled "(preview)" next to each resolved timestamp to make clear it's an approximation, not the literal Discord output.

  The resolved text is then run through a small markdown-to-HTML renderer (`renderDiscordMarkdownPreview()`) covering exactly the syntax §27 already documented as supported — bold/italic/underline/strikethrough/spoiler/inline-code/code-block/quote/headers — plus two Discord mention forms:
  - `<@&ROLE_ID>` is resolved to the real role name (`@RoleName`, styled as a blue mention pill) by fetching `GET /api/discord/roles` (existing endpoint, §15's Config tab already uses it) for the **Preview As** tenant, cached per tenant slug for the life of the modal session so re-renders on every keystroke don't refetch.
  - `<#CHANNEL_ID>` is resolved the same way against `GET /api/discord/channels` (also existing, already fetched per target row) if that channel ID happens to be one of the currently-loaded target rows' channel lists; otherwise it falls back to a generic `#channel` pill, since resolving an arbitrary typed-in channel ID would mean fetching every channel for every render.
  - A spoiler (`||text||`) renders as a black bar that reveals its text on click, matching Discord's own client behavior, so the coordinator can verify spoiler placement without it always being visible while editing.
- **Emoji picker.** A 🙂 button next to each toolbar inserts Discord's default (standard Unicode) emoji — not a server's custom/uploaded emoji, which would need per-guild fetching and image assets. Clicking it opens a small popover grid of ~80 commonly-used Unicode emoji (curated in a static `EMOJI_PICKER_LIST` in `common.js`, shared by both textareas), grouped under a few informal headings (Faces, Gestures, Symbols). Clicking an emoji inserts it at the textarea's current cursor position (`insertAtCursor()`), leaves the picker open for inserting more than one, and closes on an outside click. No new dependency or image assets — browsers render standard Unicode emoji glyphs natively, the same glyphs Discord's own client shows for non-custom emoji.

### 28.1 Out of Scope

- Resolving `<@&ROLE_ID>`/`<#CHANNEL_ID>` mentions server-side, anywhere — this remains an admin-preview-only convenience; the literal Discord markup is still what's stored and sent, and Discord's own client does the real resolution for the actual reader, same as before this section.
- A picker or preview for a server's custom (uploaded) emoji — deliberately scoped to Discord's default Unicode set only, per the user's request; custom emoji would need a per-guild fetch and image rendering this app has no other use for.
- Making the preview byte-for-byte identical to Discord's actual rendered output — it's an approximation for drafting purposes (most visible in the timestamp placeholders, which Discord renders in each *viewer's* own locale/zone, not the admin's), not a pixel-accurate Discord client reimplementation.
- Any preview or toolbar support in the pre-event Discord ping (`occurrences.py`'s `announce_msg`) or Event name/description fields — matching §27.3's existing scope boundary; only Announcement and AnnouncementTemplate bodies get the composer.

## 29. Coordinator Quality-of-Life Improvements

**Status:** Implemented, pending deployment. Client-side only — no migration, no new backend surface.

**Problem.** A set of small, repeated frictions in day-to-day coordinator use: picking the same alliance/channel combination by hand on every announcement, scrolling a growing Events or Post Log table with no way to narrow it, a Dashboard that goes blank by evening even when something's happening early the next morning, and occurrence times shown only as absolute UTC with no sense of "how soon."

**Design:**

- **Remembered last-used Discord channel per tenant.** A brand-new target row — on both the Event modal (`addEventTargetRow`/`populateEventTargetFields` in `events.js`) and the Announcement modal (`addAnnouncementTargetRow`/`populateAnnouncementTargetChannels` in `announcements.js`) — now defaults its channel dropdown to whatever channel was last picked for that tenant, via `getLastChannelForTenant()`/`setLastChannelForTenant()` in `common.js` (a `{tenantSlug: channelId}` map in `localStorage`, per-browser, never sent to the server). This only ever supplies a *default* for a row with no explicit channel — editing or duplicating an existing event/announcement still shows its real stored channel, untouched, since the caller only falls back to the remembered value when no `channelId` was passed in at all.
- **"+ Add all alliances"** button next to "+ Add another target" in the Announcement modal (`addAllAllianceTargets()` in `announcements.js`) — adds one target row per tenant not already present among the modal's current target rows, each defaulting to that alliance's own last-used channel (or "— none —" if it's never been posted to). Turns the common "post this to every alliance" case from one click-through per alliance into one click total.
- **Filter box on the Events and Post Log tables.** Both views now cache their last-fetched list (`EVENTS_CACHE`, `POSTLOG_CACHE`) and re-render through a small client-side text filter (`filterEventsTable()` matching event name or alliance name; `filterPostLogTable()` matching event name, poster, or status) rather than re-fetching — the same "cache the list, filter/re-render from it" pattern `ANNOUNCEMENTS`/`ANNOUNCEMENT_TEMPLATES` already use.
- **Dashboard "Upcoming (Next 48h)" section**, alongside the existing "Today's Events" — using the same already-fetched `GET /api/occurrences` data, filtered to occurrences starting within the next 48 hours whose date isn't today (so nothing appears twice between the two sections). Catches the coordinator checking in the evening before an early-morning event, when "Today" would otherwise be empty.
- **Relative-time badges** ("in 20 minutes", "3 hours ago") on Schedule table rows and both Dashboard sections, via a new shared `formatRelativeTime(date)` in `common.js` — the same threshold logic the announcement composer's preview (§28) already computed, now factored out so it has one implementation instead of two. The composer's own `formatDiscordRelativePreview()` becomes a thin wrapper adding its "(preview)" qualifier.

### 29.1 Out of Scope

- A full mobile-responsive layout pass on the admin UI — a real gap (PatternFly's table component doesn't collapse gracefully on a phone), but a layout project in its own right, not a quality-of-life patch.
- Server-side search/pagination for Events or Post Log — both filters are client-side over the already-fetched list; if either list grows large enough that fetching it all becomes slow, that's a separate, larger change (paginated endpoints), not part of this section.
- Remembering anything beyond the last-used channel (e.g. a remembered role, or per-user rather than per-browser preferences) — scoped to exactly the one repeated pick this section identified as friction.

## 30. Failed-Target Retry, CI, and Automated Backups

**Status:** Implemented, pending deployment. The application-code half (retry) needs no migration; CI and the backup script are operational additions, not app behavior — CI takes effect the moment `.github/workflows/ci.yml` lands on `master`, and the backup script needs a one-time `crontab` install on `lxc-taraka` (§30.2) that this patch cannot do by itself.

**Problem.** Three related operational gaps, each identified while reviewing the roadmap before this deploy: a one-time announcement that ends with some targets in `error` never gets reconsidered by anything — the coordinator's only recourse was re-creating the announcement from scratch; `pytest` only ran when it happened to be run locally before handing off a patch, so nothing caught a regression automatically between that and the manual production deploy; and Postgres backups only ever happened by hand, immediately before a schema migration — there was no routine backup a bad day (disk failure, an unrelated bug) could fall back on.

### 30.1 Retry Failed Announcement Targets

- New endpoint **`POST /admin/api/announcements/{id}/retry-failed-targets`** (`routers/admin/announcements.py`): resets every `AnnouncementTarget` on that announcement currently in `post_status == "error"` back to `"pending"` (clearing `status_detail` and `discord_message_id`), and — the part that actually matters — sets the announcement's own `status` back to `"scheduled"`, since `scheduler/announcements.py`'s delivery tick only ever looks at announcements with `status == "scheduled"`. `scheduled_for` is left untouched (it's already in the past, since the announcement already fired once), so the very next per-minute tick picks the retried target(s) back up. 400s with "No failed targets to retry" if there are none; 404s if the announcement doesn't exist or isn't owned by the calling tenant, same ownership check `cancel_announcement`/`delete_announcement` already use.
- This only matters for a **one-time** announcement. A *recurring* announcement already self-heals on its own next re-arm cycle — `send_scheduled_announcements` resets every target (including ones sitting in `error`) back to `pending` when it advances `scheduled_for` for the next occurrence (§13.5) — so this endpoint's real audience is a one-time announcement that went terminal (`failed`, or `posted` with some targets still in `error`) with nothing left to ever look at it again.
- **Admin UI**: a **Retry Failed** button appears in the Announcements table's Actions column whenever at least one target is in `error` and the announcement isn't `cancelled` (retrying a cancelled announcement would effectively un-cancel it, which isn't the intent) — `retryFailedTargets()` in `announcements.js`, calling the endpoint and reloading the list.

### 30.2 Continuous Integration

- New **`.github/workflows/ci.yml`**, two jobs on every push and pull request against `master`: `pytest` (installs `app/requirements-dev.txt`, runs the full suite — no Postgres service needed, since `tests/conftest.py` already uses an in-memory SQLite database) and `eslint` (`npm ci` + `npm run lint`, the same lint this repo's `package.json`/`eslint.config.mjs` already define and this session already ran by hand before every patch). This doesn't replace the existing patch-verification practice (commit, diff, clean-apply-in-a-worktree, full suite) for work delivered through this conversation — it's a second, automatic check once a patch actually lands on `master`, catching anything that practice might have missed.

### 30.3 Automated Nightly Backups

- New **`ops/backup.sh`**: runs `docker compose exec db pg_dump ...` (the exact command the deployment runbook already has the operator run by hand before a schema migration), gzips the output to `/opt/taraka/backups/kingshot_scheduler_<timestamp>.sql.gz`, and rotates anything older than 14 days. Runs on the **host** (the LXC), not inside the `app` container — `app`'s image (`python:3.13-slim`) has no `pg_dump`, and the `db` container (`postgres:16-alpine`) already has one and is already reachable via `docker compose exec`, so there's no reason to add a new dependency to the app image for this.
- **Requires a one-time manual install step during deployment** — this patch can land the script, but only the operator, on the actual host, can add the crontab entry: `chmod +x /opt/taraka/ops/backup.sh` then a `crontab -e` entry (`0 3 * * * /opt/taraka/ops/backup.sh >> /opt/taraka/backups/backup.log 2>&1`, full instructions in the script's own header comment). This is exactly the kind of decision a person has to make on their own machine, not something a patch applies for them.

### 30.4 Out of Scope

- Restore tooling/runbook — this section covers taking backups, not the (hopefully never-needed) restore procedure. Worth its own pass if it ever comes up for real, so the first time someone reads a restore runbook isn't during an actual incident.
- Off-box/offsite copies of the backup — `ops/backup.sh` only writes to local disk on the same LXC as the database, which doesn't protect against that LXC's own disk or host failing entirely. Adding an rclone/rsync/restic step to copy dumps elsewhere is a natural follow-up, deliberately left out here since it depends on where the operator wants them to land (already documented as a next step in the script's own comments).
- Automatic retry beyond the coordinator clicking the button — no background job re-attempts a failed target on its own; §30.1 makes retrying possible and visible, not automatic. An unattended auto-retry risks masking a real, recurring problem (a revoked bot token, a deleted channel) behind repeated silent retries instead of surfacing it.
- A CI gate that blocks merging on failure (branch protection rules) — that's a GitHub repository setting, not something this patch can configure from a file in the repo; the workflow runs and reports either way, and turning failures into a hard block is a one-click follow-up in the repo's own settings if wanted.

## 31. Audit Log Viewer, Delivery Health Card, and Read-Only Viewer Role

**Status:** Implemented, pending deployment. §31.3's migration script must run against production before the new application code is deployed, same order as every other schema-changing migration in this repo.

**Problem.** Three related medium-value roadmap items, bundled into one patch since two of them (audit log, viewer role) touch the same access-control surface: `services/audit.log_change` has written to `audit_log` from six router call sites since early in this app's life, but nothing ever reads it back — an owner has no way to actually see who changed what. Delivery success/failure for both Announcements and event Discord posts is visible only by opening the Announcements list or Post Log and reading per-row status — there's no at-a-glance rollup for "is delivery generally healthy right now." And every tenant grant is either full read/write access (`coordinator`) or full access plus member management (`owner`) — there was no way to give someone (an officer who wants visibility, a departing coordinator being wound down gently) access to look without being able to change anything.

### 31.1 Audit Log Viewer

- New **`GET /admin/api/audit-log`** (`routers/admin/audit_log.py`), owner-only (`require_tenant_owner` — same tier as `/api/members`, since an audit trail of who-changed-what is an owner-level concern by the same reasoning member management already is). Tenant-scoped by `AuditLog.tenant_id`; a superadmin-only global action logged with `tenant_id=None` (e.g. removing a kingdom-coordinator grant) is invisible here by design — this endpoint has no "platform-wide" mode, matching every other tenant-scoped list in this app. Returns each row's timestamp, the acting user's Discord username (resolved via an outer join — a row with `user_id=None`, if one is ever written, shows `null` rather than 404ing or omitting the row), table name, row id, action, and the `before`/`after` JSON blobs parsed back into objects (they're stored as serialized text per `AuditLog`'s own docstring — §4).
- No write/delete surface — `audit_log` was already documented as append-only and explicitly not a rollback mechanism (§22); this section only adds a way to read it, nothing that could mutate it.
- **Admin UI**: a new **Audit Log** tab (owner-only visibility, same `applyRoleVisibility()` condition as Access), listing entries newest-first with a compact per-row diff (only the fields that actually changed between `before`/`after`, not the full raw JSON — a real diff viewer is more machinery than a first version needs).

### 31.2 Delivery Health Card

- New **`GET /admin/api/delivery-health`** (`routers/admin/status.py`, alongside the existing `/api/status`), current-tenant-scoped like everything else in that file. Aggregates two independent counts over a trailing 7-day window: `AnnouncementTarget.post_status` for this tenant's announcement deliveries (windowed by the parent `Announcement.posted_at`, since the per-target row itself carries no timestamp), and `PostLog.status` for this tenant's event Discord posts (windowed by its own `posted_at_utc`). Returns `{window_days, announcements: {posted, error}, event_posts: {posted, error, cancelled}}`.
- **Admin UI**: a **Delivery Health** card on the Dashboard, below the existing Upcoming (§29) section — one row per channel (Announcements, Event Posts) showing a posted-count badge, an error-count badge when nonzero, and a success-rate percentage, or "No activity in the last 7 days" when there's nothing to rate. This is deliberately a rollup, not a drill-down — a coordinator who sees failures here still goes to Announcements or Post Log to act on the specific one, same as today.

### 31.3 Read-Only Viewer Role

- `user_tenants.role` and `invites.role` both gain `viewer` as a third valid value (`ck_user_tenant_role`/`ck_invite_role` CHECK constraints widened accordingly). Migration: `migrate_add_viewer_role.py` — idempotent (reads each constraint's current definition via `pg_get_constraintdef` and only widens it if `'viewer'` isn't already present), verified end-to-end against a real Postgres instance (fresh pre-migration schema, migrate, re-run to confirm no-op, confirmed the widened constraint still rejects a bogus role value) before this section's status changed to Implemented.
- New dependency **`require_not_viewer`** (`routers/admin/deps.py`) — the mirror image of `require_tenant_owner`: superadmin bypasses, otherwise a `viewer` grant on the current tenant 403s ("Viewer access is read-only") and any other role (or no explicit grant row, which shouldn't reach this dependency at all since `get_current_tenant` already gates that) passes through. Unlike `require_tenant_owner` this isn't a match-required lookup (`owner` and `coordinator` both pass; only `viewer` is rejected), so it's a rejection check rather than a role-equality check.
- Swapped onto every mutating (POST/PATCH/PUT/DELETE) endpoint that previously depended on plain `get_current_tenant`: all of `events.py`, `announcements.py` (including §30.1's `retry-failed-targets`), `announcement_templates.py`, `occurrences.py`'s three write endpoints (`update_occurrence`, `post_occurrence`, `cancel_occurrence_discord` — the first two took no tenant dependency at all before this and gained one purely for this check, via a plain `_: Tenant = Depends(require_not_viewer)`), all four of `discord_sync.py`'s POST endpoints, and `scheduler_control.py`'s `manual_regenerate`. Every GET/list endpoint in these routers is untouched — a viewer still passes `get_current_tenant`'s plain access check on those, same as a coordinator would. `invites.py`'s member-management endpoints were already `require_tenant_owner`-gated (stricter than `require_not_viewer`, since a viewer isn't an owner either), and `tenants.py`'s Kingdom/Tenant/DiscordServer CRUD is already superadmin-only — neither needed changes.
- **Admin UI**: the Access tab's **Change role** action becomes a three-way `prompt()` (`"owner"`/`"coordinator"`/`"viewer"`, pre-filled with the person's current role) in place of the old blind owner↔coordinator toggle, and the invite-creation prompt's validation accepts `"viewer"` alongside the existing two values (`access.js`). No change was needed in `routers/auth.py`'s invite-claim flow — it already passes `invite.role` through generically to the new `UserTenant`/existing grant row, special-casing only `kingdom_coordinator`.
- A `viewer` still counts toward `_sole_owner()`'s denominator the same as any non-owner grant does (i.e., not at all) — demoting or removing the only remaining `owner` is still blocked regardless of what role they'd become, unchanged from §21.1.

### 31.4 Out of Scope

- Exporting the audit log (CSV, like Post Log's export) — the in-app viewer covers the stated need; add an export if it comes up for real.
- Any retention/pruning policy for `audit_log` — it grows unbounded today and this section doesn't change that; a real gap if the table ever gets large enough to matter, but not part of this patch.
- A finer-grained Delivery Health breakdown (per-alliance in combined mode, per-event-type, a trend chart over time) — the single current-tenant 7-day rollup answers "is anything on fire right now," which is what was asked for; a historical/trend view is a larger, separate feature.
- Letting a `viewer` see the Access, Audit Log, Config, Sync, or Platform tabs — `applyRoleVisibility()`'s existing owner-only/superadmin-only gates are unchanged; a viewer's read access is scoped to the same views a coordinator already sees (Dashboard, Events, Announcements, Schedule, Gantt, Post Log), just without any of that content's write actions available.
- A `viewer`-specific UI treatment (e.g. hiding Create/Edit/Delete buttons entirely rather than letting them 403) — out of scope for this pass; a viewer clicking a write action today gets the same 403 toast any permission failure produces, not a dedicated "you're read-only" affordance. Worth a follow-up UI pass if viewers turn out to be a commonly-used role in practice.

## 32. Bulk Event Import/Export, and Placeholder Support in the Pre-Event Ping

**Status:** Implemented, pending deployment. No migration — both halves are additive endpoints/behavior on existing tables.

**Problem.** Two small, unrelated roadmap items bundled into one patch since both are the last of this round's build list. First: setting up a season's worth of recurring events one at a time through the modal is repetitive busywork for anything beyond a handful of events, and there was no way to get an alliance's events *out* of the system either (to hand to another alliance standing up the same schedule, or just to back up outside the app). Second: §27 gave Announcements six `{placeholder}`s (alliance/kingdom name, send/event time and their relative forms) but scoped them to Announcement bodies only (§27.3) — an Event's own `description`, which reaches Discord in two places (the Scheduled Event's own description field, and now the pre-event ping), stayed literal text with no way to reference "when this actually starts" without hand-typing a UTC time that goes stale the moment the schedule changes.

### 32.1 Bulk Event Import and Export

- **New `GET /admin/api/events/export.csv`** (`routers/admin/events.py`) and **`POST /admin/api/events/import.csv`**, a matched pair per the user's explicit request that import be "bundled with" export rather than shipped alone. Both use one shared column set (`_BULK_EVENT_COLUMNS`): `name, interval_days, start_time_utc, duration_hours, discord_channel, description, leadership_only, anchor_date, notification_channel_id, notification_role_id, notify_minutes_before` — deliberately excludes `scope` and `targets`. A kingdom-wide event's automatic same-Kingdom fan-out and an explicit `EventTarget` list (§20) are both multi-row, cross-tenant concepts that don't flatten into one CSV row per event without either lying about what a plain re-import would reconstruct or inventing a second, more complex file format nobody asked for — so bulk import/export is scoped to **alliance-scope events with no explicit targets**, full stop. Anything more exotic still goes through the modal, one at a time, same as today.
- **Export** (`GET`, `get_current_tenant`-scoped, read-only) selects the current tenant's own `scope == "alliance"` events, ordered by name, and streams them as CSV via the same `StreamingResponse` pattern `post_log.py`'s existing export already uses.
- **Import** (`POST`, `require_not_viewer`-gated since it's a write, multipart file upload via a new `UploadFile` parameter — this repo's first, so `python-multipart` is now a runtime dependency) parses the uploaded file with `csv.DictReader`, 422s up front if any required column is missing, and otherwise validates **each row independently** by constructing an `EventIn` (reusing every validator the single-event create form already enforces, rather than a second copy of the same rules) — a bad row doesn't abort the whole file. Every event is created with `scope` forced to `"alliance"` regardless of what the CSV might claim (there's no `scope` column at all, so this is moot in practice, but the endpoint doesn't trust row content it didn't ask for). The response is `{created: N, errors: [{row, detail}]}`, `row` being the CSV's own 1-indexed row number (header = row 1, so the first data row is row 2 — matching what a coordinator sees opening the file in a spreadsheet) so a partially-bad file can be fixed and re-uploaded without guessing which lines failed.
- **Import is additive only, never an upsert** — re-importing the same file twice creates duplicate events by design, matching how every other create endpoint in this app behaves (there's no existing merge-by-name convention anywhere to be consistent with, and inventing one here risks silently overwriting an event someone's since edited by hand).
- Each successfully-created row is logged individually via `log_change` with `after: {..., "source": "bulk_import"}`, so the Audit Log (§31.1) can distinguish a bulk-imported event from one created through the modal.
- **Admin UI**: **Export CSV** and **Import CSV** buttons next to "+ Add Event" (`exportEventsCsv()`/`importEventsCsv()` in `events.js`) — export follows the same fetch-blob-download pattern `postlog.js`'s existing CSV export already uses; import is a hidden `<input type="file">` triggered by the visible button, posting the chosen file as `FormData` (outside the `api()` helper, which always sends JSON) and reporting `{created, errors}` back as a toast, with the full per-row error list also logged to the browser console for a bulk failure too large to fit in a toast.
- **Round-trip guarantee**: export's column order and format is exactly what import expects, so "export, tweak a few rows in a spreadsheet, re-import" works with no reshaping — the property this section is actually valuable for (e.g. copying one alliance's event lineup to a new alliance being onboarded), rather than "each direction happens to exist."

### 32.2 Placeholder Support in the Pre-Event Ping and Event Description

- `Event.description` now has the same six `{placeholder}` names §27 built for Announcements (`{alliance_name}`, `{kingdom_name}`, `{send_time}`, `{send_time_relative}`, `{event_time}`, `{event_time_relative}`) resolved through it, via the same `services/templates.render_placeholders()` — no second implementation. Resolution happens once per post attempt in `_post_to_one_tenant` (`routers/admin/occurrences.py`), before either place `description` reaches Discord, so both destinations see the identical resolved text:
  - The Discord **Scheduled Event's own description field** (`create_discord_event(..., description=resolved_description)`), previously the raw literal text.
  - A new extra line in the **pre-event ping** itself (`announce_msg`), which previously carried no user-authored text at all — just the event name, date/time, channel, and a "click Interested" footer. A coordinator can now write something like `"Bring siege gear! Starts {event_time_relative}"` in the description and have it land directly in the role-pinged channel message, not just buried in the Scheduled Event's own detail view.
  - For a kingdom-wide event's fan-out, resolution happens **once per target tenant** (same loop `_post_to_one_tenant` already runs per target) so `{alliance_name}` still means the alliance actually being posted to, matching how Announcements' per-target resolution already works (§27's "resolution happens per-target, at delivery time" is unchanged in spirit — this just extends which delivery path it applies to).
- **What `{send_time}`/`{event_time}` mean here, since an Event (unlike an Announcement) has no independently-scheduled "send time" of its own**: `send_time`/`send_time_relative` resolve to *now* (the actual moment this post/ping is going out), and `event_time`/`event_time_relative` resolve to `occ.start_datetime_utc` — computed by passing `render_placeholders()` an `event_offset_minutes` equal to the live minutes between now and the occurrence's own start, rather than a stored offset field the way Announcements use one. In practice `{event_time_relative}` is the placeholder actually worth using here (e.g. "Siege begins {event_time_relative}"); `{send_time}` exists mainly for symmetry with the Announcement placeholder set rather than because posting time is something worth calling out in an event's own description.
- Unrecognized `{whatever}` text is left untouched, same as §27 — an event description with no placeholders in it at all (the overwhelming majority, retroactively) is unaffected byte-for-byte.
- No UI change beyond documentation: the Event modal's Description field gains the same short placeholder-list helper text the Announcement Body field already has (§27.2), so a coordinator discovers the feature without needing to read this spec.

### 32.3 Out of Scope

- Bulk import/export for kingdom-wide events or events carrying explicit `EventTarget` rows — per §32.1, these don't flatten into one CSV row without either lying about the reconstruction or a second, more complex file format; use the modal for these.
- Bulk *editing* (importing a CSV that updates existing events by name/id rather than only creating new ones) — import is additive-only by design; an upsert mode is a natural but materially different follow-up, not requested here.
- A preview/dry-run step before committing an import (uploading a file and seeing what *would* happen before it actually creates anything) — the per-row error reporting already tells a coordinator what failed after the fact; a true dry-run is more machinery than this version needs, and a bad import can already be cleaned up row-by-row via the existing Delete action.
- Placeholders in Event **names** or Announcement **titles** — unchanged from §27.3's existing scope boundary; both remain literal, since titles/names are typically short identifiers shown in admin lists, not viewer-facing prose.
- A dry-run or live preview of the resolved Event description before saving (mirroring §27.3's explicitly-deferred Announcement preview, later actually built in §28 once target rows existed to preview against) — an Event has no per-post "preview as" context the way a multi-target Announcement composer does, and this section's scope was placeholders working correctly at send time, not a composer rebuild for Events.

## 33. Post-Deployment Bug Fixes: Template Use, Target Channel Validation, Cross-Server Channel Errors

**Status:** Implemented, pending deployment. Client-side fixes plus one server-side error-message improvement — no migration.

**Problem.** Three issues reported after §27–§32 went live:

1. **The Templates table's "Use" button did nothing.** `useTemplate()` (`announcements.js`) opens the New Announcement modal pre-filled from a template-shaped object carrying only `{title, body_markdown, leadership_only, event_offset_minutes}` — a template has no `targets` array at all, since targets are deliberately never copied from a template (§27.2). `openAnnouncementModal()` unconditionally read `source.targets.length` before checking `source.targets` was defined, so every call from `useTemplate()` threw a `TypeError` and the modal never opened — silently, since the throw happened before `classList.add('open')` ran and nothing else was listening for it. `duplicateAnnouncement()`'s source objects (real Announcements) always have a `targets` array, which is why Duplicate worked fine and only Use was affected.
2. **A new announcement's target row can silently fail to save.** A brand-new target row defaults to that tenant's last-used Discord channel (§29) or, the first time ever posting to a tenant, to a "— none —" placeholder option — visually indistinguishable from a real selection. Saving with that row untouched correctly refused with a toast ("Every target needs a Discord channel selected"), but nothing about the row itself indicated *which* target was the problem, and a transient toast is easy to miss while looking at the form fields — this is what read as the announcement "not saving" for no visible reason.
3. **Channel-list load failures for a target tenant gave no indication of the actual cause.** Selecting a target tenant whose Discord channels fail to load (e.g. `GET /api/discord/channels` 403s because the caller lacks `UserTenant` access to that tenant, or 502s because that tenant's own Discord server/bot rejected the call) fell back to a manual channel-ID text field with a generic "Could not load Discord channels for X" toast — the same message for two failure modes that need entirely different fixes (get access granted to that tenant, vs. fix that tenant's Discord server/bot configuration), which is exactly the ambiguity behind the "can't see another alliance's channels" report. Note that seeing another tenant's channels in this picker was already, and remains, gated on holding `UserTenant` access to that tenant (`get_current_tenant`'s own access check, shared by every per-tenant Discord call) — that access boundary is intentional (§13.3: "a user can only ever pick tenants they hold UserTenant access to") and this section does not change it; it only makes it visible when that's the actual reason a channel list didn't load.

**Fixes:**

- `openAnnouncementModal()`'s targets check is now `source && source.targets && source.targets.length` — a template source (or any object with no `targets` key at all) correctly falls through to the fresh-row default, same as calling the modal with no source.
- `saveAnnouncement()` now marks every announcement-target-row missing a channel with a `target-row-invalid` CSS class (a red outline on the channel select/fallback input) before showing the validation toast, and scrolls the first invalid row into view. The class clears the moment that row's channel is actually picked (or typed into the fallback field), so the highlight tracks the live state of the form rather than freezing at the moment Save was clicked.
- `populateAnnouncementTargetChannels()`'s failure toast now includes the server's actual error message (`e.message`, already available from `api()`'s thrown `Error` — previously discarded) alongside the tenant slug, so a 403 ("You don't have access to this tenant") and a 502 (a Discord API failure from that tenant's own bot/server config) read as the two different problems they are, instead of one generic "could not load" line.

### 33.1 Out of Scope

- Changing which tenants a coordinator can target or preview channels for — the `UserTenant`-access boundary (§13.3) is unchanged; this section only makes a failure inside that boundary diagnosable instead of silent.
- Auto-selecting a real channel instead of defaulting to "— none —" on a tenant's first-ever target row — §29's documented behavior for that case is unchanged; the fix here is making the empty state visibly wrong when saved, not picking a channel on the coordinator's behalf.

## 34. Composer Enhancements: Headings, Role Mention Picker, Event Description Composer

**Status:** Implemented, pending deployment. Client-side only — no migration, no new backend tables.

**Problem.** Three related composer gaps surfaced once §28's toolbar/preview and §32's Event-description placeholders had both been in use for a while:

1. **No way to insert a heading.** `renderDiscordMarkdownPreview()` has always resolved `#`/`##`/`###` lines to `<h1>`/`<h2>`/`<h3>` (§28's own text lists "headers" among the syntax it covers), but the toolbar itself — Bold/Italic/Underline/Strikethrough/Spoiler/Quote/Inline code/Code block — never grew a matching button, so a coordinator had no way to discover the feature short of already knowing Discord supports `# ` headers and typing it by hand. Separately, the header regexes anchored on `$` rather than consuming the line's own trailing newline, so the preview's "remaining newlines become `<br>`" pass added an extra line break after every heading on top of the `<h1>`/`<h2>`/`<h3>` tag's own block margin — a real (if minor) rendering glitch, fixed alongside the missing button since both are in the same function.
2. **Typing a role mention by hand means looking up a numeric Discord role ID** — exactly the kind of manual, error-prone step the rest of the composer (channel/role *preview* resolution, §28) exists to reduce, but nothing let a coordinator actually *insert* a correct mention; the preview would only render one correctly after the fact.
3. **Event descriptions had none of §28's composer** (toolbar, live preview, emoji picker) — an explicit exclusion at the time (§28.1: "Any preview or toolbar support in ... Event name/description fields"), reasoned as out of scope because "an Event has no per-post 'preview as' context the way a multi-target Announcement composer does" (§32.3). That reasoning no longer holds now that §32 gave Event descriptions the same six placeholders Announcements have, resolved against a single owning tenant (or each fan-out target tenant in turn) — exactly the "preview as one tenant" shape the Template modal's composer (§28) already handles, not the multi-target shape that motivated the original exclusion.

**Design:**

- **Heading button.** A **H** button in every markdown toolbar (Announcement Body, Template Body, and now Event Description — see below), next to Quote since both are line-level prefixes: `prefixSelectedLines(textareaId, '# ')`, reusing the exact helper Quote already uses. `renderDiscordMarkdownPreview()`'s three header regexes now consume their line's trailing `\n` (`/^# (.*)\n?/gm` in place of `/^# (.*)$/gm`, same change for `##`/`###`) instead of leaving it to become a spurious `<br>`. The Body helper text's inline markdown reminder now lists `# heading` alongside the existing bold/italic/etc. examples.
- **Insert role mention.** A small **"Insert role mention…"** `<select>` next to the emoji picker button in every toolbar (`aRoleMention`/`tRoleMention`/`mRoleMention`), populated with `@RoleName` options for whichever tenant the composer's own **Preview As** selector currently points at — refreshed by `refreshRoleMentionSelect()` inside `renderComposerPreview()` itself, reusing the exact `ensurePreviewRolesLoaded(tenantSlug)` call (and its per-tenant cache) the live preview's own mention-resolution already makes, so this adds zero new API calls. Picking a role calls `insertAtCursor(textareaId, '<@&' + roleId + '>')` — the raw Discord mention syntax, immediately visible in the live preview as a resolved `@RoleName` pill (§28's existing mention rendering) — and resets to the placeholder option so it reads as a one-shot insert action, not a persistent field.
- **Event Description composer.** The Event modal's Description field gains the identical toolbar (now including Heading and Insert-role-mention), a live preview pane, and the emoji picker — all wired through the same `renderComposerPreview()` helper Announcements/Templates already share, via a new `renderEventDescriptionPreview()` entry point and a new **Preview As** selector (`mPreviewTenant`, populated from the current tenant only — an Event modal has no multi-target row list the way the Announcement modal does, matching how the Template modal's own preview already defaults to the current tenant). The one real difference from the Announcement/Template preview: an Event has no independently-scheduled "send time" of its own (§32.2 already established this for the actual send path) — the composer's `renderComposerPreview()` now accepts an `eventDateId`/`eventTimeId` pair (wired to the modal's existing **Anchor Date** and **Start Time** fields) as an alternative to the Announcement/Template's `dateId`/`timeId` + `offsetId`: in this mode, `{send_time}` always previews as *now*, and the offset between *now* and the Anchor Date/Start Time combination becomes the `eventOffsetMinutes` fed into the exact same `clientRenderPlaceholders()` math every other preview already uses for `{event_time}` — no second placeholder-resolution implementation, just a different way of arriving at the same two inputs (`scheduledFor`, `eventOffsetMinutes`) the shared function already takes. With no Anchor Date/Start Time set yet, the preview falls back to "now" for both and shows a note to that effect, same pattern as the Announcement composer's own "no send time set yet" fallback.

### 34.1 Out of Scope

- Custom (server-uploaded) emoji or role/channel mention resolution beyond what §28 already built — this section extends where the existing composer appears and what its toolbar can insert, not what the composer itself is capable of rendering.
- A role-mention picker or composer for the pre-event ping's own fixed text (the "📅 **{name}** has been scheduled" line in `occurrences.py`) — only the Event's own `description` field (and Announcement/Template bodies) are user-authored text; the ping's surrounding message structure remains generated, matching §27.3/§32.3's existing scope boundary for what counts as composable content.

## 35. Event Cover Images

**Status:** Implemented, pending deployment. §35.1's migration script must run against production before the new application code is deployed, same order as every other schema-changing migration in this repo.

**Problem.** A Discord Scheduled Event supports a cover image (shown on the event's own detail page and in the guild's Events list), and this app had no way to set one — every event this app posts has always gone out with Discord's own default placeholder image, and there was no field anywhere in the Event modal for it.

**Design:**

- New `EventDefinition.cover_image_data` (nullable `Text`): the event's cover image, stored as the **full data URI** the browser's own `FileReader.readAsDataURL()` produces (`data:image/png;base64,...`) — deliberately not a bare base64 blob or a separately-tracked MIME type, since a data URI is exactly the string Discord's Scheduled Event `image` field itself expects, so the value the admin UI reads from a chosen file is the same value that reaches Discord with no server-side re-encoding step in between. `NULL`/empty means no cover image, matching `description`'s own empty-means-nothing convention.
- **Validation** (`services/validators.py`'s new `parse_cover_image_data`): accepts `data:image/(png|jpeg|jpg|gif);base64,<...>` only, capped at 8MB of base64 text (a sanity ceiling against an obviously-wrong upload, e.g. a video file selected by mistake — not a re-derivation of Discord's own actual size/format limits, which Discord's API enforces itself and remains the real source of truth for "too big" or "wrong format" once the request reaches it). Shared by `EventIn` (create) and `EventPatch` (update) the same way every other event field validator already is.
- **The PATCH null-vs-empty-string distinction**, needed because `cover_image_data` is genuinely nullable (unlike `discord_channel`/`description`, which are non-nullable columns defaulting to `""`): omitting the field from a PATCH request (or sending it as JSON `null`) leaves the event's current image untouched; sending it as an explicit empty string `""` clears it. This is the same "empty string means explicitly clear" shape `EventPatch` already uses for its non-nullable text fields, adapted so a *nullable* field gets an unambiguous third state (unchanged / cleared / set-to-this-value) instead of collapsing "leave alone" and "remove" into the same `None`.
- **`create_discord_event()`/`update_discord_event()`** (`services/discord_api.py`) both gain an optional `image` parameter, included in the Discord API payload only when truthy — omitted entirely (not sent as `null`) when there's no image, matching how these payloads already omit fields Discord doesn't need. `_post_to_one_tenant` (`routers/admin/occurrences.py`) passes `event.cover_image_data` straight through on every post, for every target tenant (owning, kingdom-wide fan-out, and explicit `EventTarget`s alike) — one image per event, shown identically across every guild it posts to, same as the event's name/description/location already are.
- **Admin UI**: a new **Cover Image** field in the Event modal (`events.js`'s `handleCoverImageFile()`/`removeCoverImage()`/`setCoverImagePreview()`), between the composer preview and the Notification Channel field — a file picker (`accept="image/png,image/jpeg,image/gif"`) that reads the chosen file client-side via `FileReader.readAsDataURL()` into a hidden field, a thumbnail preview of whatever's currently set (existing image on edit, or the just-picked file), and a **Remove** button that clears it. A client-side 8MB file-size check (`COVER_IMAGE_MAX_BYTES`) rejects an obviously-oversized pick before it's ever read into memory as base64, with the same ceiling the server-side validator enforces. `duplicateEvent()` carries the source event's cover image forward into the copy, same as every other field it duplicates.

### 35.1 Migration Plan

`migrate_add_event_cover_image.py` — idempotent, hand-written (§22's established convention): adds `event_definitions.cover_image_data TEXT`, nullable, no default. No existing rows are affected — every event predating this field has no cover image, matching its actual behavior (there was no way to set one before now).

### 35.2 Out of Scope

- Editing the cover image on an **already-posted** Discord Scheduled Event through the Sync tab's drift-reconciliation "push" action (`discord_sync.py`'s `update_discord_event` calls) — `image` is a supported parameter there for future use, but this app's own `GET` of a Discord Scheduled Event doesn't return enough information (Discord returns an opaque image *hash*, not the original bytes) to detect image drift the way it already detects name/description/location drift, so cover-image mismatches aren't part of the Sync tab's comparison. Changing an already-posted event's image today means cancelling and re-posting it.
- Server-side image resizing, cropping, or re-encoding — whatever the admin uploads (subject to the format/size checks above) is exactly what Discord receives; getting the aspect ratio Discord recommends (16:9) is left to the admin picking a reasonably-shaped source image, same as this app has never resized a Discord avatar/icon anywhere else either.
- Bulk import/export (§32.1) carrying cover images — `_BULK_EVENT_COLUMNS` is unchanged; a multi-megabyte base64 blob per row would make the CSV format actively worse for its one real use case (skimming/editing event lineups in a spreadsheet), so an imported event always starts with no cover image, set afterward through the modal if wanted.

## 36. Public Events Page: Off-Center Layout Fix

**Status:** Implemented, pending deployment. CSS-only — no migration, no backend change.

**Problem.** The public `/t/{tenant_slug}/events` page rendered visibly off-center — a gap of gray page background on the left of the white content card, and a larger gap on the right — present whether or not the page had any events to show, so it wasn't a content-length artifact. This is the exact PatternFly page-layout quirk `admin.html` already worked around (see that file's own CSS comment, same root cause): `.pf-v6-c-page` is CSS grid with `header`/`sidebar`/`main` template areas, and with no sidebar element on the page, `.pf-v6-c-page__main-container`'s default "page chrome inset" margin — identical on both inline-start and inline-end — renders asymmetrically: a visible gap on the start side, while the same margin on the end side pushes the container past the viewport edge, where the element's own `overflow-x:hidden` silently clips it. One side shows the gap, the other doesn't, even though the underlying value is the same on both — that asymmetry is what read as "off-center." `events.html` uses the identical `.pf-v6-c-page` scaffold as `admin.html` but never received this fix when `admin.html` got it.

**Fix.** Ported `admin.html`'s exact override into `events.html`'s `<style>` block: `.pf-v6-c-page { display: flex; flex-direction: column; min-height: 100vh; }` plus zeroing `.pf-v6-c-page__main-container`/`.pf-v6-c-page__main`'s margin and forcing `width: 100%`, in place of fighting PatternFly's grid-template-areas token directly. `admin.html`'s version also protects a tab bar (`.pf-v6-c-tabs`) from the same grid auto-placement issue; `events.html` has no tab bar, so only the `.pf-v6-c-page`/`__main-container`/`__main` rules were needed here.

### 36.1 Out of Scope

- Auditing every other PatternFly component on either page for the same grid-auto-placement class of bug — this fix addresses the one reported symptom (the page-level container), not a general PatternFly-hardening pass.

## 37. Public Events Page: Announcements and Notification Lead Time

**Status:** Implemented, pending deployment. Server-side query/serialization change plus client-side rendering — no migration, no new tables (every field used already existed).

**Problem.** Two gaps reported once the public events page (§26) had been in use for a while:

1. **Scheduled announcements — Discord-only text posts with no start/end time, created and managed under the Announcements tab — never appeared on the public `/t/{tenant_slug}/events` page**, even though they're a real, admin-scheduled thing members would want to know is coming (e.g. a "Daily Reset Warning" that recurs every day). The public page's data endpoints (`list_events`/`list_events_all` in `routers/events.py`) only ever queried `Occurrence`/`EventDefinition` — they had no knowledge of `Announcement`/`AnnouncementTarget` at all.
2. **Neither an event nor an announcement showed how far ahead of the thing itself a Discord notification actually goes out.** `EventDefinition.notify_minutes_before` (§-prior, driving `scheduler/reminders.py`'s pre-event reminder ping) and `Announcement.event_offset_minutes` (§27, "warn ahead of an event") both already answer exactly this question for their respective row types, but neither was exposed on the public page — a member had no way to tell, at a glance, "will I get pinged before this starts, and how early?"

**Design:**

- **Announcements on the public page, kept out of the ICS feed.** `routers/events.py`'s `_announcement_row_dict()` shapes a scheduled or already-posted `Announcement` into the same row structure an event row uses (`occurrence_date`, `start_datetime_utc`, `description`, `post_status`, etc.) so the client can group, sort, and render both kinds through one code path — distinguished by a new `"kind"` field (`"event"` or `"announcement"`) on every row. `scheduled_for` (a `DateTime(timezone=True)`) supplies both the grouping date and the display time, in place of an event row's `occurrence_date`/`start_datetime_utc`; `end_datetime_utc` and `duration_hours` are `null` (an announcement has neither). `routers/ics.py` is untouched — it never imports `Announcement` and was already a fully separate query path, so this is enforced by construction, not by an extra filter to remember: an announcement was never a calendar event (no duration/end time), so it was never a candidate for the ICS feed in the first place, independent of this section's public-events-page change.
  - **Scoping.** `/t/{tenant_slug}/api/events` includes an `Announcement` when it has an `AnnouncementTarget` row naming that tenant — the same targeting relationship the admin Announcements tab already uses to fan a post out to one or more Discord servers, so "is this announcement relevant to this alliance's page" already has an exact, existing answer with no new concept needed. The combined `/api/events` includes every tenant's announcement targets, one row per (announcement, target tenant) — mirroring PostLog's existing per-tenant fan-out semantics (an announcement sent to three Discord servers is three independent posts) rather than an event's single-row-per-kingdom-wide-Occurrence shape, since an Announcement has no equivalent "one Occurrence, several tenants" structure to begin with.
  - **Status/visibility filtering**, matching the existing event-row filters' spirit: `leadership_only == True` announcements are excluded (mirrors `EventDefinition.leadership_only`'s existing public-page exclusion exactly — both are the same "categorization flag, not a security boundary" per their own docstrings, but public visibility already treats it as "don't show this to the general public" either way). Only `status IN ('scheduled', 'posted')` are shown — `'draft'` isn't a real reachable state today (nothing in the admin UI creates one) and `'failed'`/`'cancelled'` have no useful "when will this happen" information left to display, unlike a cancelled *event*, which still shows (with a "Cancelled" badge) because its Occurrence still has a fixed, informative time slot. Windowed the same way events are: `scheduled_for` within `[today, today + WINDOW_DAYS]`.
  - **No Discord channel is shown on an announcement row.** Unlike `EventDefinition.discord_channel` (an admin-typed free-text display name), `AnnouncementTarget.discord_channel_id` stores only the raw numeric Discord snowflake — not something worth showing on an unauthenticated page with no bot-API access to resolve it to a human name.
- **Visual distinction (client-side, `events.html`).** An announcement row gets a purple "📢 Announcement" label (`announcementKindBadge()`) next to its title and a purple left-border accent on its card (`.samaya-announcement-card`), and uses its own status badge vocabulary (`announcementStatusBadge()`: Scheduled/Announced/Failed/Cancelled) rather than reusing `statusBadge()`'s event-shaped one (pending/posted/active/completed/cancelled) — the two post-status vocabularies mean genuinely different things and were never meant to share a mapping. An announcement row also skips the ⏱ duration meta item, since `duration_hours` is `null` for it.
- **Notification lead time.** Event rows now carry `notify_minutes_before` (straight from `EventDefinition`, already used server-side by `scheduler/reminders.py` — nothing new stored, just newly serialized here) and announcement rows carry `event_offset_minutes` (from `Announcement`, already existing per §27). Both answer the same question — "how far ahead of the thing itself does a Discord notification go out" — through a single shared renderer, `notifyBadge(ev)`: for an event, "🔔 Reminder *N* min before"; for an announcement, "🔔 Sent *N* min before event". Renders nothing when the underlying value is unset or `0`, same as every other optional meta item already on this page (e.g. `discord_channel`), so the overwhelming majority of events (which have no reminder configured) and announcements with no referenced event (`event_offset_minutes = 0`, the default — meaning "the announcement's own send time *is* the moment being announced") are unaffected.

### 37.1 Out of Scope

- A separate "announcements only" or "events only" filter/toggle on the public page — both kinds share one chronological list by design (the whole point was surfacing announcements *alongside* events, not adding a second page to check).
- Rendering an announcement's `body_markdown` through Discord-flavored markdown (bold/italic/headers/etc.) on the public page — shown as plain escaped text, exactly how an event's own `description` is already shown here (§26 never gave event descriptions markdown rendering on the public page either, only in the admin composer's own preview pane, §28).
- Resolving `AnnouncementTarget.discord_channel_id` to a human-readable channel name for display — would need an authenticated Discord API call this public, unauthenticated page has no business making; the field is simply omitted from announcement rows, as above.
- A reminder/lead-time notification system for announcements analogous to `scheduler/reminders.py`'s pre-event ping — `event_offset_minutes` already describes an existing, different mechanism (how far the announcement's *own send* is offset from an event it references); this section only exposes that existing number, it doesn't add a second Discord message type for announcements.

## 38. Admin Console Consolidation: Cross-Kingdom Default Views

**Status:** Implemented.

**Problem.** With enough alliances now live under Kingdom 138, the admin console's one-tenant-at-a-time model (pick an alliance in the header, every page shows only that alliance) made it hard for a kingdom-level coordinator to see what's happening across alliances without repeatedly switching. This section flips most admin pages from "single alliance, switch to see another" to "every alliance you have access to, by default — narrow with a per-page filter when you actually want one alliance," while leaving the public-facing pages (§26, §37) structurally the same (they gain alliance icons and a last-activity marquee — §38.9 — but keep their existing per-alliance/combined URL split).

**What made this feasible without new access-control work:** the app already had `get_current_tenants` (`routers/admin/deps.py`) — given `X-Tenant-Slug: *` it returns every tenant a superadmin can see (all of them) or every tenant a regular user holds a `UserTenant` grant for, and given a real slug it behaves exactly like `get_current_tenant`. This section is a UI/aggregation change built on that existing dependency, not a new permissions model — `UserTenant.role` (owner/coordinator/viewer) and the write-path dependencies that check it (`require_not_viewer`, `require_tenant_owner`, `require_superadmin`) are byte-for-byte unchanged.

### 38.1 Header and Per-Page Filtering

- The header's alliance switcher is removed entirely from `admin.html` — there is no more one global "current tenant" concept governing every tab, and `common.js`'s `api(method, path, body, skipTenantHeader, tenantOverride)` no longer falls back to an implicit current tenant: every write call passes an explicit `tenantOverride`.
- Each combined-capable tab (Dashboard, Events, Announcements, Schedule, Gantt — sharing Schedule's own filter, Post Log, Sync, Discord Config) has its own "Alliance: [dropdown]" filter, defaulting to **All** (`X-Tenant-Slug: *`, the `COMBINED_SLUG` constant). `common.js`'s `getTabFilter(tab)`/`setTabFilter(tab, slug)` persist each tab's choice independently under its own `localStorage` key (`samaya_filter_<tab>`), and `renderAllianceFilterSelect(selectId, tab, onChange)` renders and wires the `<select>` identically across every tab.
- Any modal that creates or edits a single-tenant-owned row (a new Event, a new Announcement) has an explicit **Owning Alliance** selector (`renderOwningTenantSelect()`) naming which tenant it belongs to. The Event modal only shows this on creation (an existing event's owning alliance is fixed, shown as plain text); the Announcement modal always shows it, since an announcement's own alliance and its delivery Targets (§13.3/§20) are two independent things.
- Row-level mutations against a combined list (editing/deleting a specific Event, toggling an Occurrence, a Sync "push fix" row) resolve the *owning* tenant of that specific row from whatever list the page already has cached (`EVENTS_CACHE`, `occurrenceData`, or the per-alliance data a combined GET already returned) rather than from the tab's current filter value, since a combined list mixes rows from several alliances at once.

### 38.2 Dashboard

`GET /api/status` and `GET /api/delivery-health` (`routers/admin/status.py`) moved from `get_current_tenant` to `get_current_tenants`, returning `{"alliances": [...]}` — one status/health row per accessible (or filtered-to) alliance. `dashboard.js` renders one card per alliance in `#st-alliances`, plus the existing Today's Events/Upcoming (48h) sections (each row tagged with its alliance name when the filter is "All") and the Delivery Health card, now one block per alliance. "Regenerate Now" fires once per alliance when the filter is "All", reporting how many succeeded.

### 38.3 Events, Schedule, Gantt, and Announcements

- **Events**: already combined-capable server-side; gained the `eventsFilter` dropdown and the Owning Alliance selector on "+ Add Event" (§38.1).
- **Schedule**: `GET /api/occurrences` is combined-capable; gained a `scheduleFilter` dropdown, and every per-occurrence write (`togglePostFlag`, `postOne`, `cancelOccurrence`, `postSelected`) resolves that occurrence's own owning alliance from the already-fetched `occurrenceData` rather than the tab filter.
- **Gantt**: shares Schedule's own filter (`getTabFilter('schedule')`) rather than keeping a second independent one, since it's a second layout over the same occurrence window.
- **Announcements**: `GET /api/announcements` (`routers/admin/announcements.py`) moved to `get_current_tenants`; gained the `announcementsFilter` dropdown and an always-shown Owning Alliance selector (`aOwningTenant`) in the announcement modal, distinct from the Targets list.

### 38.4 Post Log and Sync

- **Post Log**: already combined-capable; gained the `postlogFilter` dropdown. CSV export sends the current filter value directly (the export endpoint already accepts `*`).
- **Sync**: `GET /api/sync/discord` (`routers/admin/discord_sync.py`) moved to `get_current_tenants`, returning `{"alliances": [...]}` — one full drift report (matched/mismatched/discord_only/postlog_only, plus its own `tenant_slug`/`tenant_name`/`tenant_color`) per accessible alliance. The page renders one set of sections per alliance (tagged with its name whenever more than one alliance is shown), and every per-row fix action (Push to Discord, Mark Cancelled, Acknowledge) carries that row's own alliance slug as an explicit `tenantOverride`, since these are still single-tenant `require_not_viewer` endpoints under the hood.

### 38.5 Discord Configuration — Accordion by Server

`GET /api/discord/config-overview` (new, `routers/admin/discord_config.py`) groups by `DiscordServer.id` rather than by Tenant (`Tenant.server_id` has no unique constraint — more than one alliance can already share one server) and returns `{"servers": [...]}`, each entry carrying only the tenant names *the requesting caller* can see for that server — never a server's full backing list — so a coordinator with access to one of two tenants sharing a server never learns the other tenant's slug through this endpoint. `config.js` renders one `<details>` accordion section per server (guild name, channels, roles), replacing the old flat single-tenant channel/role tables.

### 38.6 Merged Access & Platform Page, and the Two-Tier Role Model

Access and Platform are one tab/view now ("Access & Platform"), both superadmin-only. Per the user's explicit instruction ("we have super users and view-only users... make the most prudent choices"), the UI's visibility model collapsed from three tiers (owner/coordinator/viewer) to two — `common.js`'s `isSuperadmin()` gates both halves, replacing the old owner-only gate on Access:

- **Access** (superadmin-only now, not owner-only): invite/member management, unchanged in behavior, with its own `accessAllianceSelect` alliance picker (invites/members are still inherently per-alliance — there's no combined form of "who has access").
- **Platform** (`#platformSection`, nested inside the Access view rather than its own tab): Kingdom/Discord Server/Tenant/User CRUD, unchanged in shape except for the Tenant modal (§38's icon feature, below) and Kingdom editing gaining the two branding fields (§38.7).
- **Audit Log** is also superadmin-only now (previously owner-only), and reads through whichever alliance is currently selected on the Access tab (`auditTenantSlug()` delegates to `accessTenantSlug()`) rather than keeping a second independent selector.

**This is a UI simplification only.** `UserTenant.role` still has three values server-side, and every write-path dependency (`require_not_viewer`, `require_tenant_owner`) still enforces them exactly as before — a coordinator or viewer who isn't a superadmin simply doesn't see the Access & Platform or Audit Log tabs in the UI anymore, the same way they never saw Platform before this change.

### 38.7 Editable Titles, and Tenant Icons

Two new nullable `Kingdom` columns (one Kingdom deployment today, so deliberately kingdom-wide, not per-tenant): `public_site_title` (default, when unset, `"Kingshot Event Schedule"`) and `admin_console_title` (default `"Samaya"`). Both are edited via `editKingdom()`'s prompt chain on the Platform section (§38.6) and served by a new public, unauthenticated `GET /api/kingdom-branding` endpoint (same trust level as the already-public `GET /api/alliances`) — `init.js` applies `admin_console_title` to `admin.html`'s masthead and tab title, and `events.html` applies `public_site_title` to its own masthead, both as a plain overlay on top of the existing hardcoded defaults so an unconfigured deployment behaves exactly as before.

**Tenant icons (added mid-build per explicit user instruction):** `Tenant.icon_image_data`, a nullable `Text` column reusing the exact `parse_cover_image_data` validator already built for `EventDefinition.cover_image_data` (§35) — a full `data:image/(png|jpeg|jpg|gif);base64,...` data URI, same null-vs-empty-string PATCH semantics (omitted/null leaves it unchanged, `""` clears it). Uploaded via a proper file input in a new Tenant create/edit modal (`platform.js`'s `openTenantModal()`/`saveTenantModal()`, replacing the old `prompt()`-chain editor, since a file picker needs a real form) and rendered in place of the generic color-dot/shield-emoji wherever an alliance is named: the admin Tenants table, and on the public pages — the masthead icon, the alliance switcher, and the per-row alliance badge in combined view (`events.html`'s `TENANT_ICONS`/`.samaya-tenant-icon`). `GET /api/alliances` and every admin tenant-listing endpoint include `icon_image_data`.

### 38.8 Per-Page "About This Page" Footer

Every admin tab (Dashboard, Events, Schedule, Gantt, Post Log, Sync, Discord Config, Access & Platform's two sections, Audit Log, Announcements) has a collapsed-by-default `<details class="about-page">` block at the bottom of its view (styled in `admin.css`), written in plain language for the coordinators actually using the panel: what each section/button does, and known limitations (e.g. Sync is a manual, on-demand check; Post Log only records what Samaya itself posted; Discord Config's accordion is read-only stored data, not a live re-fetch on every load).

### 38.9 Last-Activity Marquee on Public Pages

A thin strip on `events.html` (`#lastActivityMarquee`), under the masthead's info card, above the event list: **"📅 Last event posted: *{name}* · {time}"** or **"📢 Last message: *{name}* · {time}"**, sourced from `_last_activity_for_tenants()` (`routers/events.py`) — whichever is more recent between the scope's latest `posted` `PostLog` row and its latest `posted` `AnnouncementTarget` (checked at the **individual target's own `post_status`**, deliberately never the parent `Announcement.status` aggregate, since a multi-target announcement can succeed for one alliance and fail for another — the marquee must never claim a message landed somewhere it didn't; covered by a dedicated failed-target-exclusion test). `GET /t/{slug}/api/last-activity` scopes to one alliance; `GET /api/last-activity` covers every alliance and includes the winning alliance's name. Renders nothing (not an empty bar) when there's no delivery history yet.

### 38.10 Migration

`migrate_add_tenant_icon_and_branding.py` (idempotent, same hand-written convention as every prior migration — §22) adds `tenants.icon_image_data`, `kingdoms.public_site_title`, and `kingdoms.admin_console_title`. Run once against production before deploying this section's app code.

### 38.11 Out of Scope

- Per-tenant page-title overrides beyond the existing `Tenant.name` interpolation and the new per-tenant icon — the two title fields are deliberately kingdom-wide, matching this deployment's actual shape (one Kingdom).
- Any change to the `UserTenant`/`is_superadmin` access-control model itself, or to what any of the three roles can actually do server-side — §38.6's two-tier model is a UI visibility simplification only.
- Granular (more than two-tier) UI role distinctions — explicitly deferred per the user's own stated assumption that this deployment doesn't have enough distinct admins to warrant it yet.
- A live re-fetch of Discord's actual current channel/role list on every Discord Config page load — the accordion (§38.5) shows this app's own stored config, exactly as the old single-tenant page already did.

## 39. Public Status/Kind Legend

**Status:** Implemented.

**Problem.** §37 gave events and announcements consistent kind badges (📅/📢) and a standardized six-value status vocabulary (§"Standardize the list of statuses" work), but a first-time visitor to the public events page had no way to learn what "Scheduled" vs. "Announced," or the gray/green/blue/red colors, actually mean without asking someone.

**Implementation.** A legend card (`#statusLegend`, `renderLegend()` in `events.html`) is the first thing in `<main>` — above the "Subscribe to stay up to date" card, the last-activity marquee, and the event list itself, satisfying "at least above-the-fold, if not first on the page." It renders three rows explaining the 📅 Event / 📢 Announcement / 🌐 Kingdom-wide badges, followed by one row per entry in `STATUS_META` (now carrying a `desc` string alongside its `text`/`color`), each shown as the real `pfLabel()` pill next to a one-line plain-English explanation. Deliberately generated from `STATUS_META`/`eventKindBadge()`/`announcementKindBadge()`/`scopeBadge()` — the same functions the event rows below it call — rather than a second hand-written copy of the same six statuses, so the legend cannot drift out of sync with what the page actually shows.

### 39.1 Out of Scope

- A dismiss/collapse control for returning visitors — the legend is short (nine rows, wraps to a few lines on mobile) and is not judged intrusive enough to warrant persisted collapsed state.
- Per-alliance customization of the legend's wording — the status vocabulary is deliberately fixed and shared across every alliance (§"Standardize" work); a legend that could say different things for different alliances would undermine that.

## 40. Feedback Form (Proposed)

**Status:** Spec only — not yet implemented. Written at the same time as §41–43, which share its underlying storage and public listing page (§43); implement together.

**Problem.** There is currently no channel for a community member browsing the public events page to tell the alliance/kingdom leadership "this is confusing," "it would help if X existed," or any other general feedback about Samaya itself — as opposed to a specific event or announcement being wrong (§42) or a new one being wanted (§41).

**Design.** A general-purpose feedback form, reachable from the public tickets page (§43) via a "+ Submit Feedback" button — not tied to any specific event/announcement row. Fields:

- **Category** (select): `Bug` / `Suggestion` / `Other` — stored as `Ticket.kind = 'feedback'` with `error_type` (reused column name, see §42) holding the category for a feedback-kind ticket.
- **Title** (short text, required, ~120 char limit) — becomes the ticket's headline on the public board.
- **Description** (textarea, required, same ~2000-char ceiling `Announcement.body_markdown` already uses, for consistency) — the actual feedback.
- **Contact** (optional free text — Discord handle or email) — so leadership can follow up; never shown on the public board, admin-only (see §43.3's admin surface).

Submission posts to `POST /api/tickets` (§43.1) with `kind='feedback'`, no `tenant_id` (general feedback isn't alliance-scoped), and no `related_occurrence_id`/`related_announcement_id`. On success, the visitor is shown their new ticket on the public board (§43) so they can immediately upvote-confirm it or watch it move through triage — the same "you get to see where it landed" pattern §42's error report gives.

### 40.1 Out of Scope

- Any reply/notification mechanism back to the submitter (e.g. emailing them when status changes) — `Ticket.submitter_contact` is stored for a human to read and manually reach out if they choose, not wired to `services/notifications.py`.
- Authenticated/attributed feedback (tying a ticket to a `User` row) — the public page has no login, and requiring one would defeat the point of a low-friction feedback channel.

## 41. Event/Announcement Request Form (Proposed)

**Status:** Spec only — not yet implemented. Shares storage/listing with §40/§43; implement together.

**Problem.** A community member (or an alliance leader without admin access) currently has no structured way to propose a new recurring event or a one-off announcement — it happens ad hoc, over Discord DMs or in a general chat channel, with no record and no way for other members to signal "yes, we want this too."

**Design.** A request form, reachable from the public tickets page (§43) via a "+ Request an Event or Announcement" button. Fields:

- **Request type** (select): `Event` / `Announcement` — stored as `Ticket.kind = 'event_request'` or `'announcement_request'`.
- **Alliance** (select, populated from the existing public `GET /api/alliances`, plus a `Kingdom-wide / not sure` option) — stored as `Ticket.tenant_id` when a specific alliance is picked, `null` for kingdom-wide-or-unsure. Purely informational at this stage; it does **not** grant the requester any access or pre-fill an actual `EventDefinition`'s `scope`.
- **Proposed name** (short text, required) — what the event/announcement should be called.
- **Proposed timing** (free text, optional — deliberately not a real date/time picker bound to `EventDefinition`'s recurrence model, since a request is a suggestion, not a finished spec an admin can just save) — e.g. "every Saturday evening" or "one-time, sometime next week."
- **Details** (textarea, required) — what it is, why it matters, anything an admin needs to actually build it.
- **Contact** (optional, same as §40).

Submission posts to `POST /api/tickets` with the corresponding `kind` and the fields above folded into `description` (the timing/name/details are concatenated into one stored description with light labeling, rather than three separate DB columns, since nothing downstream needs to query them independently — an admin reads the ticket and manually creates the real `EventDefinition`/`Announcement` through the existing admin UI if they approve it).

### 41.1 Out of Scope

- Any automatic creation of an `EventDefinition` or `Announcement` from an approved request — this form produces a ticket for a human admin to read and act on manually through the existing admin console (§"Events"/"Announcements" tabs), exactly like every other admin-created row. Auto-materializing unreviewed community input into a real recurring Discord-posting event is a moderation risk this spec deliberately avoids.
- Per-alliance approval routing (e.g. only that alliance's owner/coordinator sees requests tagged to their alliance) — every ticket is visible to every admin on the shared Access & Platform-adjacent triage view (§43.3), same as Audit Log's kingdom-wide visibility model (§31.1).

## 42. Error-Flag Button on Events and Announcements (Proposed)

**Status:** Spec only — not yet implemented. Shares storage/listing with §40/§41/§43; implement together.

**Problem.** Event details are admin-typed (times, channels, descriptions) and Discord posting has its own failure modes (§30's retry-failed-targets exists precisely because delivery can fail) — a member who spots something wrong ("this says Tuesday but it's actually Wednesday," "wrong channel," "this posted twice") currently has no way to flag it from the page where they noticed it.

**Design.** Each event and announcement row on the public events page (`events.html`'s per-row card, §37) gets a small "⚑ Report an issue" affordance — an icon-button in the row's meta line, next to the existing 🔔 notify badge, not a full-width button, so it doesn't compete visually with the row's actual content. Clicking it opens a modal (same X-close/Escape convention as every other modal on this page, §"all modals closeable") with:

- **Error type** (select), the values chosen to cover what's actually observable from a public, unauthenticated page: `Wrong date or time` / `Wrong channel` / `Duplicate posting` / `Didn't happen as scheduled` / `Other`. Stored in `Ticket.error_type`.
- **Description** (textarea, required) — free-text explanation, same length ceiling as §40.
- **Contact** (optional, same as §40).

The modal is pre-scoped to the row it was opened from — no event/announcement picker — and submission posts to `POST /api/tickets` with `kind='error'`, `tenant_id` set to that row's owning alliance, and `related_occurrence_id` or `related_announcement_id` set to that row's id (the two are mutually exclusive, matching the existing `kind: "event"`/`"kind": "announcement"` split §37 already introduced in the combined public feed). The ticket's public title (§43) is auto-derived as `"Issue: {event_or_announcement_name}"` rather than asked of the reporter, since the row itself already establishes what the report is about.

### 42.1 Out of Scope

- Any automatic action on the underlying `Occurrence`/`Announcement` (e.g. auto-cancelling on N reports) — an error flag only ever produces a ticket for an admin to read; every existing admin action (edit, cancel, retry) remains manual and unchanged.
- Rate-limiting or deduplicating repeated reports of the same underlying issue beyond what §43.2's per-voter upvote mechanism already provides — a second visitor hitting the same error is expected to upvote the existing ticket (the modal could eventually deep-link to "an issue like this may already be reported," but that lookup/matching UI is left for a future iteration, not this spec).

## 43. Public Tickets Page with Upvoting (Proposed)

**Status:** Spec only — not yet implemented. This is the shared backend and public listing page for §40–42's three submission forms; none of those forms are useful without it, so all four sections ship together.

**Problem.** §40–42 each produce a piece of community input, but without a shared, visible destination there is no way for the community to see that their feedback landed anywhere, or to signal which of several open issues/requests matter most — every report currently either goes nowhere or has to be manually triaged over Discord with no visible record.

### 43.1 Data Model

A new `Ticket` table (`app/models/db.py`, its own section following the existing "grouped by which phase built it" convention):

| Column | Type | Notes |
|---|---|---|
| `id` | PK | |
| `kind` | `CheckConstraint`: `feedback` / `event_request` / `announcement_request` / `error` | |
| `error_type` | nullable text | error-report subtype (§42) or feedback category (§40); unused for request kinds |
| `title` | text | shown on the public board; auto-derived for `error` (§42), submitter-typed for the other three |
| `description` | text, same ~2000-char ceiling as `Announcement.body_markdown` | |
| `tenant_id` | nullable FK → `tenants.id` | null = kingdom-wide/general |
| `related_occurrence_id` | nullable FK → `occurrences.id` | `error` kind only, mutually exclusive with the next column |
| `related_announcement_id` | nullable FK → `announcements.id` | `error` kind only |
| `submitter_contact` | nullable text | admin-visible only, never rendered on the public board |
| `status` | `CheckConstraint`: `open` / `planned` / `in_progress` / `done` / `declined` | admin-managed triage state, default `open` |
| `upvote_count` | integer, default `1` | denormalized cache of `TicketVote` rows, incremented/decremented alongside vote writes rather than `COUNT()`-ed on every page load |
| `created_at` / `updated_at` | `DateTime(timezone=True)` | `ensure_utc()` conventions apply, as everywhere else in this codebase |

A second table, `TicketVote` (`ticket_id` FK, `voter_key` text, `UniqueConstraint(ticket_id, voter_key)`), backs the upvote mechanism below. No `User` FK anywhere in either table — this is an unauthenticated, public-facing feature by design, same trust level as the events page itself.

### 43.2 Anonymous Upvoting

The public page has no login, so "one vote per person" is approximated rather than guaranteed, the same tradeoff every anonymous-upvote system on the open web makes:

- On first visit to `/feedback`, the client generates a random UUID and stores it in `localStorage` (`samaya_voter_id`) — sent as an `X-Voter-Id` header on every vote request. This is a per-browser token, not an identity.
- `POST /api/tickets/{id}/vote` upserts a `TicketVote(ticket_id, voter_key=hash(X-Voter-Id + server-side pepper))` row (hashed so the raw client-supplied UUID is never stored verbatim) and increments `upvote_count`; calling it again for the same ticket/voter pair deletes the row instead and decrements (a toggle, not an accumulator) — the vote button on `/feedback` reflects this as filled/unfilled per ticket, from a `GET /api/tickets` response that includes each ticket's `voted_by_me` boolean when the request carries a recognized `X-Voter-Id`.
- Clearing browser storage or using another browser resets a visitor's vote weight to zero on their next vote — an accepted, disclosed limitation (mentioned in `/feedback`'s own `<details class="about-page">`-style footnote, matching §38.8's admin convention), not a bug to engineer around with IP tracking or fingerprinting.

### 43.3 Public Board and Admin Triage

- **Public**: a new standalone page, `static/feedback.html`, served by a new public route at `/feedback` (mirroring `events.html`/`ics.py`'s existing pattern of a public, unauthenticated, no-shared-JS page — §22's architectural precedent), listing every non-`declined` ticket sorted by `upvote_count` descending, each row showing its kind badge (reusing §37/§39's badge pattern — a fourth/fifth/sixth color for feedback/request/error kinds), title, description, status badge, relative "reported N ago" time, and the upvote button/count. `error`-kind tickets additionally link back to the event/announcement they reference (by name, not by admin-only id). `submitter_contact` is never included in this endpoint's public response shape.
- **Admin**: a new `routers/admin/tickets.py` (`GET /api/tickets` full-detail including `submitter_contact`, `PATCH /api/tickets/{id}` for `status` only) behind `require_not_viewer` (a viewer can see the same triage list everyone with tenant access can, per the existing viewer-role convention of §31.3, but cannot change `status`) and a new "Tickets" admin tab, listing every ticket kingdom-wide (no per-alliance filter — a small enough volume, and cross-alliance visibility is the point, same reasoning as Audit Log's §31.1 kingdom-wide scope) with a status dropdown per row. Changing status does not trigger any Discord post, email, or other notification (§40.1) — it only updates what the public board shows.

### 43.4 Migration

A new hand-written, idempotent migration script (`migrate_add_tickets.py`, same no-Alembic convention as every prior schema change) creates `tickets` and `ticket_votes`.

### 43.5 Out of Scope

- Comments/discussion threads on a ticket — upvoting is the only interaction the public board offers; richer discussion is left to the alliance's existing Discord channels.
- Merging duplicate tickets, or any admin tool beyond the status dropdown — an admin who spots duplicates handles it by setting the weaker one to `declined` with (informally, over Discord) a pointer to the surviving ticket; no in-app merge/redirect mechanism.
- Search or filtering on the public board beyond the fixed upvote-count sort — with the volume this deployment expects, a flat sorted list is legible without it; can be revisited if the ticket count grows enough to warrant it.
- Any change to `services/notifications.py` (email) or Discord posting triggered by ticket activity — every notification path in this section is "a human reads the admin Tickets tab," not an automated alert.

## 44. Roadmap / Deferred Ideas

Small, well-scoped ideas that have come up but are deliberately not built yet — kept in one place rather than scattered across chat history, so they aren't reinvented or lost. Not commitments or a priority order; each becomes its own real section (with a real `##` number) once it's actually picked up.

- **Recent past events on the public events page.** A short "Recently Happened" list on `events.html`, covering roughly the last 48–72 hours, above or below the main upcoming list. Scope deliberately narrow: only an `Occurrence` whose `post_status` is `posted` or `completed` (i.e. it actually made it onto the calendar and, where applicable, actually went out to Discord) and an announcement whose delivery to that alliance actually reached `posted` on at least one `AnnouncementTarget` — never a `cancelled`/`failed`/merely-`scheduled` row, since the point is "what genuinely happened," not "what was supposed to happen." Needs a new `WINDOW_HOURS_PAST`-bounded query alongside the existing `WINDOW_DAYS`-forward one in `routers/events.py` (both the per-alliance and combined `GET /api/events` variants), most naturally returned as a second array (`past`) alongside the existing list rather than merged into one chronological feed, so the client can render it as its own, clearly-separated section rather than mixing "still coming" and "already done" in one list.

## 45. Sync Tab False Positives: Naturally-Completed Events and a Striped-Row Rendering Bug

**Status:** Implemented.

**Problem 1 — every event that finishes normally was reported as a possible Discord deletion.** The Sync tab's "PostLog Only" bucket exists to catch a Discord Scheduled Event deleted directly in Discord rather than through Samaya, but it was firing for the overwhelming majority-case instead: an event that simply ran to completion. Two compounding causes, both in place since early in this app's life and only now surfacing as user-visible noise:

- `routers/webhooks.py`'s `GUILD_SCHEDULED_EVENT_UPDATE`/`GUILD_SCHEDULED_EVENT_DELETE` handlers, meant to flip `PostLog.status` to `completed`/`cancelled` the moment Discord reports either happening, can never actually run. Discord's "Interactions Endpoint URL" (what this route's signature verification and PING handling exist for) only ever delivers Interaction objects — PING, application commands, message components, modal submits. `GUILD_SCHEDULED_EVENT_UPDATE`/`_DELETE` are Gateway dispatch events, sent only over a bot's persistent Gateway (WebSocket) connection, never as an HTTP POST to any webhook URL — and this app, a stateless FastAPI service with no connected Gateway client, was never going to receive them here. The two handlers are kept in place (commented as known-dead code) rather than deleted, since the route's actual, working responsibility — PING/interaction verification — lives in the same function.
- Consequently `PostLog.status` for a finished event sits on `posted` forever. Discord's own "list scheduled events" endpoint stops surfacing a `COMPLETED` event from that listing fairly quickly — so `_sync_discord_for_tenant` (`routers/admin/discord_sync.py`) would find the PostLog row's `discord_event_id` missing from Discord's response and report it under "PostLog Only," worded as "may have been deleted directly in Discord," with a red issue count and a "Mark Cancelled" action — actively misleading for an event that succeeded.

**Fix.** `_sync_discord_for_tenant` now computes, from Samaya's own schedule data (`EventDefinition.start_time_utc` + `duration_hours` + the PostLog row's `occurrence_date`), whether the occurrence was already due to have ended by the time of the check. A PostLog row missing from Discord's list *and* already past its own end time is bucketed into a new `naturally_completed` list instead of `postlog_only`, explicitly excluded from `summary.issues` (the count behind the Sync tab's red badge), and rendered on the Sync page as a calm, green "✅ Naturally Completed" section ("Nothing to fix; mark them completed to clear them from this report") rather than the red "🔴 PostLog Only" one. A row missing from Discord but *not* yet due to have ended keeps the original "possible deletion" treatment unchanged — that combination is still genuinely suspicious. A new fix action, `POST /api/sync/mark-completed/{post_log_id}` (mirroring the existing `mark-cancelled` endpoint), lets a coordinator explicitly close a row out to `PostLog.status = "completed"`, at which point it drops out of every future sync's `WHERE status == "posted"` scan entirely. `_last_activity_for_tenants` (`routers/events.py`, spec §38.9) was widened from `PostLog.status == "posted"` to `status IN ("posted", "completed")` so marking a row completed doesn't make it disappear from the public page's "last activity" marquee.

**Problem 2 — even-numbered table rows had an unpainted gap under the Actions column.** `admin.css`'s zebra striping (`tr:nth-child(even) td { background: var(--bg2) }`) paints each `<td>` individually, but two Actions cells (Templates and Announcements, both in `announcements.js`) were built with `style="display:flex;gap:6px;flex-wrap:wrap"` set directly on the `<td>` itself. A `<td>` isn't meant to carry `display:flex` — doing so puts it in a different formatting context than its sibling cells, and when a row's other cells wrapped onto a second line (e.g. an announcement with several Targets), that flex-formatted cell no longer reliably filled the row's full height the way a normal table cell does, leaving an unpainted white strip under Actions in even rows.

**Fix.** Both cells now keep a plain `<td class="pf-v6-c-table__td">` (default `display: table-cell`, so it stretches to match the row like every other cell) with the flex layout moved onto an inner `<div style="display:flex;gap:6px;flex-wrap:wrap">` wrapping the buttons instead. No other table in the admin console uses this pattern (checked directly — `announcements.js`'s Templates and Announcements tables were the only two occurrences of `<td ... style="display:flex...">` in the codebase).

### 45.1 Out of Scope

- A real Discord Gateway connection (so `GUILD_SCHEDULED_EVENT_UPDATE`/`_DELETE` could genuinely be received) — a persistent WebSocket client is a materially different deployment shape (a long-running process rather than a stateless request-served FastAPI app) and out of scope for what fixing this particular symptom needs; the schedule-derived heuristic above is sufficient for reconciliation purposes.
- Automatically calling `POST /api/sync/mark-completed/{id}` for every naturally-completed row on a schedule — left as a one-click coordinator action for now, consistent with every other Sync fix action being manually triggered rather than automatic.

**Addendum (spec §49 batch).** The Sync page's user-facing label was shortened from "Naturally Completed" to plain "Completed" (the stat card, section heading, and the "PostLog Only" section's cross-reference sentence, all in `sync.js`) — "naturally" read as unnecessary hedging to coordinators reading the report. The internal `naturally_completed` field/list name, the `mark-completed` endpoint, and every code comment are unchanged; this is a display-text-only change.

## 46. Month Calendar View and Discord Preview

**Status:** Implemented.

**Problem.** Two related gaps on the public events page, plus one shared across the admin console: (1) the public page only ever showed a flat chronological list — there was no month-at-a-glance way to see how a week's events land relative to each other; (2) neither the public page nor the admin console gave any way to see what an event or announcement would actually look like once it reaches Discord short of posting it for real, so "does this description read right," "is the cover image going to look OK," or "did I get the placeholders right" could only be answered by publishing and then checking Discord directly.

### 46.1 List/Calendar Toggle (public events page)

`events.html` gained a "📋 List / 📆 Calendar" toggle (`setEventsView()`/`getEventsView()`, persisted in `localStorage` as `samaya_events_view`) sitting above the event list. **List remains the default view** for both new visitors and anyone who hasn't picked Calendar before.

The calendar renders a standard month grid (Monday-first), built entirely client-side from the same `EVENTS_DATA` the list view already fetched — no new endpoint. Each event/announcement is bucketed onto a calendar day using `tzDateParts()`, which reads the day/month/year **in the visitor's selected display time zone** (§15.4's existing `samaya_display_tz`), not UTC — an event at 11 PM UTC can be "tomorrow" for a visitor several hours ahead, and the calendar places it on the day a viewer would actually expect to find it, the same reasoning `fmtDateTime()` already applies to the admin side's local-date handling. Switching time zones re-renders whichever view (list or calendar) is currently showing.

A day cell shows up to three compact colored items (orange for events, purple for announcements, matching the existing kind-badge colors) plus a "+N more" when there are more; clicking an item opens the Discord preview (§46.2) directly, while clicking anywhere else on the day cell opens a day-detail panel below the grid listing that day's full cards. Prev/Today/Next navigate by month; since the underlying data is still the existing 28-day-forward window (`WINDOW_DAYS`, `routers/events.py`), a month outside that window simply renders empty rather than fetching anything new — full historical/future month browsing would need a materially different (paginated or unbounded) backend query, which is out of scope here.

### 46.2 "Preview as it would look on Discord"

Every event/announcement card on the public page, and every row on the admin Dashboard's occurrence cards, Schedule table, Events table, and Announcements table, is now clickable and opens a "Discord Preview" modal (X-close/Escape, same convention as every other modal in this app) rendering an approximation of how that item would actually appear on Discord:

- **Announcements** render as a plain Discord message bubble (avatar, "Samaya BOT" header, timestamp, body).
- **Events** render as a Scheduled-Event-shaped card instead (title, 🗓️ start time, duration/channel meta, description, a disabled "✓ Interested" button, and the cover image banner when `cover_image_data` is set) — a plain chat bubble would misrepresent what an Event actually looks like on Discord.

Both reuse the exact placeholder-resolution and Discord-markdown-to-HTML rendering the Announcement/Template/Event-description composer preview already built (spec §27/§28/§32.2 — `clientRenderPlaceholders`/`renderDiscordMarkdownPreview`), rather than a third implementation: on the admin side directly (`common.js`'s new `openDiscordPreview(kind, item)`, called from `dashboard.js`/`schedule.js`/`events.js`/`announcements.js`'s row click handlers), and as a deliberately-simplified duplicate on the public page (`events.html`, no shared JS with admin per §22) that renders role/channel mentions as generic "@role mention"/"#channel mention" labels rather than resolved names, since a public unauthenticated page has no access to a guild's actual role/channel list the way the logged-in admin composer does.

An `EventDefinition` row in the admin Events tab (as opposed to a concrete `Occurrence`) has no single "next occurrence" to preview against, so it previews using its own `anchor_date`/`start_time_utc` — the same convention the Events modal's own live description preview (§32.2) already uses. `_occurrence_dict` (`routers/admin/serializers.py`) gained two previously-omitted fields, `description` and `cover_image_data`, so the Dashboard/Schedule preview has the same content Discord itself actually received.

Every row-level click handler (`handleRowPreviewClick`, `common.js`) ignores clicks on an actual control inside the row (a button, a checkbox, a `<select>`) so Post/Cancel/Edit/Duplicate/the post-to-Discord toggle keep working exactly as before — only a click on the row's otherwise-inert surface opens the preview.

### 46.3 Out of Scope

- Browsing calendar months outside the existing 28-day-forward data window — see §46.1; would need a different backend query shape than exists today.
- A live re-fetch of Discord's actual current role/channel names for the public page's preview — the public page has no authenticated path to that data at all (see §46.2); this is an inherent limitation of previewing from an unauthenticated context, not a bug.
- Any interactivity in the preview itself (actually clicking "Interested," reacting, replying) — it's a static visual approximation, not an embedded Discord widget.

## 47. Calendar View Legibility and Accessibility Pass

**Status:** Implemented.

**Problem.** Feedback on §46's calendar view, from real usage: calendar day-items were too small and low-contrast to read at a glance; the status/kind legend's flex-wrap layout left badges and descriptions raggedly aligned instead of scanning as tidy rows; the month/year calendar title was left-aligned and the same size as any other card heading, easy to miss; the Prev/Next month buttons were bare `◀`/`▶` glyphs giving no hint which month they'd land on; every modal's `×` close button and the plain-link-styled "Close" buttons on the Time Zone and Discord Preview modals were small, low-affordance targets; and the calendar's day cells and colored items had no keyboard path at all — a mouse was required to use the calendar.

**Fix, all in `events.html` unless noted:**

- **Legend (`renderLegend`)** — `.samaya-legend-kinds`/`.samaya-legend-statuses` switched from `display:flex;flex-wrap:wrap` to a fixed 3-column CSS grid (`repeat(3, minmax(0,1fr))`), collapsing to one column under 700px. Every badge+description row now lines up into clean columns instead of wrapping wherever space ran out.
- **Calendar item legibility (`.cal-item`)** — font-size raised (0.72rem → 0.78rem, 0.62rem → 0.68rem on mobile), weight bumped to 600, and the event color darkened from `#c9590c` to `#a34a0a` — the original orange's white-text contrast ratio (~4.4:1) fell just under WCAG AA's 4.5:1 small-text threshold; the announcement purple (`#7d5260`, ~6.5:1) already cleared it and is unchanged. `.cal-cell` grew slightly (84px → 96px min-height, 56px → 64px on mobile) to fit the larger text.
- **Calendar header** — restructured into a `.cal-header-row` 3-column grid (nav | title | balancing spacer) so `#calMonthLabel` (`.cal-month-label`) is centered against the whole card rather than the leftover space beside the nav buttons, and enlarged to the page's heading-lg token (heading-md on mobile). `calendarPrevMonth`/`calendarNextMonth`'s shared `renderCalendar()` now also sets the nav buttons' text to the adjacent month's abbreviated name (`◀ Aug` / `Oct ▶`) and a full `aria-label` (`"Previous month: August 2026"`) instead of the bare arrows carrying no month information.
- **Modal close affordance** — `.samaya-modal-close` (`events.html` and, identically, `admin.css`) grew to a 44×44px minimum touch target (WCAG 2.5.5) with a larger glyph (1.3rem → 1.75rem) and an explicit `:focus-visible` outline. The Time Zone and Discord Preview modals' footer "Close" buttons changed from `pf-m-link` (styled as a bare text link) to `pf-m-primary` (solid blue, white text) in both `events.html` and `admin.html`, matching how every other confirming modal action in this app is styled; their "Cancel" siblings on other modals are unchanged, since cancelling is a different action from a simple close.
- **Keyboard accessibility** — calendar day cells and colored items (previously plain `<div onclick>` with no keyboard path at all) gained `role="button"`, `tabindex="0"`, a descriptive `aria-label` (the day cell's names the date and item count; each item's names the event/announcement title), and an `onkeydown` handler treating Enter/Space as a click, matching the existing `role="button"` convention `buildEventCardHtml`'s list-view cards already used — those cards additionally gained the same Enter/Space `onkeydown` handler and an `aria-label`, since `tabindex`+`role="button"` alone doesn't give a `<div>` native button key handling.

No backend changes; `STATIC_ASSET_VERSION` bumped (`routers/admin/ui.py`) since only static assets changed.

## 48. Reserved

(Number skipped in the working session that produced §47/§49 — no content was ever assigned to it.)

## 49. Combined Owning-Alliance/Scope Selector, Event/Announcement Reassignment, and In-Place Announcement Editing

**Status:** Implemented.

**Problem.** Three related gaps, all raised together: (1) creating an Event required picking an "Owning Alliance" from one `<select>` *and* separately ticking a "Kingdom-wide" checkbox — two controls for one underlying choice, and editing an existing event hid the alliance picker entirely (moving an event to a different alliance wasn't possible at all); (2) Announcement had no scope/kingdom-wide concept whatsoever, unlike Event, so the two forms felt inconsistent and there was no way to label an announcement as "this is for the whole Kingdom" the way an event's scope does; (3) an already-scheduled Announcement could only be Cancelled, Deleted, or Duplicated — never edited in place, so fixing a typo in a not-yet-sent announcement meant cancelling it and recreating it from scratch (losing its id/audit trail) or using Duplicate and then cancelling the original.

**Fix:**

- **Combined selector (`renderOwningTenantScopeSelect`, `common.js`)** — one `<select>` whose options are every alliance twice: once as a plain alliance option and once as that alliance's "🌐 Kingdom-wide (posted via `<name>`)" option, grouped into two `<optgroup>`s. The value encodes both pieces (`alliance:<slug>` / `kingdomwide:<slug>`); `parseOwningTenantScopeValue()` splits it back into `{scope, slug}`. This replaces the old plain `renderOwningTenantSelect` + a separate `#mKingdomWide` checkbox for both the Event modal (`events.js`) and the Announcement modal (`announcements.js`) — `renderOwningTenantSelect` itself is unchanged and still used where there's no scope concept at all (Access tab's alliance picker).
- **Events: editable, not just at creation** — the Owning Alliance row in `admin.html`'s Event modal is no longer hidden on edit; `openEventModal()` always renders the combined selector, pre-filled from the event's current owner/scope. `saveEvent()` now always sends `scope`, and on an edit additionally sends `owning_tenant_slug` in the payload — the X-Tenant-Slug header stays the event's *original* owning tenant (so `update_event` can find and authorize the row), while `owning_tenant_slug` carries where it should move *to*.
- **Backend reassignment (`EventPatch.owning_tenant_slug`, `routers/admin/events.py`)** — `update_event` resolves the new slug via `resolve_target_tenants` (the same access check Events'/Announcements' explicit multi-target lists already use), so moving an event requires access to *both* the old and new alliance, not just the old one. Picking kingdom-wide (on the new owner) still requires `check_kingdom_coordinator` for the *new* tenant's Kingdom. `discord_channel`/`notification_channel_id`/`notification_role_id` are real Discord snowflake IDs tied to one guild — reassigning across two tenants on *different* `DiscordServer`s (spec §25) clears them rather than silently carrying over IDs that belong to the wrong guild; reassigning between two tenants sharing the same server leaves them untouched, since they're still valid.
- **Events table Alliance column bug fix (`events.js`)** — the column previously showed the literal word "Alliance" for every non-kingdom-wide row (a leftover `scopeLabel` bug) instead of the owning tenant's name; it now shows the tenant's color dot + name, or "🌐 Kingdom-wide (via `<name>`)". The event-name column no longer appends a redundant `(tenant name)` tag, since the Alliance column now carries that on its own.
- **Announcements gain the same scope concept (`Announcement.scope`, migration `migrate_add_announcement_scope.py`)** — a new `scope` column (`'alliance'`/`'kingdom-wide'`, same CHECK-constraint shape as `EventDefinition.scope`), picked from the same combined selector in the Announcement modal. It's a display/ownership label only: delivery still goes entirely through the existing explicit `AnnouncementTarget` rows (the pre-existing "+ Add all alliances" button, spec §29, is still how a kingdom-wide announcement actually reaches every alliance) — picking kingdom-wide doesn't change *how* or *where* it's sent, only what the announcement is labeled as *for*.
- **Announcements: editable in place (`AnnouncementPatch`, `PATCH /api/announcements/{id}`)** — a new endpoint alongside the existing create/cancel/retry/delete ones, gated to `status == 'scheduled'` only (once posted, the message already went out; once failed/cancelled, the row is terminal — same terminal-state reasoning `delete_announcement` already applies). Supports the same `owning_tenant_slug` reassignment as events (no Discord-field-clearing needed here, since Announcement has no bare channel/role fields of its own — only its explicit targets, each already tied to its own tenant). The admin table gained an **Edit** button (shown only while `status == 'scheduled'`, next to Cancel) that reopens the compose modal pre-filled with the announcement's current title/body/schedule/targets/scope, via the same `openAnnouncementModal(source, isEdit)` the "New"/Duplicate/"Use template" flows already used — `isEdit=true` additionally prefills the existing Send Date/Time (a duplicate still leaves those blank, since a copy always needs a fresh future schedule) and routes `saveAnnouncement()` to `PATCH` instead of `POST`.

### Out of Scope

- Automatically re-populating a kingdom-wide announcement's targets when new alliances join the Kingdom, or removing them when `scope` changes back to `alliance` — targets remain a fully independent, explicitly-managed list either way (see the scope's own "display/ownership label only" note above).
- Changing an Announcement's owning alliance's Discord identity implications — since Announcement has no bare `discord_channel`/notification fields of its own (unlike Event), there's nothing analogous to clear on reassignment.

### Addendum — Standardized Table Columns and Real Notification Target Names

Follow-up feedback on the same batch: the Alliance Events, Leadership Notifications, and Announcements tables (all in the admin Events/Announcements tabs — not the Schedule tab's per-occurrence tables, which are a different concern) had inconsistent, ad hoc column sets, and every "Targets" column only ever showed raw tenant names or bare Discord snowflake IDs, never the actual channel/role/server a coordinator would recognize.

**Fix.**

- All three tables now share one column set: **Name, Alliance, Interval, Schedule Start, Status, Notification Targets, Actions** — dropping the Alliance Events/Leadership tables' previous separate Duration/Anchor columns (still visible via Edit and the row's Discord-preview click) and adding a new Alliance column to the Announcements table (previously missing one entirely).
- **Notification Targets column (`enhanceNotificationTargetLabels`, `common.js`)** — since the channel/role IDs involved are real Discord snowflakes, not names, each cell first renders with the bare ID (all that's known synchronously) inside a `data-notif-channel="tenantSlug:id"`/`data-notif-role="tenantSlug:id"` span, then a shared helper resolves every such span to a real `#name`/`@name` label — one batched `/api/discord/channels`+`/api/discord/roles` fetch per distinct tenant slug appearing in the table (cached), not one per row or per target. For events: the row's own primary channel (already a plain name, no resolution needed), its pre-event ping channel/role, and a summary line per extra `EventTarget`. For announcements: each `AnnouncementTarget`'s owning alliance name, its Discord server name (`Tenant.server_name`, already returned by `/api/tenants`), and its channel.
- **Alliance Events section background tint** — `admin.css`'s existing `--bg2` token now tints the "🛡️ Alliance Events" heading the same way `--bg3` already tinted "👑 Leadership Notifications," so the two sections read as equally-deliberate, distinct bands instead of only one of them looking styled.
- The Announcements page's stale "cannot be edited" copy was corrected to reflect §49's new in-place editing.

Out of scope: resolving channel/role names server-side into the API response itself (kept as a client-side, cached lookup — consistent with how the Announcement composer's own preview already resolves mentions).

## 50. Scheduled Announcements in the Admin Schedule View

**Status:** Implemented.

**Problem.** The 28-Day Schedule tab (`#v-schedule`, both its Table and Timeline layouts) only ever showed `Occurrence` rows. A still-`scheduled` Announcement — something a coordinator is just as likely to be tracking against the same 28-day window — was invisible there; seeing it required switching to the separate Announcements tab, with no shared at-a-glance view of "everything going out in the next few weeks."

**Fix.** Both Schedule layouts gained a third, independent section for scheduled announcements, alongside (not merged into) the existing Alliance Events/Leadership Notifications sections — an Announcement has no start/end time, duration, `post_to_discord` toggle, or per-target Post/Cancel-to-Discord action the way an `Occurrence` does, so it was never going to fit those tables' rows.

- **Table layout (`schedAnnouncementsWrap`/`schedAnnouncementsTable`, `admin.html`; `renderScheduleAnnouncements`, `schedule.js`)** — a "📢 Announcements" table beneath the Leadership Notifications table, sharing §49's addendum column set (Name, Alliance, Interval, Schedule Start, Status, Notification Targets) but with an **Actions** column offering only **Edit** and **Cancel** — no post-to-Discord toggle or Post button (those are Events-only concepts), and no Delete either: this table only ever lists `status == 'scheduled'` announcements, and `delete_announcement` is terminal-state-only (§13.3/§27), so a Delete button here would always 400 — Delete/Retry Failed/Duplicate remain on the Announcements tab itself, where an announcement's full lifecycle (including terminal states) is visible. `editAnnouncement`/`cancelAnnouncement` (`announcements.js`) are reused as-is.
- **Timeline layout (`ganttAnnouncementsWrap`, `admin.html`; `renderAnnouncementGantt`, `gantt.js`)** — a third Gantt-style grid, reusing `buildGanttGrid` by mapping each Announcement into the same `{event_name, occurrence_date, owning_tenant_id, start_datetime_utc}` shape an `Occurrence` already has. Unlike an Event's recurrence — expanded into real `Occurrence` rows up front by `regenerate_occurrences` — a recurring Announcement only ever has one live `scheduled_for` at a time (the next send, advanced in place after each delivery by `scheduler/announcements.py`), so this plots exactly one marker per announcement, never a projected range of future sends the way a recurring event's Gantt row does.
- **Data loading (`loadSchedule`, `schedule.js`)** — fetches `GET /api/announcements` (scoped by the Schedule tab's own alliance filter, same as the existing occurrences fetch) into `announcements.js`'s own `ANNOUNCEMENTS` cache, rather than a Schedule-local one, so `editAnnouncement`/`cancelAnnouncement` work correctly from the Schedule tab even if the Announcements tab was never visited this session; the fetched list is then filtered to `status == 'scheduled'` for display in both new sections.

No backend changes — this is a display-only combination of two already-existing endpoints on one tab; `STATIC_ASSET_VERSION` bumped (`routers/admin/ui.py`) since only static assets changed.

### Out of Scope

- A combined single table/timeline mixing Events and Announcements row-for-row — kept as separate sections given how different their underlying shapes are (range vs. point-in-time, different action sets).
- Showing anything other than `status == 'scheduled'` announcements here — posted/failed/cancelled ones remain a concern for the Announcements tab, not a forward-looking 28-day schedule.

## 51. Daily Auto-Post Job

**Status:** Implemented.

**Problem.** Posting an occurrence to Discord was entirely manual: a coordinator had to check the "Post?" box (or leave the default unchecked, in which case nothing happened at all) and click "Post"/"Post Selected" for every upcoming occurrence, every day, across every alliance. Nothing acted on a coordinator's behalf even for the routine case of "this occurrence is coming up and definitely needs to go out."

**Fix.** A fourth scheduler job, `scheduler/auto_post.py`'s `auto_post_upcoming_occurrences`, runs once daily at **UTC 16:00** — deliberately after regeneration's own UTC 00:00 slot, so a fresh day's occurrences are always in place first. For every `Occurrence` starting within the next **7 days** whose `post_status` isn't already `posted`/`cancelled`:

- **Nothing on Discord yet** — posted normally, through the exact same target-resolution path the manual Post button uses. `post_occurrence`'s inline kingdom-wide-fan-out-plus-explicit-targets logic was factored out into `_resolve_post_targets(db, event, owning)` (`routers/admin/occurrences.py`) so the manual endpoint and this job can't drift on what "every target for this occurrence" means; `_post_to_one_tenant` itself (already a plain, non-FastAPI function) is reused as-is, targets and all — same PostLog-reservation-before-Discord-call race protection, same per-target independent success/failure.
- **A matching Discord event already exists** (same name, start time within 15 minutes of what Samaya would have posted — `MATCH_TOLERANCE_MINUTES`) — this is a Discord Scheduled Event created directly, bypassing Samaya entirely. Rather than create a duplicate, the job records a `PostLog` row against the existing `discord_event_id` with no second Discord API call at all (`posted_by="system (auto-post, synced from Discord)"`).
- **A same-named Discord event exists but its time doesn't match** — flagged, not overwritten: `Occurrence.post_status` is set to `'error'` and `status_detail` explains the conflict, surfacing via the Schedule page's existing `⚠` status-detail indicator (no new UI needed — this reuses the same field/rendering an ordinary posting failure already uses). The coordinator resolves it deliberately through the existing Discord Sync tooling (`routers/admin/discord_sync.py`) rather than the job guessing which side is "right."
- **Skipped, not touched at all** — an occurrence whose `EventDefinition.active` is `False`; one starting in under 15 minutes (the same floor `post_occurrence` already enforces on the manual button, since something this imminent is better left to a coordinator's own judgment call); one outside the 7-day window; one already `posted`/`cancelled`.

Deliberately **not** gated on the `post_to_discord` checkbox — that flag defaults to unchecked, so requiring it would just reintroduce the daily per-occurrence chore this job exists to remove. The checkbox (and the manual Post/Post Selected buttons) remain fully available on the Schedule page for anything a coordinator wants to handle by hand.

### Out of Scope

- Diffing an existing-but-matching Discord event's description/channel/cover-image against what Samaya would have posted — matching is judged on name + start time only; a finer-grained content diff is what the Discord Sync tab's own drift report already does for posted occurrences, and is not duplicated here.
- Any change to the 15-minute-before-start cutoff, the 7-day window, or the 16:00 UTC run time being configurable per-tenant — all three are fixed constants for now (`WINDOW_DAYS`/`MIN_LEAD_TIME_SECONDS`/`MATCH_TOLERANCE_MINUTES` in `scheduler/auto_post.py`, the `CronTrigger(hour=16, minute=0, ...)` in `main.py`).

### Addendum — Corrected "Kingdom-wide" Wording

Follow-up feedback: the combined owning-alliance/scope selector's kingdom-wide options (§49) read "🌐 Kingdom-wide (posted via `<name>`)", and the Events/Announcements/Schedule table badges read "🌐 Kingdom-wide (via `<name>`)". Both wordings imply that alliance's Discord server is what actually posts a kingdom-wide item — it isn't: an Event still fans out to every alliance's own server independently (`_resolve_post_targets`), and an Announcement's `scope` is a display/ownership label only, with delivery entirely governed by its explicit `AnnouncementTarget` list regardless of scope. The named alliance is only ever used to look up which Kingdom to scope to. Reworded everywhere to "🌐 Kingdom-wide (via `<name>`'s Kingdom)", which says what's actually true without implying anything about where the posting happens.
