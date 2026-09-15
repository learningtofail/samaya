// Shared helpers loaded before every other admin view script (dashboard.js,
// events.js, schedule.js, gantt.js, postlog.js, config.js, sync.js). Classic
// <script> tags, not ES modules, so the functions and consts declared here
// are reachable as plain globals from every file loaded after this one —
// see the <script src> order in admin.html.

// ── Admin key (defense-in-depth behind Cloudflare Access) ──────
// Stored in this browser's localStorage only; sent as X-Admin-Key on
// every /admin/api/* call. A wrong or missing key gets a 401 from
// the server, which prompts again below.
function getAdminKey() {
  let key = localStorage.getItem('samaya_admin_key');
  if (!key) {
    key = prompt('Enter the Samaya admin key:') || '';
    if (key) localStorage.setItem('samaya_admin_key', key);
  }
  return key;
}
function clearAdminKey() {
  localStorage.removeItem('samaya_admin_key');
}

// ── Tenant selection ─────────────────────────────────────────
// Which alliance's data every /admin/api/* call operates on, sent as
// X-Tenant-Slug (see routers/admin/deps.py.get_current_tenant). A
// bridge-period mechanism: once real per-user sessions exist, this
// becomes derived from login instead of a manually-picked value stored
// in this browser only.
let TENANTS = [];               // populated by loadTenants(), used for the
                                 // picker and for TENANT_COLORS below
const TENANT_COLORS = {};       // tenant.id -> tenant.color, replaces the
                                 // old hardcoded ALLIANCE_COLORS constant

function getCurrentTenantSlug() {
  return localStorage.getItem('samaya_tenant_slug') || '';
}
function setCurrentTenantSlug(slug) {
  localStorage.setItem('samaya_tenant_slug', slug);
}

async function loadTenants() {
  TENANTS = await api('GET', '/api/tenants', null, /*skipTenantHeader=*/true);
  TENANTS.forEach(t => { TENANT_COLORS[t.id] = t.color; });
  const picker = document.getElementById('tenantPicker');
  if (!picker) return;
  picker.innerHTML = TENANTS.map(t =>
    `<option value="${escapeHtml(t.slug)}">${escapeHtml(t.name)}</option>`
  ).join('');
  const current = getCurrentTenantSlug();
  if (current && TENANTS.some(t => t.slug === current)) {
    picker.value = current;
  } else if (TENANTS.length) {
    setCurrentTenantSlug(TENANTS[0].slug);
    picker.value = TENANTS[0].slug;
  }
}

function onTenantChange(slug) {
  setCurrentTenantSlug(slug);
  // Reload whichever view is currently open under the new tenant.
  const activeView = document.querySelector('.view.active');
  if (activeView) showView(activeView.id.replace('v-', ''), document.querySelector('nav button.active'));
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
let configDirty = false;

function markConfigDirty() { configDirty = true; }

function showView(id, btn) {
  const activeView = document.querySelector('.view.active');
  const current = activeView ? activeView.id.replace('v-', '') : null;
  if (current === 'config' && id !== 'config' && configDirty) {
    if (!confirm('You have unsaved Discord config changes. Leave anyway?')) return;
    configDirty = false;
  }
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
  document.getElementById('v-' + id).classList.add('active');
  btn.classList.add('active');
  if (id === 'dashboard') loadDashboard();
  if (id === 'events')    loadEvents();
  if (id === 'schedule')  loadSchedule();
  if (id === 'gantt')     loadGantt();
  if (id === 'postlog')   loadPostLog();
  if (id === 'sync')      loadSync();
  if (id === 'config')    renderThemeSwatches();
}

async function api(method, path, body, skipTenantHeader) {
  const headers = {'Content-Type':'application/json', 'X-Admin-Key': getAdminKey()};
  if (!skipTenantHeader) {
    const slug = getCurrentTenantSlug();
    if (slug) headers['X-Tenant-Slug'] = slug;
  }
  const opts = { method, headers };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch('/admin' + path, opts);
  if (res.status === 401) {
    clearAdminKey();
    throw new Error('Admin key rejected — reload the page and re-enter it.');
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

