# Samaya — repo map

FastAPI + PostgreSQL + Discord event scheduler for the Kingshot K138 community,
deployed at `/opt/taraka` on `lxc-taraka`, serving `ks138.taraka.dev`. Multi-tenant:
supports several alliances, each their own `Tenant`, plus events shared across
a whole Kingdom. This file exists so you (Claude) can find the 1-2 files a task
actually needs instead of reading the whole tree. Update it when files are
added, removed, or renamed — not on every logic change inside a file.

## Top level

- `README.md` — live URLs, stack, quick start, common ops commands
- `docker-compose.yml` — app + Postgres 16, Caddy/Cloudflare Tunnel in front (not in this repo)
- `.env.example` — required env vars (`DB_PASSWORD`, `SECRET_KEY`, `DISCORD_OAUTH_CLIENT_ID`/`_SECRET`/`_REDIRECT_URI`, `SUPERADMIN_DISCORD_IDS`, `PLATFORM_BOT_TOKEN`, `PLATFORM_PUBLIC_KEY`, SMTP settings)
- `package.json` / `eslint.config.mjs` — lints `app/static/` JS only; no JS build step, no bundler
- `migrate_to_multitenant.py` (repo root, one level up from `app/`) — one-time hand-written production migration (this repo has never actually used Alembic despite listing it as a dependency); already run once for the Kingdom/Tenant model — check with whoever deployed last before assuming it still needs running
- `migrate_add_announcement_recurring.py` (`app/`) — one-time hand-written migration adding `leadership_only`/`recurring`/`interval_days` to `announcements`; same no-Alembic caveat as above — check whether it's already been run before assuming it still needs running
- `migrate_add_event_targets.py` (`app/`) — one-time hand-written migration creating the `event_targets` table (spec §20); same no-Alembic caveat, same check-before-assuming-it's-needed caveat

## `app/main.py`

