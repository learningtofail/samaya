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

async function loadMe() {
  ME = await api('GET', '/api/me', null, /*skipTenantHeader=*/true);
  return ME;
}

function isCurrentTenantOwner() {
  if (!ME) return false;
  if (ME.is_superadmin) return true;
  const tenant = TENANTS.find(t => t.slug === getCurrentTenantSlug());
  return !!tenant && ME.tenant_roles[tenant.id] === 'owner';
}

// ── Tenant selection ─────────────────────────────────────────
// Which alliance's data every /admin/api/* call operates on, sent as
// X-Tenant-Slug (see routers/admin/deps.py.get_current_tenant). The
// picker only ever lists tenants /api/me and /api/tenants already say
// this user has access to — selecting a slug outside that list isn't
// possible through the UI, and the server enforces it either way.
let TENANTS = [];               // populated by loadTenants(), used for the
                                 // picker and for TENANT_COLORS below
const TENANT_COLORS = {};       // tenant.id -> tenant.color, replaces the
                                 // old hardcoded ALLIANCE_COLORS constant

// The literal slug '*' is the combined-view pseudo-tenant (spec §14):
// sent as-is in the X-Tenant-Slug header, which deps.get_current_tenants
// on the server recognizes as "every tenant I have access to" for
// read/list endpoints. Write endpoints never receive '*' — see the
// tenantOverride param on api() below, used by the Add Event modal etc.
// to target one explicit tenant while combined mode is selected.
const COMBINED_SLUG = '*';

function getCurrentTenantSlug() {
  return localStorage.getItem('samaya_tenant_slug') || '';
}
function setCurrentTenantSlug(slug) {
  localStorage.setItem('samaya_tenant_slug', slug);
}
function isCombinedMode() {
  return getCurrentTenantSlug() === COMBINED_SLUG;
}

async function loadTenants() {
  TENANTS = await api('GET', '/api/tenants', null, /*skipTenantHeader=*/true);
  TENANTS.forEach(t => { TENANT_COLORS[t.id] = t.color; });
  const current = getCurrentTenantSlug();
  if (current === COMBINED_SLUG && TENANTS.length > 1) {
    // already valid, nothing to correct
  } else if (current && TENANTS.some(t => t.slug === current)) {
    // already valid, nothing to correct
  } else if (TENANTS.length) {
    setCurrentTenantSlug(TENANTS[0].slug);
  }
  renderTenantLink();
}

// Header's alliance control (spec §24): a text link showing the current
// selection, rather than the old <select id="tenantPicker"> — the actual
// list of choices now lives in the Switch Alliance modal (openTenantModal
// below), which is what needed a <select>'s worth of state in the first
// place.
function renderTenantLink() {
  const link = document.getElementById('tenantLink');
  if (!link) return;
  const slug = getCurrentTenantSlug();
  const tenant = TENANTS.find(t => t.slug === slug);
  const label = slug === COMBINED_SLUG ? '— All my alliances —' : (tenant ? tenant.name : (slug || 'Select alliance'));
  link.textContent = label + ' ▾';
}

function openTenantModal() {
  const list = document.getElementById('tenantModalList');
  const current = getCurrentTenantSlug();
  const options = TENANTS.map(t => ({ slug: t.slug, name: t.name }))
    .concat(TENANTS.length > 1 ? [{ slug: COMBINED_SLUG, name: '— All my alliances —' }] : []);
  list.innerHTML = options.map(o =>
    `<button type="button" class="pf-v6-c-button ${o.slug === current ? 'pf-m-primary' : 'pf-m-secondary'}" style="justify-content:flex-start" onclick="selectTenant('${escapeHtml(o.slug)}')">${escapeHtml(o.name)}</button>`
  ).join('');
  document.getElementById('tenantModal').classList.add('open');
}

function closeTenantModal() {
  document.getElementById('tenantModal').classList.remove('open');
}

function selectTenant(slug) {
  closeTenantModal();
  onTenantChange(slug);
}

// Views with no meaningful "combined" form — each is inherently tied to
// one tenant's own Discord bot/guild config or its own invite grants
// (spec §14.1). Switching into one of these while combined mode is
// selected falls back to the first real tenant instead.
// 'announcements' is here too: an announcement is authored by one tenant
// (owning_tenant_id) even though it can target several, and the admin
// API's announcements list/cancel endpoints were kept single-tenant
// (unlike events/occurrences/post-log) since combined viewing wasn't
// part of what this feature needed (spec §13 predates §14).
const SINGLE_TENANT_ONLY_VIEWS = ['config', 'sync', 'access', 'platform', 'announcements'];

