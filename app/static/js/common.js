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
  const picker = document.getElementById('tenantPicker');
  if (!picker) return;
  picker.innerHTML = TENANTS.map(t =>
    `<option value="${escapeHtml(t.slug)}">${escapeHtml(t.name)}</option>`
  ).join('') + (TENANTS.length > 1
    ? `<option value="${COMBINED_SLUG}">— All my alliances —</option>`
    : '');
  const current = getCurrentTenantSlug();
  if (current === COMBINED_SLUG && TENANTS.length > 1) {
    picker.value = COMBINED_SLUG;
  } else if (current && TENANTS.some(t => t.slug === current)) {
    picker.value = current;
  } else if (TENANTS.length) {
    setCurrentTenantSlug(TENANTS[0].slug);
    picker.value = TENANTS[0].slug;
  }
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
    document.getElementById('tenantPicker').value = TENANTS[0].slug;
    applyRoleVisibility();
  }
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
