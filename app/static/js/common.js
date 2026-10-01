// Shared helpers loaded before every other admin script. Classic <script>
// tags, not ES modules, so everything declared at the top level here is a
// plain global for the files loaded after it (see the <script src> order in
// admin.html, which is the dependency order).
//
// Spec §66.7: the console has seven tabs (Events, Schedule, Delivery log,
// Event types, Feedback, Setup, Audit log). Each tab's script registers its
// own loader in VIEW_LOADERS; showView() below calls it when the tab opens.

// ── Current user ─────────────────────────────────────────────
let ME = null;

// view id -> function that (re)loads and renders that tab. Filled in by each
// tab's own script at load time.
const VIEW_LOADERS = {};

// ── Modals ───────────────────────────────────────────────────
// Every modal backdrop names its own close function in data-close-fn. One
// listener per interaction (Escape, backdrop click, dismiss button) calls it,
// so no modal needs its own wiring for these.
function closeFnOf(backdrop) {
  const fnName = backdrop && backdrop.dataset.closeFn;
  return fnName && typeof window[fnName] === 'function' ? window[fnName] : null;
}

document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  const open = document.querySelectorAll('.pf-v6-c-backdrop.open');
  if (!open.length) return;
  const fn = closeFnOf(open[open.length - 1]);
  if (fn) fn();
});

document.querySelectorAll('.pf-v6-c-backdrop').forEach((backdrop) => {
  backdrop.addEventListener('click', (e) => {
    if (e.target !== backdrop) return;
    const fn = closeFnOf(backdrop);
    if (fn) fn();
  });
});

document.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-modal-dismiss]');
  if (!btn) return;
  const fn = closeFnOf(btn.closest('.pf-v6-c-backdrop'));
  if (fn) fn();
});

// Remembers what had focus when a modal opened and gives it back on close.
// Modals can stack (the occurrence picker opens over nothing, but the scope
// chooser hands off to the event form), so this is a stack, not one slot.
const _modalReturnFocus = [];

function focusModal(modalEl) {
  if (!modalEl) return;
  _modalReturnFocus.push(document.activeElement);
  const focusable = modalEl.querySelector('select, textarea, input:not([type="hidden"]), button:not(.samaya-modal-close)');
  if (focusable) focusable.focus();
}

function unfocusModal() {
  const el = _modalReturnFocus.pop();
  if (el && typeof el.focus === 'function' && document.contains(el)) el.focus();
}

// Open/close a modal by id with the shared focus handling. Each modal's own
// close<Name>() function (named in data-close-fn) calls closeModalById().
function openModalById(id) {
  const modal = document.getElementById(id);
  if (!modal) return;
  modal.classList.add('open');
  focusModal(modal);
}

function closeModalById(id) {
  const modal = document.getElementById(id);
  if (!modal || !modal.classList.contains('open')) return;
  modal.classList.remove('open');
  unfocusModal();
}

// ── Delegated row actions ────────────────────────────────────
// Per-row buttons built in template strings carry data-action="name" and a
// data-* payload; one listener on the container maps the name to a handler.
function bindActions(root, handlers) {
  if (!root) return;
  root.addEventListener('click', (e) => {
    const el = e.target.closest('[data-action]');
    if (!el || !root.contains(el) || el.disabled) return;
    const fn = handlers[el.dataset.action];
    if (fn) fn(el, e);
  });
}

// ── API ──────────────────────────────────────────────────────
// tenantOverride sends one explicit X-Tenant-Slug (a real slug, or '*' for the
// combined read-only view on endpoints that accept it). skipTenantHeader is
// kept for the calls that need no tenant at all.
async function api(method, path, body, skipTenantHeader, tenantOverride) {
  const headers = { 'Content-Type': 'application/json' };
  if (!skipTenantHeader && tenantOverride) headers['X-Tenant-Slug'] = tenantOverride;
  const opts = { method, headers, credentials: 'same-origin' };
  if (body !== undefined && body !== null) opts.body = JSON.stringify(body);
  const res = await fetch('/admin' + path, opts);
  if (res.status === 401) {
    window.location.href = '/auth/login';
    throw new Error('Not logged in. Redirecting to login.');
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(describeApiError(err, res.statusText));
  }
  if (res.status === 204) return null;
  return res.json();
}