function onTenantChange(slug) {
  setCurrentTenantSlug(slug);
  applyRoleVisibility();
  const activeView = document.querySelector('.view.active');
  const activeId = activeView ? activeView.id.replace('v-', '') : null;
  if (slug === COMBINED_SLUG && activeId && SINGLE_TENANT_ONLY_VIEWS.includes(activeId)) {
    setCurrentTenantSlug(TENANTS[0].slug);
    applyRoleVisibility();
  }
  renderTenantLink();
  // Reload whichever view is currently open under the (possibly just
  // corrected) tenant selection.
  const current = document.querySelector('.view.active');
  if (current) showView(current.id.replace('v-', ''), document.querySelector('.pf-v6-c-tabs__item.pf-m-current .pf-v6-c-tabs__link'));
}

// Shows/hides the Access tab (current-tenant owner only) and Platform tab
// (superadmin only) — called once after login and again on every tenant
// switch, since "am I this tenant's owner" depends on which one is selected.
// Also disables the single-tenant-only tabs (§14.1) while combined mode
// is selected, rather than hiding them outright, since they're still
// valid destinations once a real tenant is picked again.
function applyRoleVisibility() {
  const accessItem = document.getElementById('accessTabItem');
  const platformItem = document.getElementById('platformTabItem');
  if (accessItem)   accessItem.style.display = (!isCombinedMode() && isCurrentTenantOwner()) ? '' : 'none';
  if (platformItem) platformItem.style.display = (!isCombinedMode() && ME && ME.is_superadmin) ? '' : 'none';
  const configItem = document.getElementById('configTabItem');
  const syncItem = document.getElementById('syncTabItem');
  const announcementsItem = document.getElementById('announcementsTabItem');
  [configItem, syncItem, announcementsItem].forEach(item => {
    if (!item) return;
    item.classList.toggle('pf-m-disabled', isCombinedMode());
    item.querySelector('button').disabled = isCombinedMode();
  });
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
// Same IANA zone list the old mTimezone dropdown offered.
const DISPLAY_TZ_OPTIONS = [
  'UTC', 'America/Toronto', 'America/New_York', 'America/Chicago',
  'America/Denver', 'America/Los_Angeles', 'Europe/London', 'Europe/Paris',
  'Asia/Singapore', 'Asia/Tokyo', 'Australia/Sydney',
];

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

function openTimezoneModal() {
  const list = document.getElementById('timezoneModalList');
  const current = getDisplayTz();
  // The detected/stored zone can be anything in the IANA database, not
  // just one of our curated common ones — if it's not in the list,
  // prepend it rather than silently leaving it unrepresented as a choice.
  const options = DISPLAY_TZ_OPTIONS.includes(current) ? DISPLAY_TZ_OPTIONS : [current, ...DISPLAY_TZ_OPTIONS];
  list.innerHTML = options.map(tz =>
    `<button type="button" class="pf-v6-c-button ${tz === current ? 'pf-m-primary' : 'pf-m-secondary'}" style="justify-content:flex-start" onclick="selectTimezone('${tz}')">${tz}</button>`
  ).join('');
  document.getElementById('timezoneModal').classList.add('open');
}

function closeTimezoneModal() {
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
  if (id === 'schedule')      renderSchedule();
  if (id === 'gantt')         loadGantt();
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
  if (id === 'schedule')  loadSchedule();
  if (id === 'gantt')     loadGantt();
  if (id === 'postlog')   loadPostLog();
  if (id === 'sync')      loadSync();
  if (id === 'config')    loadDiscordConfig();
  if (id === 'access')    loadAccess();
  if (id === 'platform')  loadPlatform();
  if (id === 'announcements') loadAnnouncements();
}

// tenantOverride sends one explicit tenant slug regardless of the
// picker's current value — used by write actions (create/edit an event,
// create an announcement) while combined mode ('*') is selected, since
// no write endpoint accepts '*' (spec §14.3): there's always exactly one
// owning tenant for something being created, even when the list view
// showing it is combined.
async function api(method, path, body, skipTenantHeader, tenantOverride) {
  const headers = {'Content-Type':'application/json'};
  if (!skipTenantHeader) {
    const slug = tenantOverride || getCurrentTenantSlug();
    if (slug) headers['X-Tenant-Slug'] = slug;
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
