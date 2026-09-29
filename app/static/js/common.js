// Shared helpers loaded before every other admin view script (dashboard.js,
// events.js, schedule.js, gantt.js, postlog.js, config.js, sync.js,
// access.js, platform.js). Classic <script> tags, not ES modules, so the
// functions and consts declared here are reachable as plain globals from
// every file loaded after this one — see the <script src> order in
// admin.html.

// ── Current user ─────────────────────────────────────────────
// Populated by loadMe() before anything else runs (see init.js). Drives
// which tabs/buttons show at all — e.g. the Access tab only appears for
// a tenant owner, the Platform tab only for a superadmin.
let ME = null;

// ── Modal close: Escape key ──────────────────────────────────
// Every modal backdrop (event/announcement/template/tenant/timezone) has
// a data-close-fn attribute naming its own close function — an X button
// in each modal's header already calls that function directly on click;
// this one listener covers Escape for all of them without each modal
// needing its own keydown handler. Registered once, here, since common.js
// loads before every modal-owning view script.
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  const openModal = document.querySelector('.pf-v6-c-backdrop.open');
  if (!openModal) return;
  const fnName = openModal.dataset.closeFn;
  if (fnName && typeof window[fnName] === 'function') window[fnName]();
});

async function loadMe() {
  ME = await api('GET', '/api/me', null, /*skipTenantHeader=*/true);
  return ME;
}

// Spec §38: the admin console has exactly two meaningful tiers now —
// superadmin (full read/write, everywhere, including the merged Access &
// Platform console and the Audit Log) and everyone else (view-only in
// practice). There aren't enough distinct admins yet to warrant the old
// per-tenant owner/coordinator nuance in the UI layer, so visibility
// checks below collapsed onto ME.is_superadmin alone. The underlying
// owner/coordinator/viewer roles still exist server-side (UserTenant.role)
// and write endpoints still enforce them (require_not_viewer,
// require_tenant_owner) — this is a UI simplification, not a permissions
// change.
function isSuperadmin() {
  return !!(ME && ME.is_superadmin);
}

// ── Tenants + per-tab alliance filters (spec §38.1) ──────────
// There is no more one global "current tenant" governing every tab — the
// header alliance switcher is gone. Each data-bearing tab (Dashboard,
// Events, Announcements, Post Log, Sync, Discord Config) keeps its own
// independent filter selection, defaulting to "All" (COMBINED_SLUG),
// remembered per tab so narrowing one tab doesn't affect the others.
let TENANTS = [];               // populated by loadTenants()
const TENANT_COLORS = {};       // tenant.id -> tenant.color
const TENANT_ICONS = {};        // tenant.slug -> icon_image_data (or null)

// The literal slug '*' is the combined-view pseudo-tenant: sent as-is in
// the X-Tenant-Slug header, which deps.get_current_tenants on the server
// recognizes as "every tenant I have access to" for read/list endpoints.
// Write endpoints never receive '*' — a create/edit modal always sends
// one explicit real slug (its own Alliance field, or api()'s
// tenantOverride param), never the filter's current value.
const COMBINED_SLUG = '*';

function getTabFilter(tab) {
  return localStorage.getItem('samaya_filter_' + tab) || COMBINED_SLUG;
}
function setTabFilter(tab, slug) {
  localStorage.setItem('samaya_filter_' + tab, slug);
}

async function loadTenants() {
  TENANTS = await api('GET', '/api/tenants', null, /*skipTenantHeader=*/true);
  TENANTS.forEach(t => { TENANT_COLORS[t.id] = t.color; TENANT_ICONS[t.slug] = t.icon_image_data || null; });
}

// Renders a small "Alliance: [dropdown]" filter into the given <select>,
// used identically by every combined-capable tab. `onChange` is called
// (with no args) after the tab's own filter state updates — the caller
// re-fetches/re-renders its own data using getTabFilter(tab) again.
function renderAllianceFilterSelect(selectId, tab, onChange) {
  const select = document.getElementById(selectId);
  if (!select) return;
  const current = getTabFilter(tab);
  const options = [{ slug: COMBINED_SLUG, name: '— All Alliances —' }, ...TENANTS.map(t => ({ slug: t.slug, name: t.name }))];
  select.innerHTML = options.map(o =>
    `<option value="${escapeHtml(o.slug)}" ${o.slug === current ? 'selected' : ''}>${escapeHtml(o.name)}</option>`
  ).join('');
  select.onchange = () => { setTabFilter(tab, select.value); onChange(); };
}