// FastAPI returns `detail` as a string for HTTPExceptions and as a list of
// {loc, msg} objects for request-validation failures.
function describeApiError(err, fallback) {
  const d = err && err.detail;
  if (Array.isArray(d)) {
    return d.map((x) => (x && x.msg ? x.msg : String(x))).join('; ') || fallback;
  }
  return d || fallback;
}

// ── Toasts ───────────────────────────────────────────────────
// kind: falsy = success, true or 'error' = error, 'warn' = warning (the
// action worked but something needs attention, e.g. discord_errors).
let _toastTimer = null;

function toast(msg, kind) {
  const t = document.getElementById('toast');
  if (!t) return;
  t.textContent = msg;
  t.classList.remove('toast--error', 'toast--warn');
  if (kind === true || kind === 'error') t.classList.add('toast--error');
  else if (kind === 'warn') t.classList.add('toast--warn');
  t.classList.add('show');
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => t.classList.remove('show'), kind ? 7000 : 3500);
}

// Shows the Discord problems a save reported while still treating the save
// itself as done.
function toastDiscordErrors(errors, doneMessage) {
  if (errors && errors.length) {
    toast(doneMessage + ' But Discord reported: ' + errors.join('; '), 'warn');
  } else {
    toast(doneMessage);
  }
}

// ── HTML helpers ─────────────────────────────────────────────
function byId(id) {
  return document.getElementById(id);
}

// Every server-provided string interpolated into innerHTML goes through this.
function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

// A PatternFly label. `text` is plain text and is escaped here.
function pfLabel(text, color) {
  return `<span class="pf-v6-c-label ${color} pf-m-filled"><span class="pf-v6-c-label__content"><span class="pf-v6-c-label__text">${escapeHtml(text)}</span></span></span>`;
}

// <option> list. options: [{value, label}], selected: value to mark.
function optionsHtml(options, selected) {
  return options.map((o) =>
    `<option value="${escapeHtml(o.value)}"${String(o.value) === String(selected) ? ' selected' : ''}>${escapeHtml(o.label)}</option>`
  ).join('');
}

function emptyRow(colspan, message) {
  return `<tr class="pf-v6-c-table__tr"><td class="pf-v6-c-table__td samaya-empty" colspan="${colspan}">${escapeHtml(message)}</td></tr>`;
}

// Event type color chip. The hex comes from the server and is validated
// there, but it is still escaped and shown through a data attribute that CSS
// cannot read, so it is applied with the CSSOM after render (see
// applyTypeColors) rather than an inline style attribute.
function typeChip(type) {
  const name = type ? type.name : '?';
  const color = type ? type.color : '#475569';
  return `<span class="type-chip"><span class="type-chip__dot" data-color="${escapeHtml(color)}"></span>${escapeHtml(name)}</span>`;
}