FastAPI app entrypoint. Owns: the startup check (`SECRET_KEY` + Discord OAuth
credentials must be set — the app refuses to boot without them, the same
fail-closed posture the old `ADMIN_API_KEY` check used before Phase 4 replaced
it with real sessions), the `lifespan` context (creates tables, runs a catch-up
regeneration per tenant if overdue, starts APScheduler's three jobs), the
validation-error handler, and wiring all routers/static mount together. Touch
this for startup/shutdown behavior or to add a new top-level router — not for
route logic itself.

## `app/routers/` — HTTP layer

- `events.py` — public, unauthenticated, tenant-scoped by URL path (not header — calendar apps subscribing to an ICS feed can't send custom headers): `GET /t/{tenant_slug}/api/events`, `GET /t/{tenant_slug}/events` (renders `static/events.html`), plus the combined `GET /api/events`/`GET /events`, and `GET /api/alliances` (name/slug/color roster of every tenant, powering `events.html`'s alliance switcher — spec §26.3)
- `ics.py` — `GET /t/{tenant_slug}/ics/events.ics`, the public calendar feed, same path-scoping reasoning as `events.py`
- `webhooks.py` — `POST /webhooks/discord`, inbound Discord interaction webhook, signature-verified against `PLATFORM_PUBLIC_KEY` (a tenant with its own custom bot and its own Discord "Interactions Endpoint URL" isn't supported here — see the file's own docstring for the narrow gap this leaves)
- `auth.py` — Discord OAuth login, invite-claim (`GET /invite/{token}`), the OAuth callback, logout. The only way to reach Discord OAuth at all, and the only way OAuth ever grants real access (via consuming a pending `Invite` — see `models/db.py`)
- `auth_pages.py` — the two static error pages `auth.py`'s flow redirects to on failure (`/invite-invalid`, `/auth/login-failed`); split out because they're pure presentation, no OAuth logic
- `admin/` — everything behind a real login (`get_current_user`/`get_current_tenant` in `admin/deps.py`), mounted at `/admin`. Split by concern — **open only the file for the concern you're touching**:
  - `ui.py` — serves `static/admin.html`, nothing else, no auth dependency (deliberately — see its own docstring). Also rewrites every `/static/...` reference to append `?v=STATIC_ASSET_VERSION` before serving, since Cloudflare edge-caches `/static/*.js`/`*.css` for hours regardless of origin freshness (spec §23) — bump `STATIC_ASSET_VERSION` in this file whenever a static asset actually changes, or a redeploy can silently keep serving stale JS/CSS until the edge cache expires or is purged
  - `me.py` — `GET /api/me`: who's logged in, their tenant roles, superadmin flag — drives which UI tabs the frontend shows
  - `status.py` — `GET /api/status`
  - `events.py` — admin event CRUD (including its `targets` field, spec §20 — explicit extra destinations independent of Kingdom membership, see `EventTarget` in `models/db.py`), plus `PUT /api/events/{id}/notification-override` (a non-owning tenant's own ping channel for a kingdom-wide event — see `EventTenantNotification` in `models/db.py`, the *automatic*-fan-out counterpart to `EventTarget`)
  - `occurrences.py` — occurrence list/patch, and the two Discord-posting endpoints (`post`, `delete .../discord`) — this is where kingdom-wide fan-out AND explicit `EventTarget` posting both live, deduplicated against each other (`_post_to_one_tenant`, `_resolve_notification`)
  - `scheduler_control.py` — manual `preview` and per-tenant `regenerate`
  - `post_log.py` — PostLog list + CSV export, tenant-scoped
  - `discord_config.py` — per-tenant channel/role listing, via `get_discord_config` (reads `tenant.server.bot_token`/`tenant.server.guild_id`, spec §25 — bot credentials live on `DiscordServer` now, not `Tenant`)
  - `discord_sync.py` — the Discord↔PostLog reconciliation endpoints, tenant-scoped
  - `tenants.py` — Kingdom/Tenant/Discord Server CRUD (including `PATCH /api/kingdoms/{id}` and `GET/POST/PATCH /api/discord-servers`, spec §25), the actual "onboard a new alliance" surface; every write here is superadmin-only, `list_tenants` is scoped to what the caller actually has access to. A Tenant's create/update takes `server_id` (an existing `DiscordServer` row), not `guild_id`/`bot_token`/`public_key` directly
  - `invites.py` — tenant invites (owner-issued) and kingdom-coordinator invites (superadmin-issued), both with revocation; also owns editing/removing grants that already exist (`/api/members` for UserTenant, `/api/kingdom-coordinators` for UserKingdom) — the standing-access counterpart to the invite lifecycle above
  - `users.py` — platform-wide user listing and superadmin-flag toggling, superadmin-only; separate from `invites.py` because it edits the `User` row itself, not a per-tenant/per-kingdom grant
  - `announcements.py` — scheduled-announcement create/list/cancel/delete; delivery itself is in `scheduler/announcements.py`, not here. Delete is terminal-state-only (`posted`/`failed`/`cancelled`) — a `scheduled` one has to be cancelled first, matching the admin UI's Cancel-then-Delete two-step
  - `schemas.py` — shared Pydantic request models
  - `serializers.py` — shared `_event_dict`/`_occurrence_dict`/`_log_dict` response shaping
  - `deps.py` — shared FastAPI dependencies: `get_current_user`, `get_current_tenant` (checks real `UserTenant` access, not just "does this tenant exist"), `require_tenant_owner`, `require_superadmin`, `check_kingdom_coordinator` (a plain function, not a `Depends` — only some events need it), `get_discord_config`, `get_occurrence_with_event`, `find_post_log`, `resolve_target_tenants`/`check_target_access` (slug-resolution + access-check for an explicit multi-target list — shared by Announcements and Events' `targets` field, spec §13.3/§20). Check here before writing yet another inline lookup or permission check
  - `__init__.py` — just wires the above into one `router` (no logic to change here)

## `app/services/` — business logic, no HTTP

- `discord_api.py` — bot-token-authenticated Discord REST client (create/update/cancel scheduled events, channels, roles, token verify, send message). One cohesive concern; not split further on purpose
- `discord_oauth.py` — **separate from `discord_api.py`** on purpose: app-level OAuth2 (`client_id`/`client_secret`, authorization codes), a genuinely different Discord auth mechanism, not the same concern split arbitrarily
- `sessions.py` — signed session cookies (`SameSite=Strict` is the CSRF defense here, not a separate token scheme — see its docstring for why) and the short-lived OAuth-flow state cookies
- `audit.py` — `log_change`, called at mutation points across the admin routers; not a generic ORM-diff hook
- `time_utils.py` — `ensure_utc`, extracted after the identical tzinfo-normalization fix (aiosqlite doesn't round-trip `tzinfo` on a `DateTime(timezone=True)` column the way asyncpg does) turned up independently in three different files
- `recurrence.py` — interval-in-days + anchor-date occurrence math (no named recurrence types — see architectural decision in project memory if you need the "why")
- `validators.py` — shared Pydantic field-validator bodies for `EventIn`/`EventPatch`
- `notifications.py` — SMTP email sending (separate from Discord notifications, which live in `discord_api.py`)

## `app/scheduler/` — background jobs (APScheduler, registered in `main.py`)

Three genuinely separate jobs, one file each — this used to be one `jobs.py`
until it grew past the point where that made sense (same reasoning as the
`admin.py` split in `routers/admin/`). Every public function takes an
optional `session_factory` (defaults to `models.AsyncSessionLocal`) — these
jobs run outside any HTTP request, so they can't use FastAPI's `get_db`
dependency the way routers do, and without this parameter a test has no way
to point them at a test database instead of the real one.

- `regeneration.py` — `regenerate_occurrences` (daily at UTC 00:00, or on-demand per-tenant from the admin "regenerate" button), rebuilds the `Occurrence` table from `EventDefinition` + recurrence rules
- `reminders.py` — `send_pre_event_reminders` (per-minute)
- `announcements.py` — `send_scheduled_announcements` (per-minute); independent per-`AnnouncementTarget` delivery, same resilience principle as kingdom-wide `PostLog` fan-out

## `app/models/`

- `db.py` — every table, grouped by which phase built it (see the file's own header comment for the full map): `Kingdom`/`Tenant`/`DiscordServer` (multi-tenancy — `Tenant.server_id` FKs to `DiscordServer`, which carries `guild_id`/`bot_token`/`public_key` now that alliances sharing one Discord server is explicit rather than implied by repeated values, spec §25; `Tenant.server` is `lazy="joined"`, eager on every fetch), `User`/`UserTenant`/`UserKingdom`/`Invite`/`AuditLog` (real auth), `EventDefinition`/`EventTenantNotification`/`Occurrence`/`PostLog` (events), `Announcement`/`AnnouncementTarget` (scheduled announcements), `SchedulerState` (per-tenant job status)
- `__init__.py` — engine/session factory, `get_db()` dependency, `DATABASE_URL` env lookup

## `app/static/` — admin UI, no build step, no framework

Plain HTML/CSS/JS served directly by `routers/admin/ui.py` (`admin.html`) and
`routers/events.py` (`events.html`, the public calendar page). Classic
`<script src>` tags loaded in order — not ES modules — so top-level
functions/`let`/`const` in one file are reachable from files loaded after it
in the same page; load order in `admin.html` is the dependency order.

- `admin.css` — all admin UI styles
- `admin.html` — markup + view sections only; no inline CSS or logic
- `js/theme.js` — self-contained IIFE, color palettes + `window.applyTheme`/`window.SAMAYA_THEMES`; loaded in `<head>` to avoid a flash of the wrong theme
- `js/common.js` — `api()` (cookie-based auth now, no header key), `toast()`, `showView()`, `loadMe()`/`ME`/`applyRoleVisibility()` (drives which tabs show), `loadTenants()`/`TENANT_COLORS` — load-bearing for every other `js/*.js` file below. Also owns the header's two controls (spec §24): `renderTenantLink()`/`openTenantModal()`/`selectTenant()` (alliance) and `startHeaderClock()`/`openTimezoneModal()`/`selectTimezone()` (time zone) — both text-link-plus-modal now, not `<select>`s
- `js/dashboard.js`, `events.js`, `schedule.js`, `gantt.js`, `postlog.js`, `config.js`, `sync.js`, `access.js`, `platform.js` — one per admin view, matching `admin.html`'s `<!-- DASHBOARD -->` / `<!-- EVENTS -->` / etc. section comments. `access.js` (owner-only: invite management) and `platform.js` (superadmin-only: Kingdom/Tenant/Discord Server creation, spec §25 — a Tenant's create/edit prompts pick a `DiscordServer` by id rather than entering a guild ID/bot token inline; those live on the server now) are shown/hidden per `applyRoleVisibility()`
- `js/init.js` — `loadMe()` then `loadTenants()` then paints the dashboard; handles the "logged in but no tenant access yet" empty state
- `events.html` — standalone, no shared JS with `admin.html` (own inline `<script>` block). Mirrors `admin.html`'s header pattern (spec §26.2/§26.3): `formatHeaderClock()`/`startHeaderClock()`/`openTimezoneModal()`/`selectTimezone()` (same `samaya_display_tz` `localStorage` key as admin) and `loadAlliances()`/`renderAllianceLink()`/`openAllianceModal()` (alliance switcher, backed by `GET /api/alliances`) — duplicated logic, not imported, since this page has no shared JS files by design (§22)

## `app/tests/`

In-memory SQLite (`aiosqlite`) via `conftest.py`'s fixtures — never touches
production Postgres. Key fixtures: `tenant`/`second_tenant` (two tenants in
one Kingdom), `test_user` (a **superadmin** by design — business-logic tests
shouldn't also be implicitly testing the permission model), `client` (logged
in as `test_user`), `make_user_and_client` (factory for a specific
non-superadmin user with specific `UserTenant`/`UserKingdom` grants — use this
whenever `client`'s superadmin default would hide the thing being tested).
Organized by what's tested, not by router file:

- `test_routes_public.py` — unauthenticated, tenant-scoped public routes (`events.py`, `ics.py`), including cross-tenant isolation
- `test_routes_events.py` — admin event CRUD, `scope` field validation, kingdom-wide read visibility across tenants
- `test_post_occurrence.py` — the Discord-posting race-condition/error-path tests, plus kingdom-wide fan-out (`TestKingdomWidePost`); monkeypatches `routers.admin.occurrences.create_discord_event` (patch target must match wherever a name is actually imported, not where it's defined)
- `test_event_targets.py` — spec §20's explicit `EventTarget` rows: the API surface (create/patch/access-control), posting fan-out generalization, dedup against kingdom-wide's automatic fan-out, and notification-resolution isolation between targets
- `test_auth.py` — session token round-trip/tamper rejection, `_claim_invite`'s atomic race guard, the full HTTP invite-claim round-trip (not just the unit-level function)
- `test_auth_permissions.py` — the permission model itself: no-session→401, no-tenant-access→403, coordinator-vs-owner, kingdom-coordinator (an alliance owner does NOT automatically get this), superadmin-only tenant/kingdom CRUD
- `test_announcements.py` — creation validation (2000-char Discord limit, empty targets, past `scheduled_for` rejected with 422), permission scoping to the creator's own tenant access, delivery (including one-target-failure not blocking another), cancellation, and deletion (`TestDeleteAnnouncement` — terminal-state-only, 400 on a still-`scheduled` one, 404 on a nonexistent id). Delivery tests use `_run_delivery_job` to inject a test session factory — see that helper's docstring if a delivery test needs to call the job function itself. `_future_iso(minutes=60)` builds a genuinely-future timestamp for creation payloads (the past-date guard rejects anything else); `_backdate_to_due()` creates via the API with a valid future time then mutates `scheduled_for` directly on the ORM row afterward, for tests that need a "due" announcement to feed to the delivery job
- `test_access_management.py` — editing/removing an already-claimed UserTenant or UserKingdom grant, Kingdom editing, and platform-wide user/superadmin management, including the sole-owner and can't-self-demote guards
- `test_discord_servers.py` — spec §25's `DiscordServer` entity: CRUD + superadmin gating on `/api/discord-servers`, Tenant create/update via `server_id` (including 404 on an unknown one), and the actual point of the redesign — two tenants sharing one server, and a bot-token update on that server reaching both with no per-tenant override to go stale
- `test_recurrence.py` — `services/recurrence.py` math, no HTTP
- `test_validation.py` — `services/validators.py` / schema validation, including the `scope` field

Run with `pip install -r app/requirements-dev.txt && cd app && pytest`.
`requirements.txt` is runtime-only (what the Docker image installs);
`requirements-dev.txt` layers pytest + aiosqlite on top for local/test use.

## Conventions worth knowing before editing

- All timestamps are UTC. Not every DB driver round-trips `tzinfo` on a `DateTime(timezone=True)` column the same way (asyncpg does, aiosqlite doesn't) — use `services/time_utils.ensure_utc()` rather than assuming a value already has `tzinfo` set
- Tenant selection in the admin API is a request header (`X-Tenant-Slug`), not a URL path segment — a bridge-period-turned-permanent choice so route paths didn't need to change again when Phase 4 added real per-user access checks on top of it. Public routes use a path segment instead (`/t/{slug}/...`), since calendar apps can't send custom headers
- A `kingdom-wide` event has exactly one `Occurrence` row but fans out to one independent `PostLog` row per `Tenant` in its `Kingdom` when posted — each tenant's Discord post succeeds, fails, or gets cancelled independently of the others (see `occurrences.py`'s `_post_to_one_tenant`)
- Being an `owner` of one alliance does **not** imply kingdom coordinator access (the right to create/edit kingdom-wide events) — that's a separate `UserKingdom` grant, superadmin-issued only, by deliberate design
- Scheduler jobs (`scheduler/*.py`) open their own DB sessions directly rather than through FastAPI's `get_db` — that's why they take an injectable `session_factory` parameter; a test calling one of these functions directly must pass a test session factory, or it will try to reach the real database
- Single uvicorn worker only — APScheduler runs in-process in `main.py`'s lifespan; a second worker would double-run the daily regeneration and the two per-minute jobs