// Populates a plain "which alliance does this belong to" <select> for a
// create/edit modal (spec §38.1) — every such select uses this, since
// there's no more ambient "current tenant" to imply the answer.
function renderOwningTenantSelect(selectId, selectedSlug) {
  const select = document.getElementById(selectId);
  if (!select) return;
  select.innerHTML = TENANTS.map(t =>
    `<option value="${escapeHtml(t.slug)}" ${t.slug === selectedSlug ? 'selected' : ''}>${escapeHtml(t.name)}</option>`
  ).join('');
}

// Spec §49 — the combined "Owning Alliance" selector Events and
// Announcements both use, replacing a plain alliance <select> plus a
// separate "Kingdom-wide" checkbox with a single control: every option
// is either one alliance (scope=alliance, that tenant owns it) or that
// same alliance's kingdom-wide option (scope=kingdom-wide, that tenant
// still does the actual posting/owns the row — see EventDefinition/
// Announcement.scope's own docstrings for why an owning tenant is still
// needed even when kingdom-wide). Option values are "alliance:<slug>" or
// "kingdomwide:<slug>"; callers split on the first ":" to recover both
// the scope and the slug. Also used for reassigning an existing row to a
// different alliance (or into/out of kingdom-wide) from the same control
// on edit, not just at creation.
function renderOwningTenantScopeSelect(selectId, selectedSlug, selectedScope) {
  const select = document.getElementById(selectId);
  if (!select) return;
  const allianceOptions = TENANTS.map(t =>
    `<option value="alliance:${escapeHtml(t.slug)}">${escapeHtml(t.name)}</option>`
  ).join('');
  const kingdomOptions = TENANTS.map(t =>
    `<option value="kingdomwide:${escapeHtml(t.slug)}">🌐 Kingdom-wide (posted via ${escapeHtml(t.name)})</option>`
  ).join('');
  select.innerHTML =
    `<optgroup label="Alliance">${allianceOptions}</optgroup>` +
    `<optgroup label="Kingdom-wide">${kingdomOptions}</optgroup>`;
  const kind = selectedScope === 'kingdom-wide' ? 'kingdomwide' : 'alliance';
  const wanted = `${kind}:${selectedSlug}`;
  select.value = wanted;
  // Fall back to the first option (rather than leaving the browser's
  // default blank-ish selection) when selectedSlug doesn't match any
  // known tenant — e.g. TENANTS[0] not loaded yet.
  if (select.value !== wanted) select.selectedIndex = 0;
}

// Splits a renderOwningTenantScopeSelect option value back into
// { scope, slug } — the one place both events.js and announcements.js
// parse it, so the "alliance:"/"kingdomwide:" encoding only lives here.
function parseOwningTenantScopeValue(value) {
  const idx = value.indexOf(':');
  const kind = value.slice(0, idx);
  const slug = value.slice(idx + 1);
  return { scope: kind === 'kingdomwide' ? 'kingdom-wide' : 'alliance', slug };
}

// Shows/hides the merged Access & Platform tab and the Audit tab —
// superadmin-only now (see isSuperadmin()'s comment above). Called once
// after login; there's no more per-tenant-switch re-check needed since
// this no longer depends on which alliance is selected anywhere.
function applyRoleVisibility() {
  const accessItem = document.getElementById('accessTabItem');
  const auditItem = document.getElementById('auditTabItem');
  if (accessItem) accessItem.style.display = isSuperadmin() ? '' : 'none';
  if (auditItem)  auditItem.style.display = isSuperadmin() ? '' : 'none';
}

// ── HTML escaping ────────────────────────────────────────────
// Every value interpolated into innerHTML below that originates from
// the database (event names, channel labels, descriptions, etc.) must
// go through this — those fields are admin-editable text, not fixed
// UI strings, so treat them as untrusted.
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

const GANTT_PALETTE = ['#bbf7d0','#bfdbfe','#fed7aa','#fde68a','#e9d5ff','#99f6e4','#fecaca','#d9f99d','#fbcfe8','#a5f3fc','#c7d2fe','#fef08a'];
const DOW3 = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
let occurrenceData = [];