function applyTypeColors(root) {
  const scope = root || document;
  scope.querySelectorAll('[data-color]').forEach((node) => {
    if (/^#[0-9a-fA-F]{6}$/.test(node.dataset.color)) node.style.backgroundColor = node.dataset.color;
  });
  scope.querySelectorAll('[data-accent]').forEach((node) => {
    if (/^#[0-9a-fA-F]{6}$/.test(node.dataset.accent)) node.style.borderLeftColor = node.dataset.accent;
  });
}

// ── Roles ────────────────────────────────────────────────────
async function loadMe() {
  ME = await api('GET', '/api/me', null, /*skipTenantHeader=*/true);
  return ME;
}

function isSuperadmin() {
  return !!(ME && ME.is_superadmin);
}

function roleForTenant(tenantId) {
  return ME && ME.tenant_roles ? ME.tenant_roles[tenantId] : undefined;
}

// A viewer can read everything and change nothing (spec §31.3). The server
// enforces this on every write; the UI only hides what would be refused.
function canWriteTenant(tenant) {
  if (!tenant) return false;
  if (isSuperadmin()) return true;
  const role = roleForTenant(tenant.id);
  return !!role && role !== 'viewer';
}

function writableTenants() {
  return TENANTS.filter(canWriteTenant);
}

function canWriteAnywhere() {
  return writableTenants().length > 0;
}

function isOwnerOfTenant(tenant) {
  return !!tenant && (isSuperadmin() || roleForTenant(tenant.id) === 'owner');
}

// ── Tenants and per-tab alliance filters ─────────────────────
let TENANTS = [];
const TENANT_ICONS = {};

// '*' is the combined read-only pseudo-alliance, sent as-is in
// X-Tenant-Slug on list endpoints that accept it.
const COMBINED_SLUG = '*';

function getTabFilter(tab) {
  let saved = null;
  try { saved = localStorage.getItem('samaya_filter_' + tab); } catch { /* storage blocked */ }
  if (saved && saved !== COMBINED_SLUG && !TENANTS.some((t) => t.slug === saved)) return COMBINED_SLUG;
  return saved || COMBINED_SLUG;
}

function setTabFilter(tab, slug) {
  try { localStorage.setItem('samaya_filter_' + tab, slug); } catch { /* storage blocked */ }
}

async function loadTenants() {
  TENANTS = await api('GET', '/api/tenants', null, /*skipTenantHeader=*/true);
  TENANTS.forEach((t) => { TENANT_ICONS[t.slug] = t.icon_image_data || null; });
}

function tenantById(id) {
  return TENANTS.find((t) => t.id === id) || null;
}

function tenantBySlug(slug) {
  return TENANTS.find((t) => t.slug === slug) || null;
}

function tenantName(id) {
  const t = tenantById(id);
  return t ? t.name : '#' + id;
}

// Fills an "Alliance" filter <select>: every accessible alliance, plus "All"
// unless allowAll is false. onChange runs after the choice is stored.
function renderAllianceFilterSelect(selectId, tab, onChange, allowAll) {
  const select = document.getElementById(selectId);
  if (!select) return;
  const useAll = allowAll !== false;
  let current = getTabFilter(tab);
  if (!useAll && current === COMBINED_SLUG) current = TENANTS[0] ? TENANTS[0].slug : '';
  const options = TENANTS.map((t) => ({ value: t.slug, label: t.name }));
  if (useAll) options.unshift({ value: COMBINED_SLUG, label: 'All alliances' });
  select.innerHTML = optionsHtml(options, current);
  select.onchange = () => { setTabFilter(tab, select.value); onChange(); };
}

// The alliance a tab should act as for single-alliance reads (event types,
// feedback): its filter, or the first accessible alliance for "All".
function singleSlugFor(tab) {
  const f = getTabFilter(tab);
  if (f !== COMBINED_SLUG) return f;
  return TENANTS[0] ? TENANTS[0].slug : '';
}

// The alliance a write to an event must be sent as: the event's owner when the
// caller can see it, otherwise (a kingdom-wide event owned elsewhere in the
// Kingdom) the first alliance the caller can write for.
function writeSlugForEvent(event) {
  const owner = tenantById(event.owning_tenant_id);
  if (owner) return owner.slug;
  const any = writableTenants()[0] || TENANTS[0];
  return any ? any.slug : '';
}

// ── Tabs ─────────────────────────────────────────────────────
function showView(id) {
  const view = document.getElementById('v-' + id);
  const btn = document.querySelector('.tabs__link[data-view="' + id + '"]');
  if (!view || !btn) return;
  document.querySelectorAll('.view').forEach((v) => v.classList.remove('active'));
  document.querySelectorAll('.tabs__link').forEach((b) => b.removeAttribute('aria-current'));
  view.classList.add('active');
  btn.setAttribute('aria-current', 'page');
  try { history.replaceState(null, '', '#' + id); } catch { /* not fatal */ }
  const loader = VIEW_LOADERS[id];
  if (loader) loader();
}

document.querySelector('.tabs__list')?.addEventListener('click', (e) => {
  const btn = e.target.closest('.tabs__link[data-view]');
  if (btn) showView(btn.dataset.view);
});

// Superadmin-only tabs. Setup stays visible to everyone (display name and
// notification destinations are not superadmin-only); its superadmin parts
// are hidden inside it by setup.js.
function applyRoleVisibility() {
  const auditItem = document.getElementById('auditTabItem');
  if (auditItem) auditItem.classList.toggle('hidden', !isSuperadmin());
}

// ── Display time zone ────────────────────────────────────────
const DISPLAY_TZ_OPTIONS = [
  'UTC', 'America/Toronto', 'America/New_York', 'America/Chicago',
  'America/Denver', 'America/Los_Angeles', 'Europe/London', 'Europe/Paris',
  'Asia/Singapore', 'Asia/Tokyo', 'Australia/Sydney',
];

function allTimeZones() {
  try {
    if (typeof Intl.supportedValuesOf === 'function') return Intl.supportedValuesOf('timeZone');
  } catch { /* fall through to the short list */ }
  return DISPLAY_TZ_OPTIONS;
}

function getDisplayTz() {
  let saved = null;
  try { saved = localStorage.getItem('samaya_display_tz'); } catch { /* storage blocked */ }
  return saved || Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
}

function setDisplayTz(tz) {
  try { localStorage.setItem('samaya_display_tz', tz); } catch { /* storage blocked */ }
}

let _headerClockTimer = null;

function formatHeaderClock() {
  const now = new Date();
  const utcStr = now.toLocaleTimeString('en-GB', { timeZone: 'UTC', hour: '2-digit', minute: '2-digit' });
  const tz = getDisplayTz();
  if (tz === 'UTC') return `${utcStr} UTC`;
  try {
    const localStr = now.toLocaleTimeString('en-GB', { timeZone: tz, hour: '2-digit', minute: '2-digit' });
    return `${utcStr} UTC · ${localStr} (${tz})`;
  } catch {
    return `${utcStr} UTC`;
  }
}

function startHeaderClock() {
  const el = document.getElementById('headerClock');
  if (!el) return;
  const tick = () => { el.textContent = formatHeaderClock(); };
  tick();
  if (_headerClockTimer) clearInterval(_headerClockTimer);
  _headerClockTimer = setInterval(tick, 30000);
}

document.getElementById('headerClock')?.addEventListener('click', () => openTimezoneModal(false));

function openTimezoneModal(firstVisit) {
  const list = document.getElementById('timezoneModalList');
  const intro = document.getElementById('timezoneModalIntro');
  const current = getDisplayTz();
  if (intro) {
    intro.textContent = firstVisit
      ? `Times are shown in UTC and in your own time zone so nothing is missed across zones. ${current} was detected from your browser. Pick another below if that is wrong, or close this if it looks right.`
      : 'Sets the local half of every time shown next to UTC in this console, including the header clock.';
  }
  const zones = allTimeZones();
  const options = (zones.includes(current) ? zones : [current, ...zones]).map((tz) => ({ value: tz, label: tz }));
  list.innerHTML = `<label class="pf-v6-c-form__label" for="timezoneSelect"><span class="pf-v6-c-form__label-text">Time zone</span></label>`
    + `<select class="pf-v6-c-form-control" id="timezoneSelect">${optionsHtml(options, current)}</select>`;
  document.getElementById('timezoneSelect').addEventListener('change', (e) => selectTimezone(e.target.value));
  openModalById('timezoneModal');
}

function maybeShowFirstVisitTzModal() {
  try {
    if (localStorage.getItem('samaya_display_tz')) return;
  } catch {
    return;
  }
  openTimezoneModal(true);
}

function closeTimezoneModal() {
  let saved = null;
  try { saved = localStorage.getItem('samaya_display_tz'); } catch { /* storage blocked */ }
  if (!saved) setDisplayTz(getDisplayTz());
  closeModalById('timezoneModal');
}

function selectTimezone(tz) {
  setDisplayTz(tz);
  closeTimezoneModal();
  startHeaderClock();
  const active = document.querySelector('.view.active');
  const loader = active && VIEW_LOADERS[active.id.replace('v-', '')];
  if (loader) loader();
}

// ── Time formatting ──────────────────────────────────────────
function toUtcDate(isoStr) {
  const hasTz = /Z$|[+-]\d{2}:\d{2}$/.test(isoStr);
  return new Date(hasTz ? isoStr : isoStr + 'Z');
}

// "19:00 UTC · 3:00 PM EDT"; collapses to UTC alone when the display zone is UTC.
function dualTimeString(utcIso) {
  const d = toUtcDate(utcIso);
  const utcPart = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC' }).format(d) + ' UTC';
  const tz = getDisplayTz();
  if (tz === 'UTC') return utcPart;
  const localPart = new Intl.DateTimeFormat('en-US', { hour: 'numeric', minute: '2-digit', hour12: true, timeZoneName: 'short', timeZone: tz }).format(d);
  return utcPart + ' · ' + localPart;
}

// "2026-10-02 19:00 UTC", plus the local time and, only when it differs from
// the UTC date, the local date.
function fmtDateTime(utcIso) {
  const d = toUtcDate(utcIso);
  const p = new Intl.DateTimeFormat('en-CA', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC',
  }).formatToParts(d).reduce((a, x) => (a[x.type] = x.value, a), {});
  const utcDateKey = `${p.year}-${p.month}-${p.day}`;
  const utcStr = `${utcDateKey} ${p.hour}:${p.minute} UTC`;
  const tz = getDisplayTz();
  if (tz === 'UTC') return utcStr;
  const lp = new Intl.DateTimeFormat('en-CA', { year: 'numeric', month: '2-digit', day: '2-digit', timeZone: tz })
    .formatToParts(d).reduce((a, x) => (a[x.type] = x.value, a), {});
  const localDateKey = `${lp.year}-${lp.month}-${lp.day}`;
  const localTimeStr = new Intl.DateTimeFormat('en-US', { hour: 'numeric', minute: '2-digit', hour12: true, timeZoneName: 'short', timeZone: tz }).format(d);
  if (localDateKey === utcDateKey) return `${utcStr} · ${localTimeStr}`;
  const localDateStr = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', timeZone: tz }).format(d);
  return `${utcStr} · ${localDateStr}, ${localTimeStr}`;
}

function utcDateKey(date) {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'UTC', year: 'numeric', month: '2-digit', day: '2-digit' }).format(date);
}

