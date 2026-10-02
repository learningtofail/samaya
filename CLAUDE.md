# Samaya — repo map

FastAPI + PostgreSQL + Discord event scheduler for the Kingshot K138 community,
deployed at `/opt/taraka` on `lxc-taraka`, serving `ks138.taraka.dev`. Multi-tenant:
several alliances, each their own `Tenant`, plus events shared across a whole
Kingdom. This file exists so you (Claude) can find the 1-2 files a task
actually needs instead of reading the whole tree. Update it when files are
added, removed, or renamed, not on every logic change inside a file.
`docs/samaya-specification.md` Part I is the current-state spec; §66 is the
design record for the event model.

## Top level

- `README.md` — live URLs, stack, quick start, common ops commands
- `docker-compose.yml` — app + Postgres 16, Caddy/Cloudflare Tunnel in front (not in this repo)
- `.env.example` — required env vars (`DB_PASSWORD`, `SECRET_KEY`, `DISCORD_OAUTH_CLIENT_ID`/`_SECRET`/`_REDIRECT_URI`, `SUPERADMIN_DISCORD_IDS`, `PLATFORM_BOT_TOKEN`, `PLATFORM_PUBLIC_KEY`, SMTP settings)
- `package.json` / `eslint.config.mjs` — lints `app/static/*.html` (inline scripts) and top-level `app/static/*.js` (`events-public.js`, `feedback.js`) only. The admin console's `app/static/js/*.js` files are not covered, a known gap. No JS build step and no bundler in the shipped app. `npm test` runs vitest (dev only)
- `vitest.config.js`, `tests/frontend/events-public.test.js` — unit tests for `events-public.js`'s pure functions. The real script is loaded via `new Function(source)` against a minimal jsdom `document`; setting `globalThis.__SAMAYA_TEST__ = true` makes the script export its pure section to `globalThis.__SAMAYA_TEST_EXPORTS__` and return before any DOM wiring. The flag is never set in the browser
- `app/alembic/` — **the only convention for schema changes**. `env.py` reads `DATABASE_URL` and uses `models.db.Base.metadata`. Revisions: `6fc935931248` (baseline as of spec §62), `a1f0c0de0001` (event model tables), `a1f0c0de0002` (feedback moderation and responses), `a1f0c0de0004` (spec §67: destinations, audience groups, secondary servers, merged deliveries; additive, with a downgrade; seeds one "Notifications" destination per alliance from its channel and role, see `tests/test_migration_0004.py`, opt-in via `SAMAYA_MIGRATION_TEST_PG`), `a1f0c0de0005` (spec §68: destinations become Kingdom-owned Audiences holding several server/channel destinations, groups folded in; merges identical per-alliance destinations; has a downgrade that does not rebuild groups; see `tests/test_migration_0005.py`, same opt-in), `a1f0c0de0003` (closure: drops the replaced tables; destructive, no downgrade; refuses to run while `post_log` still holds Discord event IDs, see `app/cutover_delete_old_discord_events.py`). Foreign keys in guarded SQL are dropped by what they reference, not by name, because production's names came from the old hand-written scripts. README's "Schema Migrations" section has the day-to-day workflow
- `app/migrate_*.py` (10 files, including `migrate_to_multitenant.py`) — **historical only**, superseded by Alembic; never run again. They reference tables that no longer exist
- `app/register_discord_commands.py` — registers the slash commands from `services/discord_commands.COMMANDS` with Discord under the bot token's own application, which can differ from the OAuth login application (dry run by default, `--apply` to send). Run in the container: `docker compose run --rm app python register_discord_commands.py`
- `app/cutover_delete_old_discord_events.py` — one-time cutover step: deletes the Discord Scheduled Events the old system created (IDs from `post_log`), dry run by default, `--apply` to delete. Tested in `tests/test_cutover_script.py`. Lives in `app/` so it runs inside the container
- `.github/workflows/ci.yml` — on every push and PR to `master` and `unified-event-model`: `pytest` (in-memory SQLite) after `ruff check .`; a `frontend` job (eslint + vitest); `docker-build` (builds `app/Dockerfile`, boots it against a real `postgres:16-alpine` after `alembic upgrade head`, curls `/api/kingdom-branding`); `alembic-baseline` (`alembic upgrade head` then `alembic check`, so a model change with no migration fails CI)
- `pyproject.toml` — `[tool.ruff.lint]` config; `E711`/`E712` are ignored because `== True`/`== None` is required SQLAlchemy filter syntax, `E701` because one-line `if x: return y` is this codebase's convention
- `ops/backup.sh` — nightly `pg_dump` via `docker compose exec db`, gzipped, 14 day rotation; runs on the LXC host (crontab line in its header)
- `ops/dev_smoke_engine.py` — runs the delivery engine against a real Discord test server. Dev bot and dev database only, never production credentials
- `docs/cutover-runbook.md` — the one-time, destructive move from the old event system to the unified model (backup, export, stop app, delete old Discord events, migrate, start, configure, smoke test)
- `docs/rollback-runbook.md` — code-only rollback, migration rollback (and when `alembic downgrade` cannot be trusted), restore from `ops/backup.sh`, and the cutover rollback
- `docs/alliance-branding-guidelines.md` (spec §63, deferred) — what an alliance leader submits and what the platform derives from it