// ── Display time zone (spec §15) ────────────────────────────
// Governs the "local" half of every dual-time display (dualTimeString/
// fmtTime/fmtDateTime in events.js) across the whole admin UI — global,
// not tied to any one view, hence living here rather than in events.js
// where it used to be buried inside the Add/Edit Event modal.
//
// Same IANA zone list the old mTimezone dropdown offered — kept as the
// fallback for a browser without Intl.supportedValuesOf (see allTimeZones
// below), and as the "detected zone" fast path when it's already common.
const DISPLAY_TZ_OPTIONS = [
  'UTC', 'America/Toronto', 'America/New_York', 'America/Chicago',
  'America/Denver', 'America/Los_Angeles', 'Europe/London', 'Europe/Paris',
  'Asia/Singapore', 'Asia/Tokyo', 'Australia/Sydney',
];

// Full IANA time zone database (~400 zones) rather than the ~10-option
// curated list above — Intl.supportedValuesOf('timeZone') is the
// standard way to get it (Baseline widely-available since 2023; every
// browser this app already requires for Intl.DateTimeFormat's own
// timeZoneName option supports it). Falls back to the curated list on an
// older engine that lacks it rather than throwing.
function allTimeZones() {
  try {
    if (typeof Intl.supportedValuesOf === 'function') {
      return Intl.supportedValuesOf('timeZone');
    }
  } catch (e) { /* fall through to the curated list below */ }
  return DISPLAY_TZ_OPTIONS;
}

function getDisplayTz() {
  // Falls back to the browser-detected zone, not a hardcoded 'UTC' —
  // the old version's default was the actual bug this section fixes:
  // a new user saw their own zone named right next to the control and
  // still got UTC everywhere until they opened the Event modal and
  // picked it themselves.
  return localStorage.getItem('samaya_display_tz')
    || Intl.DateTimeFormat().resolvedOptions().timeZone
    || 'UTC';
}

function setDisplayTz(tz) {
  localStorage.setItem('samaya_display_tz', tz);
}

// Header's time-zone control (spec §24): a live "HH:MM UTC · HH:MM <zone>"
// clock, rather than the old bare <select id="displayTzPicker"> — clicking
// it opens the Time Zone modal (openTimezoneModal below) to change it.
// Needs no login/tenant context, same as the picker it replaces, so
// init.js starts it immediately rather than waiting on loadMe().
let _headerClockTimer = null;

function formatHeaderClock() {
  const now = new Date();
  const utcStr = now.toLocaleTimeString('en-GB', { timeZone: 'UTC', hour: '2-digit', minute: '2-digit' });
  const tz = getDisplayTz();
  if (tz === 'UTC') return `🕐 ${utcStr} UTC`;
  let localStr;
  try {
    localStr = now.toLocaleTimeString('en-GB', { timeZone: tz, hour: '2-digit', minute: '2-digit' });
  } catch (e) {
    // An invalid/unrecognized IANA zone name (shouldn't happen via the
    // modal's own option list, but guards a hand-edited localStorage
    // value) — fall back to showing UTC only rather than throwing.
    return `🕐 ${utcStr} UTC`;
  }
  return `🕐 ${utcStr} UTC · ${localStr} (${tz})`;
}

function startHeaderClock() {
  const el = document.getElementById('headerClock');
  if (!el) return;
  const tick = () => { el.textContent = formatHeaderClock(); };
  tick();
  if (_headerClockTimer) clearInterval(_headerClockTimer);
  _headerClockTimer = setInterval(tick, 30000); // minute-resolution display, 30s is plenty
}

// firstVisit=true renders extra explanatory copy and is triggered
// automatically once, the first time someone opens the admin UI with no
// stored preference yet (see maybeShowFirstVisitTzModal below) — every
// other call (the header clock click) is a plain re-open.
function openTimezoneModal(firstVisit) {
  const list = document.getElementById('timezoneModalList');
  const intro = document.getElementById('timezoneModalIntro');
  const current = getDisplayTz();
  if (intro) {
    intro.textContent = firstVisit
      ? `We show event times in both UTC and your local time zone so nothing gets missed across time zones. We've detected ${current} from your browser — pick a different one below if that's wrong, or just close this if it looks right.`
      : 'Controls the local half of every time shown alongside UTC across the admin UI — the header clock, and every dual-time display in Dashboard, Schedule, Gantt, Post Log, and Announcements.';
  }
  // Full IANA database (~400 zones) in a native <select> rather than the
  // old curated 10-button list — a native select supports type-to-jump,
  // so this needs no separate search box to stay usable at this size.
  const zones = allTimeZones();
  const options = zones.includes(current) ? zones : [current, ...zones];
  list.innerHTML = `<select class="pf-v6-c-form-control" id="timezoneSelect" style="width:100%" onchange="selectTimezone(this.value)">`
    + options.map(tz => `<option value="${tz}" ${tz === current ? 'selected' : ''}>${tz}</option>`).join('')
    + `</select>`;
  document.getElementById('timezoneModal').classList.add('open');
}