function utcTimeKey(date) {
  return new Intl.DateTimeFormat('en-GB', { timeZone: 'UTC', hour: '2-digit', minute: '2-digit', hour12: false }).format(date);
}

// "in 20 minutes" / "3 hours ago"
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
  return '';
}

// 90 -> "1 hour 30 minutes before"; 0 -> "At start".
function describeReminder(minutes) {
  if (minutes === 0) return 'At start';
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor((minutes % 1440) / 60);
  const mins = minutes % 60;
  const parts = [];
  if (days) parts.push(days + (days === 1 ? ' day' : ' days'));
  if (hours) parts.push(hours + (hours === 1 ? ' hour' : ' hours'));
  if (mins) parts.push(mins + (mins === 1 ? ' minute' : ' minutes'));
  return parts.join(' ') + ' before';
}

// Plain-language recurrence for an event dict.
function describeRecurrence(ev) {
  if (ev.recurrence_kind !== 'interval_days' || !ev.interval_days) return 'One-off';
  if (ev.interval_days === 1) return 'Every day';
  if (ev.interval_days === 7) return 'Every week';
  return `Every ${ev.interval_days} days`;
}

// ── Kingdom names (for the {kingdom_name} placeholder preview) ─
let KINGDOM_NAMES_CACHE = null;

async function ensureKingdomNamesLoaded() {
  if (KINGDOM_NAMES_CACHE) return KINGDOM_NAMES_CACHE;
  KINGDOM_NAMES_CACHE = {};
  try {
    const kingdoms = await api('GET', '/api/kingdoms', null, /*skipTenantHeader=*/true);
    kingdoms.forEach((k) => { KINGDOM_NAMES_CACHE[k.id] = k.name; });
  } catch { /* the preview just shows a blank kingdom name */ }
  return KINGDOM_NAMES_CACHE;
}

// Reads an image file as a data URI for the cover image / alliance icon
// fields. Resolves to '' (after a toast) when the file is too large or unreadable.
function readImageFile(file, maxBytes) {
  return new Promise((resolve) => {
    if (!file) { resolve(''); return; }
    if (file.size > maxBytes) {
      toast(`That image is too large (max ${Math.round(maxBytes / 1048576)}MB). Pick a smaller file.`, true);
      resolve('');
      return;
    }
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => { toast('Could not read that image file', true); resolve(''); };
    reader.readAsDataURL(file);
  });
}