## `app/main.py`

Entrypoint. Owns the startup check (`SECRET_KEY` and Discord OAuth credentials
must be set or the app refuses to boot), the `lifespan` (registers the two
scheduler jobs, runs one catch-up `generation_job()`, starts APScheduler), the
validation-error handler, and router/static wiring. It does not create tables:
`alembic upgrade head` is a deploy step. Single uvicorn worker only.

## `app/routers/`

- `events.py` — public, unauthenticated. `GET /t/{slug}/api/events`, `/api/events`, `/events`, `/t/{slug}/events`, `/api/alliances`, `/api/kingdom-branding`, `/api/last-activity`. Rows come from `services/public_events.public_rows`; HTML goes through `bust_static_cache()`
- `ics.py` — public ICS feeds, `/t/{slug}/ics/events.ics` and the combined feed, also via `public_rows`
- `tickets_public.py` — the public feedback board: `GET /feedback`, `GET/POST /api/tickets` (`GET` returns `{active, archived}`), `POST /api/tickets/{id}/vote` (anonymous toggle keyed by `X-Voter-Id` hashed with `SECRET_KEY`). Rate limited per IP via `services/rate_limit.py`. Never returns `submitter_contact`
- `webhooks.py` — `POST /webhooks/discord`, signature verified against `PLATFORM_PUBLIC_KEY`; answers PING and dispatches slash commands, autocomplete and modal submits to `services/discord_commands.py` (spec §70)
- `auth.py`, `auth_pages.py` — Discord OAuth login, invite claim, logout, and the two static failure pages. OAuth only grants access by consuming a pending `Invite`
- `admin/` — everything behind a login, mounted at `/admin`, tenant chosen by `X-Tenant-Slug` (`*` = every accessible alliance, read-only lists). Open only the file for the concern:
  - `ui.py` — serves `admin.html` (no auth dependency, on purpose), rewrites `/static/...` with `?v=STATIC_ASSET_VERSION`
  - `unified_events.py` + `unified_schemas.py` — `GET/POST/PATCH/DELETE /api/event-types` and `/api/events` (create falls back to the event type's defaults; PATCH/DELETE return `discord_errors` when a Discord call failed). Pydantic request models are in `unified_schemas.py`
  - `unified_engine.py` — `GET /api/occurrences`, `PATCH /api/occurrences/{id}` (cancel, restore, move, clear move, message override), `POST /api/events/{id}/split` (the "this and following" edit), `GET /api/deliveries`, `POST /api/deliveries/{id}/retry`, `GET /api/delivery-health`
  - `tickets.py` — admin feedback triage: list (all statuses, responses, `submitter_contact`), `PATCH` (status, title, description, kind, error_type, audited), `DELETE` (superadmin), responses `POST/PATCH/DELETE` (author or superadmin)
  - `me.py` — `GET/PATCH /api/me` (`display_name`)
  - `tenants.py` — Kingdom, Tenant and Discord Server CRUD (superadmin writes) and secondary servers per alliance (`secondary_server_ids`; superadmin). Servers belong to a Kingdom. Uniqueness collisions go through `services/db_errors.raise_friendly_integrity_error()`
  - `invites.py` — tenant and kingdom-coordinator invites, plus editing and removing existing grants (`/api/members`, `/api/kingdom-coordinators`)
  - `users.py` — platform-wide user list, superadmin flag, display name (superadmin only)
  - `audiences.py` — `/api/audiences` (a Kingdom-owned, named list of destinations: server + channel + optional role; `leadership_only`; Kingdom coordinators write), `PUT /api/alliance-audiences` (an alliance's links and `post_by_default`; owner, coordinator or superadmin) and `/api/kingdom-servers` (names only, for the editor). A used Audience returns 409 listing users. Every write calls `resync_kingdom_events`
  - `discord_config.py` — `GET /api/discord/channels`, `/roles`, guild info, via `get_discord_config` (bot credentials come from `tenant.server`)
  - `audit_log.py` — `GET /api/audit-log`, read-only over `AuditLog` rows written by `services/audit.log_change`
  - `schemas.py` — request models for the setup endpoints (tenants, servers, kingdoms, invites, users, notification destination)
  - `deps.py` — shared dependencies: `get_current_user`, `get_current_tenant(s)`, `require_tenant_owner`, `require_not_viewer` (superadmin bypasses, `viewer` gets 403), `require_superadmin`, `check_kingdom_coordinator`, `get_discord_config`, `resolve_target_tenants`/`check_target_access`, `PLATFORM_BOT_TOKEN`. Check here before writing an inline lookup or permission check
  - `__init__.py` — wires the above into one `router`

## `app/services/`

- `destinations.py` — resolves where an event posts (§67, §68): `resolve_destinations` (targets = alliance x destination of each chosen Audience), `plan_destinations`, `group_sends` (merge on guild + channel + offset), `find_conflicts`. The only place that decides destinations; the Events form calls it through `POST /api/events/preview-destinations`
- `event_engine.py` — the delivery engine (spec §66.4). `sync_event_occurrences` (idempotent occurrence and delivery generation, never touches cancelled or moved occurrences), `run_generation`, `run_delivery_tick` and `process_delivery` (atomic claim, commit, then call Discord: at most once per delivery), `recover_stale_claims`, `apply_event_change`, `remove_event_from_discord`, `refresh_posted_discord_events`. Takes an injectable Discord client (`get_discord()` returns `services/discord_api` in production) so tests use `FakeDiscord`
- `public_events.py` — `public_rows()`, the single query path for every public read. Excludes inactive and leadership-only events; one row per kingdom-wide event, one per audience alliance in the combined view
- `discord_commands.py` — `/schedule`, `/next` and `/feedback` (spec §70): `COMMANDS` definitions, handlers that take the interaction dict and return the response dict (reads go through `public_rows`), per-user feedback limit
- `discord_client.py` — `get_discord()`
- `discord_api.py` — bot-token Discord REST client (create/update/cancel scheduled events, channels, roles, send message, token verify). `update_discord_event` takes optional `start`/`end`. Logs every non-2xx and network error
- `discord_oauth.py` — app-level OAuth2 (separate from the bot client on purpose)
- `recurrence.py` — `occurs_on()`, the single recurrence predicate (`interval_days` from an anchor date, or `none`, with optional `until_date`)
- `templates.py` — `render_placeholders()`: `{alliance_name}`, `{kingdom_name}`, `{send_time}`, `{send_time_relative}`, `{event_time}`, `{event_time_relative}`. Plain regex over a fixed name set; an unknown `{x}` is left as written
- `validators.py` — shared Pydantic field validators (times, dates, intervals, reminders, colors, cover image data URI under about 8MB)
- `ticket_views.py` — status groups and response/author shaping for the feedback board. A response author shows as `display_name` or "Team", never the Discord username
- `db_errors.py` — `IntegrityError` to friendly 422, normalized across SQLite and Postgres
- `sessions.py` (signed session cookie, `SameSite=Strict` is the CSRF defense), `audit.py` (`log_change`), `time_utils.py` (`ensure_utc`), `notifications.py` (SMTP), `rate_limit.py` (in-memory per-IP limiter, valid because there is one worker; `SAMAYA_DISABLE_RATE_LIMIT=1` in tests), `static_assets.py` (`STATIC_ASSET_VERSION`, `bust_static_cache()`)

## `app/scheduler/`

`unified_jobs.py` only: `generation_job` (daily 00:00 UTC and once at startup) and `delivery_tick_job` (every minute), thin wrappers over `services/event_engine.py` registered in `main.py`.

## `app/models/`

- `db.py` — every table: `Kingdom`, `DiscordServer`, `Tenant` (`notification_*` are legacy until revision 0006), `TenantSecondaryServer`, `Audience`, `AudienceDestination`, `AllianceAudience`, `EventAudience`, `User` (with `display_name`), `UserTenant`, `UserKingdom`, `Invite`, `AuditLog`, `Ticket`, `TicketVote`, `TicketResponse`, `EventType`, `Event`, `EventAlliance`, `EventReminder`, `EventOccurrence`, `Delivery`. Each has its own docstring
- `__init__.py` — engine and session factory, `get_db()`, `DATABASE_URL` lookup

## `app/static/` — no build step, no framework

Classic `<script src>` files in dependency order (not ES modules), so top-level names are shared across the admin scripts. Bump `STATIC_ASSET_VERSION` in `services/static_assets.py` on any static change; Cloudflare caches `/static/*` for hours.

- `admin.html` / `admin.css` — markup and styles only. No inline `style=`, no inline event handlers; hide with the `.hidden` class via `classList`, never `.style.display`. Modals use `focusModal()`/`unfocusModal()` and `data-modal-dismiss`. New CSS is BEM. Every tab ends in a collapsed `<details class="about-page">` explainer
- `js/common.js` — `api()` (cookie auth, explicit `tenantOverride` slug), `toast()`, `showView()`, `loadMe()`/`ME`/`applyRoleVisibility()`, `loadTenants()`, per-tab alliance filters (`getTabFilter`/`setTabFilter`, `localStorage` key `samaya_filter_<tab>`, `'*'` = all), header clock and time zone modal, modal focus helpers, tab and dismiss delegation
- `js/events.js` — Events tab: filtered list, the create/edit form, the Audiences panel (multi-select with live "Posts to N audiences in M channels" and conflicts from the preview call), the three-way edit scope dialog (this occurrence: `PATCH /api/occurrences/{id}`; this and following: `POST /api/events/{id}/split`; all: `PATCH /api/events/{id}`)
- `js/composer.js` — message composer: markdown toolbar, live preview (a browser-side approximation of `services/templates.py`), emoji picker, role mention picker
- `js/reminders.js` (reminder chips), `js/pickers.js` (Discord channel/role pickers for one alliance and one of its servers, text fallback when Discord is unreachable)
- `js/schedule.js` — Schedule tab: occurrences as a table or timeline, with cancel, restore, move, message edit
- `js/deliveries.js` — Delivery log tab: health card, filters, retry
- `js/types.js` — Event types tab
- `js/tickets.js` — Feedback tab: moderation, edit, dismiss, responses
- `js/setup.js`, `js/access.js`, `js/platform.js` — Setup tab: display name, Kingdom audiences with a multi-destination editor and the alliances that use each one (setup.js), then Discord servers with a Kingdom (platform.js, plus each alliance's primary and secondary servers), then (superadmin) access and invites, kingdoms, Discord servers, alliances with icon upload, users
- `js/audit.js` — Audit log tab; `js/init.js` — startup, branding title, empty state for a user with no tenant access
- `events.html` / `events.css` / `events-public.js` — the public schedule page (spec §62): standalone light theme, no PatternFly, one IIFE with no globals. No crests (rows show alliance names). Alliance filter chips, live-now hero with a progress bar, schedule by day, list and calendar views, 24-hour time, Add-to-Calendar menu, Discord preview, report-an-issue. Rows carry `kind` (`event` or `announcement`), `type`, `reminder_minutes`. Fonts load from Google Fonts unless self-hosted under `static/vendor/`
- `feedback.html` / `feedback.css` / `feedback.js` — the public board: active tickets, a collapsed Archived section (read-only), team responses with the author's display name. Per-browser `samaya_voter_id` in `localStorage`

## `app/tests/`

In-memory SQLite via `conftest.py`. Fixtures: `tenant`/`second_tenant` (one Kingdom), `test_user` (a **superadmin** by design), `client` (logged in as `test_user`), `client_no_session`, `make_user_and_client(tenant_grants=..., kingdom_grants=..., discord_id=...)` (returns `(client, user_id)`; use it whenever the superadmin default would hide the thing being tested), and for the engine `fake`/`sf`/`configured` with `unified_helpers.py` (`FakeDiscord`, `make_event`, `sync`, `deliveries`, `NOW`, `UTC`). Organized by what is tested:

- `test_destinations_engine.py`, `test_audiences_api.py`, `test_discord_api_rate_limit.py`, `test_migration_0004.py`, `test_migration_0005.py` — spec §67 and §68: merging, conflicts, leadership, API permissions, 429 handling, the 0004 and 0005 migrations (need Postgres)
- `test_unified_models.py` — database-level CHECK and UNIQUE guarantees
- `test_unified_events.py` — event type and event API: validation, defaults, audit, visibility, permissions
- `test_event_engine.py` — generation, tick, at-most-once, failure isolation, shared guilds, reminders, per-occurrence edits, split, retry, delivery log and health (fake Discord)
- `test_public_surface.py` — every public route against a leadership-only event, ICS, last-activity, combined and per-alliance views
- `test_feedback_board.py` — public board, moderation, responses, archive, display names
- `test_validation.py` — field validation on event creation (each rejection names the field)
- `test_cutover_script.py` — the cutover Discord deletion script
- `test_discord_commands.py`, `test_register_discord_commands.py` — signed interactions against `/webhooks/discord` (schedule, next, autocomplete, feedback modal and ticket, rate limit) and the registration script
- `test_auth.py`, `test_auth_permissions.py`, `test_access_management.py`, `test_discord_servers.py`, `test_admin_consolidation.py` (tenant icon, kingdom branding, config overview), `test_audit_log.py` — auth, permission model, access, setup
- `test_recurrence.py`, `test_templates.py`, `test_db_errors.py`, `test_discord_oauth.py`, `test_notifications.py`, `test_rate_limit.py`, `test_static_assets.py`, `test_ui.py` — pure units

Run with `pip install -r app/requirements-dev.txt && cd app && pytest`.

## Conventions worth knowing before editing

- All timestamps are UTC. Use `services/time_utils.ensure_utc()`: aiosqlite does not round-trip `tzinfo` the way asyncpg does
- A Postgres CHECK passes on NULL, so write `IS NOT NULL` explicitly when a column must be set
- Pydantic `allow_none` validators need `lambda cls, v:` wrapping
- Admin tenant selection is the `X-Tenant-Slug` header; public routes use `/t/{slug}` because calendar apps cannot send headers
- Event types and kingdom-wide events need a `UserKingdom` grant; owning an alliance does not imply it
- Leadership-only events never create Discord Scheduled Events and are never public. Anything public must read through `services/public_events.public_rows`
- Jobs open their own sessions, so engine functions take an injectable `session_factory` and Discord client; a test must pass the test factory
- Single uvicorn worker only; APScheduler runs in-process