// Called once from init.js after the page is otherwise ready — a stored
// samaya_display_tz means either an explicit past choice or a past
// confirm/dismiss of this very modal (selectTimezone/closeTimezoneModal
// both set it), so its mere presence is "already asked."
function maybeShowFirstVisitTzModal() {
  if (localStorage.getItem('samaya_display_tz')) return;
  openTimezoneModal(/*firstVisit=*/true);
}

function closeTimezoneModal() {
  // Closing without picking a zone still counts as "confirmed" — this is
  // what stops the first-visit prompt from reappearing on every future
  // visit even if the detected default was left as-is (see
  // maybeShowFirstVisitTzModal above, which only checks presence).
  if (!localStorage.getItem('samaya_display_tz')) setDisplayTz(getDisplayTz());
  document.getElementById('timezoneModal').classList.remove('open');
}

function selectTimezone(tz) {
  setDisplayTz(tz);
  closeTimezoneModal();
  startHeaderClock();
  // Reload whichever view is currently open so its times re-render
  // under the new zone — same set of views that read getDisplayTz()
  // through fmtTime/fmtTimeShort/fmtDateTime.
  const activeView = document.querySelector('.view.active');
  if (!activeView) return;
  const id = activeView.id.replace('v-', '');
  if (id === 'dashboard')     loadDashboard();
  if (id === 'schedule')      { renderSchedule(); renderGantt(occurrenceData); }
  if (id === 'postlog')       loadPostLog();
  if (id === 'announcements') loadAnnouncements();
}

function showView(id, btn) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.querySelectorAll('.pf-v6-c-tabs__item').forEach(li => li.classList.remove('pf-m-current'));
  document.getElementById('v-' + id).classList.add('active');
  btn.closest('.pf-v6-c-tabs__item').classList.add('pf-m-current');
  if (id === 'dashboard') loadDashboard();
  if (id === 'events')    loadEvents();
  if (id === 'schedule')  { setScheduleLayout(localStorage.getItem('samaya_schedule_layout') || 'table'); loadSchedule(); }
  if (id === 'postlog')   loadPostLog();
  if (id === 'sync')      loadSync();
  if (id === 'config')    loadDiscordConfig();
  // Spec §38.6 — Access & Platform merged into one tab/view ('access');
  // loadAccess() now also renders the Platform section for a superadmin.
  if (id === 'access')    loadAccess();
  if (id === 'audit')     loadAuditLog();
  if (id === 'announcements') { loadAnnouncements(); loadAnnouncementTemplates(); }
}

// tenantOverride sends one explicit tenant slug regardless of any tab
// filter's current value — every write endpoint (create/edit an event,
// create an announcement, a Sync "push fix" row, ...) requires exactly
// one real tenant, never the combined '*' pseudo-tenant, so callers
// always pass an explicit slug here rather than relying on a global
// "current tenant" (removed — see the §38.1 comment above TENANTS).
async function api(method, path, body, skipTenantHeader, tenantOverride) {
  const headers = {'Content-Type':'application/json'};
  if (!skipTenantHeader) {
    if (tenantOverride) headers['X-Tenant-Slug'] = tenantOverride;
  }
  const opts = { method, headers, credentials: 'same-origin' };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch('/admin' + path, opts);
  if (res.status === 401) {
    window.location.href = '/auth/login';
    throw new Error('Not logged in — redirecting to login.');
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({detail: res.statusText}));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

function toast(msg, err) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.background = err ? '#991b1b' : 'var(--banner)';
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3500);
}

// ── Remembered last-used Discord channel per tenant (spec §29) ──────
// Coordinators post to the same channel for a given alliance almost every
// time — a brand-new target row (Events or Announcements) defaults to
// whatever channel was last picked for that tenant, rather than forcing
// a fresh "— none —" pick on every single row. Per-browser only
// (localStorage), never sent to the server, and never overrides an
// explicit channelId passed in for an edit/duplicate — callers only
// consult this when there's no real value to prefill with.
const LAST_CHANNEL_KEY = 'samaya_last_channels';

function getLastChannelForTenant(tenantSlug) {
  try {
    const map = JSON.parse(localStorage.getItem(LAST_CHANNEL_KEY) || '{}');
    return map[tenantSlug] || null;
  } catch (e) { return null; }
}

function setLastChannelForTenant(tenantSlug, channelId) {
  if (!tenantSlug || !channelId) return;
  try {
    const map = JSON.parse(localStorage.getItem(LAST_CHANNEL_KEY) || '{}');
    map[tenantSlug] = channelId;
    localStorage.setItem(LAST_CHANNEL_KEY, JSON.stringify(map));
  } catch (e) { /* best-effort convenience only */ }
}

// ── Relative time ("in 20 minutes" / "3 hours ago") (spec §29) ──────
// Shared by the Schedule/Dashboard occurrence badges and (via a thin
// wrapper) the announcement composer's live preview (§28) — one
// implementation rather than two copies of the same threshold math.
function formatRelativeTime(date) {
  const diffSeconds = Math.round((date.getTime() - Date.now()) / 1000);
  const abs = Math.abs(diffSeconds);
  const units = [['day', 86400], ['hour', 3600], ['minute', 60], ['second', 1]];
  for (const [name, secs] of units) {
    if (abs >= secs || name === 'second') {
      const count = Math.max(1, Math.round(abs / secs));
      const plural = count === 1 ? name : name + 's';
      return diffSeconds >= 0 ? `in ${count} ${plural}` : `${count} ${plural} ago`;
    }
  }
}

// ── Emoji picker (spec §28) ──────────────────────────────────
// Discord's *default* (standard Unicode) emoji set only — never a
// server's custom/uploaded emoji, which would need a per-guild fetch and
// image assets this app has no other use for. Browsers render these
// glyphs natively, the same glyphs Discord's own client shows for
// non-custom emoji, so a curated static list is all this needs.
const EMOJI_PICKER_LIST = {
  'Faces': ['😀','😁','😂','🤣','😊','😇','🙂','😉','😍','🤩','😎','🤔','😐','😴','😭','😡','🤯','🥳','😅','🤗'],
  'Gestures': ['👍','👎','👏','🙌','🤝','🙏','💪','✌️','🤞','👋','🫡','🤙','👀','🖐️','☝️'],
  'Symbols': ['🔥','⭐','✨','💯','⚔️','🛡️','🏆','⚠️','✅','❌','❗','❓','⏰','📅','📢','🔔','💀','👑','🎉','🚨'],
};

// Inserts `text` at the current cursor position of the textarea with the
// given id (replacing any selection), fires `input` so char counts and
// the live preview update, and restores focus with the cursor placed
// right after the inserted text.
function insertAtCursor(textareaId, text) {
  const ta = document.getElementById(textareaId);
  if (!ta) return;
  const start = ta.selectionStart;
  const end = ta.selectionEnd;
  ta.value = ta.value.slice(0, start) + text + ta.value.slice(end);
  const cursor = start + text.length;
  ta.focus();
  ta.setSelectionRange(cursor, cursor);
  ta.dispatchEvent(new Event('input', { bubbles: true }));
}

// One shared popover, repositioned/retargeted per call rather than one
// per textarea — matches the "one modal, refilled" convention used
// elsewhere in this app rather than duplicating markup.
function toggleEmojiPicker(buttonEl, textareaId) {
  let picker = document.getElementById('emojiPicker');
  const alreadyOpenForThis = picker && picker.classList.contains('open') && picker.dataset.targetTextarea === textareaId;
  if (!picker) {
    picker = document.createElement('div');
    picker.id = 'emojiPicker';
    picker.className = 'emoji-picker';
    document.body.appendChild(picker);
    document.addEventListener('click', (e) => {
      if (!picker.contains(e.target) && !e.target.classList.contains('emoji-picker-btn')) {
        picker.classList.remove('open');
      }
    });
  }
  if (alreadyOpenForThis) {
    picker.classList.remove('open');
    return;
  }
  picker.dataset.targetTextarea = textareaId;
  picker.innerHTML = Object.entries(EMOJI_PICKER_LIST).map(([heading, emojis]) =>
    '<div class="emoji-picker-heading">' + heading + '</div>'
    + '<div class="emoji-picker-grid">'
    + emojis.map(e => '<button type="button" class="emoji-picker-item" onclick="insertAtCursor(\'' + textareaId + '\',\'' + e + '\')">' + e + '</button>').join('')
    + '</div>'
  ).join('');
  const rect = buttonEl.getBoundingClientRect();
  picker.style.top = (rect.bottom + window.scrollY + 4) + 'px';
  picker.style.left = (rect.left + window.scrollX) + 'px';
  picker.classList.add('open');
}

// ── "Preview as it would look on Discord" modal (spec §46) ──────────
// Click-to-preview for an event/announcement row/card across Dashboard,
// Schedule, Events, and Announcements — reuses the exact rendering
// announcements.js's composer preview already built (renderDiscordMarkdownPreview/
// clientRenderPlaceholders/formatDiscordAbsolutePreview/formatDiscordRelativePreview,
// all defined there — safe to call from here since every admin script has
// already loaded and defined its top-level functions by the time a user
// can click anything). `kind` is 'occurrence' (Dashboard/Schedule row —
// an Occurrence dict), 'eventdef' (Events tab row — an EventDefinition
// dict, previewed against its own anchor_date/start_time_utc since it has
// no concrete "next occurrence" the way a real Occurrence does), or
// 'announcement' (an Announcement dict, ANNOUNCEMENTS-shaped).
// Row-level click target for the tables/cards below — lets the whole
// row/card open the preview while a click on an actual control inside it
// (Post, Cancel, Edit, a checkbox, a <select>...) keeps doing what it
// already does instead of also popping the preview open underneath it.
function handleRowPreviewClick(evt, kind, item) {
  if (evt.target.closest('button, a, select, input, .pf-v6-c-check')) return;
  openDiscordPreview(kind, item);
}

async function openDiscordPreview(kind, item) {
  const modal = document.getElementById('discordPreviewModal');
  const pane = document.getElementById('discordPreviewModalBody');
  if (!modal || !pane) return;
  pane.innerHTML = '<p style="color:var(--muted)">Loading preview…</p>';
  modal.classList.add('open');

  let tenantSlug, title, rawBody, scheduledFor, eventOffsetMinutes, coverImage, channelLabel, durationHours;
  const isAnnouncement = kind === 'announcement';

  if (kind === 'occurrence') {
    tenantSlug = tenantSlugFor(item.owning_tenant_id);
    title = item.event_name;
    rawBody = item.description || '';
    scheduledFor = new Date();
    eventOffsetMinutes = Math.round((new Date(item.start_datetime_utc).getTime() - Date.now()) / 60000);
    coverImage = item.cover_image_data || null;
    channelLabel = item.discord_channel;
    durationHours = item.duration_hours;
  } else if (kind === 'eventdef') {
    tenantSlug = tenantSlugFor(item.owning_tenant_id);
    title = item.name;
    rawBody = item.description || '';
    scheduledFor = new Date();
    const start = new Date(item.anchor_date + 'T' + (item.start_time_utc || '00:00') + ':00Z');
    eventOffsetMinutes = Math.round((start.getTime() - Date.now()) / 60000);
    coverImage = item.cover_image_data || null;
    channelLabel = item.discord_channel;
    durationHours = item.duration_hours;
  } else {
    tenantSlug = item.owning_tenant_slug;
    title = item.title;
    rawBody = item.body_markdown || '';
    scheduledFor = new Date(item.scheduled_for);
    eventOffsetMinutes = item.event_offset_minutes || 0;
  }

  const tenant = TENANTS.find(t => t.slug === tenantSlug);
  const allianceName = tenant ? tenant.name : (tenantSlug || '');

  const kingdomNames = await ensureKingdomNamesLoaded();
  const [roles, channels] = await Promise.all([
    ensurePreviewRolesLoaded(tenantSlug),
    ensurePreviewChannelsLoaded(tenantSlug),
  ]);

  const resolved = clientRenderPlaceholders(rawBody, {
    allianceName, kingdomName: tenant ? kingdomNames[tenant.kingdom_id] : '',
    scheduledFor, eventOffsetMinutes,
  });
  const html = renderDiscordMarkdownPreview(resolved, { roles, channels });

  if (isAnnouncement) {
    pane.innerHTML = `
      <div class="discord-preview-msg">
        <div class="discord-preview-avatar">S</div>
        <div class="discord-preview-body">
          <div class="discord-preview-header">
            <span class="discord-preview-name">Samaya</span><span class="discord-preview-bot-tag">BOT</span>
            <span style="color:#949ba4;font-size:0.75em">${formatDiscordAbsolutePreview(scheduledFor)}</span>
          </div>
          <div class="discord-preview-text">${html}</div>
        </div>
      </div>`;
  } else {
    const eventStart = new Date(scheduledFor.getTime() + eventOffsetMinutes * 60000);
    pane.innerHTML = `
      <div class="discord-preview-event">
        ${coverImage ? `<img class="discord-preview-event-cover" src="${coverImage}" alt="">` : ''}
        <div class="discord-preview-event-body">
          <div class="discord-preview-event-title">${escapeHtml(title)}</div>
          <div class="discord-preview-event-time">🗓️ ${formatDiscordAbsolutePreview(eventStart)} <span style="color:#949ba4">(${formatDiscordRelativePreview(eventStart)})</span></div>
          <div class="discord-preview-event-meta">${durationHours ? '⏱ ' + durationHours + 'h' : ''}${channelLabel ? (durationHours ? ' · ' : '') + '💬 ' + escapeHtml(channelLabel) : ''}</div>
          <div class="discord-preview-text discord-preview-event-desc">${html}</div>
          <button type="button" class="discord-preview-event-interested" disabled>✓ Interested</button>
        </div>
      </div>`;
  }
}

function closeDiscordPreviewModal() {
  document.getElementById('discordPreviewModal').classList.remove('open');
}

// ── Notification Targets column (spec §49) ─────────────────────────
// The Alliance Events/Leadership Notifications tables (events.js) and the
// Announcements table (announcements.js) all show a "Notification
// Targets" column naming the actual Discord channel(s)/role(s) a row
// posts/pings to. The channel/role fields involved are real Discord
// snowflake IDs, not names — resolving them requires a per-tenant API
// call, so each cell first renders with the bare ID (all that's known
// synchronously) inside a `data-notif-channel="tenantSlug:id"` /
// `data-notif-role="tenantSlug:id"` span, and enhanceNotificationTargetLabels()
// upgrades every such span to a real "#name"/"@name" label afterward —
// one batched fetch per distinct tenant rather than one per row/target.
const _notifChannelNameCache = {};
const _notifRoleNameCache = {};

async function _loadNotifChannelMap(tenantSlug) {
  if (_notifChannelNameCache[tenantSlug]) return _notifChannelNameCache[tenantSlug];
  try {
    const channels = await api('GET', '/api/discord/channels', null, false, tenantSlug);
    const map = {};
    channels.forEach(c => { map[c.id] = c.name; });
    _notifChannelNameCache[tenantSlug] = map;
    return map;
  } catch (e) {
    return {};
  }
}

async function _loadNotifRoleMap(tenantSlug) {
  if (_notifRoleNameCache[tenantSlug]) return _notifRoleNameCache[tenantSlug];
  try {
    const roles = await api('GET', '/api/discord/roles', null, false, tenantSlug);
    const map = {};
    roles.forEach(r => { map[r.id] = r.name; });
    _notifRoleNameCache[tenantSlug] = map;
    return map;
  } catch (e) {
    return {};
  }
}

async function enhanceNotificationTargetLabels(root) {
  const scope = root || document;
  const chanEls = Array.from(scope.querySelectorAll('[data-notif-channel]'));
  const roleEls = Array.from(scope.querySelectorAll('[data-notif-role]'));
  const slugs = new Set(
    chanEls.map(el => el.dataset.notifChannel.split(':')[0])
      .concat(roleEls.map(el => el.dataset.notifRole.split(':')[0]))
  );
  for (const slug of slugs) {
    if (!slug) continue;
    const [channelMap, roleMap] = await Promise.all([_loadNotifChannelMap(slug), _loadNotifRoleMap(slug)]);
    chanEls
      .filter(el => el.dataset.notifChannel.startsWith(slug + ':'))
      .forEach(el => {
        const id = el.dataset.notifChannel.slice(slug.length + 1);
        if (channelMap[id]) el.textContent = '#' + channelMap[id];
      });
    roleEls
      .filter(el => el.dataset.notifRole.startsWith(slug + ':'))
      .forEach(el => {
        const id = el.dataset.notifRole.slice(slug.length + 1);
        if (roleMap[id]) el.textContent = '@' + roleMap[id];
      });
  }
}
