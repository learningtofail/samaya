/* Public events page logic. Standalone (no shared JS with admin). Data contract is unchanged:
   GET /api/events | /t/{slug}/api/events, /api/alliances, /api/last-activity, /api/kingdom-branding, POST /api/tickets. */
(function () {
'use strict';

// Set by tests/frontend/events-public.test.js before this script is
// evaluated (audit remediation, Phase 4) — never true in the browser, so
// production behavior below is completely unaffected. Lets the pure,
// DOM-independent functions defined above the "Modals" section get unit
// tested directly, without a bundler and without executing this file's
// real DOM wiring/fetches against a fake page (see the guard right before
// those begin, below).
const SAMAYA_TEST = typeof globalThis !== 'undefined' && globalThis.__SAMAYA_TEST__ === true;

// ── Page mode ───────────────────────────────────────────────────
const PATH_PARTS = window.location.pathname.split('/').filter(Boolean);
const COMBINED_MODE = PATH_PARTS[0] !== 't';
const TENANT_SLUG = COMBINED_MODE ? '' : (PATH_PARTS[1] || '');
const API_URL = COMBINED_MODE ? '/api/events' : `/t/${TENANT_SLUG}/api/events`;
const ICS_URL = COMBINED_MODE ? '/ics/events.ics' : `/t/${TENANT_SLUG}/ics/events.ics`;
const ICS_ABSOLUTE_URL = window.location.origin + ICS_URL;
const ICS_WEBCAL_URL = ICS_ABSOLUTE_URL.replace(/^https?:\/\//, 'webcal://');
const DAY = 86400000, HOUR = 3600000;
const DEFAULT_KINGDOM_COLOR = 'oklch(0.82 0.13 85)';
let KINGDOM_COLOR = DEFAULT_KINGDOM_COLOR;   // replaced by the Kingdom's brand color when one is set
let KINGDOM_HEX = '';                          // the same color when it is a hex value, else ''
const DARK_INK = 'oklch(0.14 0.014 260)';

const $ = (id) => document.getElementById(id);

// ── Brand + CSSOM variable painting (no inline style attributes) ─
function paintOne(el) {
  el.dataset.vars.split(';').forEach((p) => {
    const i = p.indexOf(':');
    if (i > 0) el.style.setProperty(p.slice(0, i).trim(), p.slice(i + 1).trim());
  });
  el.removeAttribute('data-vars');
}
function lum(hex) {
  const n = parseInt(hex.slice(1), 16);
  const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
  return 0.2126 * f(n >> 16) + 0.7152 * f((n >> 8) & 255) + 0.0722 * f(n & 255);
}
function brandInk(hex) {
  const L = lum(hex);
  return 1.05 / (L + 0.05) >= (L + 0.05) / 0.05 ? '#ffffff' : '#000000';
}
function applyBrand(color) {
  const page = document.querySelector('.page');
  if (/^#[0-9a-f]{6}$/i.test(color || '')) {
    page.style.setProperty('--brand', color);
    page.style.setProperty('--brand-ink', brandInk(color));
  } else {
    page.style.removeProperty('--brand');
    page.style.removeProperty('--brand-ink');
  }
}
function currentBrandSlug() { return COMBINED_MODE ? (FILTER === 'all' ? '' : FILTER) : TENANT_SLUG; }

// ── State ───────────────────────────────────────────────────────
let ALLIANCES = [];
let EVENTS = [];          // grouped rows, each with _start/_end/_k
let FILTER = 'all';
let VIEW = localStorage.getItem('samaya_events_view') === 'calendar' ? 'calendar' : 'list';
let SITE_TITLE = '';
let CAL_YEAR = null, CAL_MONTH = null, SEL_DAY = null;
const OPEN = new Set();
const COLOR_MAP = {};
let ALL_LOADED = false;

// ── Utilities ───────────────────────────────────────────────────
function escapeHtml(v) {
  return String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function safeColor(c, fallback) {
  return /^#[0-9a-f]{3,8}$/i.test(c || '') || /^(oklch|hsl|rgb)a?\([\d.,%\s\/a-z-]+\)$/i.test(c || '') ? c : fallback;
}
function inkOn(color) { return /^#[0-9a-f]{6}$/i.test(color || '') ? brandInk(color) : DARK_INK; }
function pad2(n) { return String(n).padStart(2, '0'); }
function parseIso(iso) { return Date.parse(/Z$|[+-]\d\d:?\d\d$/.test(iso) ? iso : iso + 'Z'); }

// ── Time zone ───────────────────────────────────────────────────
const TZ_KEY = 'samaya_display_tz';
const TZ_FALLBACK = ['UTC', 'America/Toronto', 'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles', 'Europe/London', 'Europe/Paris', 'Asia/Singapore', 'Asia/Tokyo', 'Australia/Sydney'];
function allTimeZones() {
  try { if (typeof Intl.supportedValuesOf === 'function') return Intl.supportedValuesOf('timeZone'); } catch (e) { /* use fallback */ }
  return TZ_FALLBACK;
}
function getDisplayTz() { return localStorage.getItem(TZ_KEY) || Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; }
function setDisplayTz(tz) { localStorage.setItem(TZ_KEY, tz); }
function hm(ms, tz) {
  try { return new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZone: tz }).format(ms); }
  catch (e) { return new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZone: 'UTC' }).format(ms); }
}
function dayKey(ms, tz) {
  const p = {};
  new Intl.DateTimeFormat('en-CA', { year: 'numeric', month: '2-digit', day: '2-digit', timeZone: tz }).formatToParts(ms).forEach((x) => { p[x.type] = x.value; });
  return `${p.year}-${p.month}-${p.day}`;
}
function fmtDay(ms, tz, opts) { return new Intl.DateTimeFormat(undefined, Object.assign({ timeZone: tz }, opts)).format(ms); }
function tzShort(tz) { return tz.split('/').pop().replace(/_/g, ' '); }
function cd(ms) {
  ms = Math.max(0, ms);
  const s = Math.floor(ms / 1000), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), ss = s % 60;
  return h >= 48 ? `${Math.floor(h / 24)}d ${pad2(h % 24)}h` : `${pad2(h)}:${pad2(m)}:${pad2(ss)}`;
}
function rel(ms) {
  const m = Math.round(ms / 60000);
  if (m < 60) return `in ${m} min`;
  const h = Math.floor(m / 60);
  return h < 24 ? `in ${h}h${m % 60 ? ' ' + (m % 60) + 'm' : ''}` : `in ${Math.floor(h / 24)}d`;
}
function formatDuration(hours) {
  if (!hours) return '';
  if (hours === 24) return '24h (all day)';
  if (hours < 1) return `${Math.round(hours * 60)} min`;
  return hours % 24 === 0 && hours > 24 ? `${hours / 24} days` : `${hours}h`;
}

// ── Status vocabulary (same meanings as the admin console) ─────
const STATUS = {
  scheduled: { text: 'Scheduled', color: 'oklch(0.45 0.02 260)', desc: 'Set to go out, but has not been sent to Discord yet.' },
  announced: { text: 'Announced', color: 'oklch(0.48 0.13 235)', desc: 'Posted to Discord. Normal state until it starts.' },
  live:      { text: 'Live now', color: 'oklch(0.45 0.14 150)', desc: 'The event is happening right now.' },
  completed: { text: 'Completed', color: 'oklch(0.5 0.02 260)', desc: 'The event has ended.' },
  failed:    { text: 'Failed', color: 'oklch(0.5 0.19 25)', desc: 'Something went wrong sending this to Discord.' },
  cancelled: { text: 'Cancelled', color: 'oklch(0.5 0.19 25)', desc: 'Called off. It will not be posted, or was pulled after posting.' },
};
const KINDS = [
  { text: 'Event', color: 'oklch(0.5 0.12 75)', desc: 'A scheduled game event with a start time and duration.' },
  { text: 'Message', color: 'oklch(0.5 0.14 300)', desc: 'A Discord message with no duration. Not in the calendar feed.' },
  { text: 'Kingdom-wide', color: 'oklch(0.5 0.12 75)', desc: 'Every alliance in the Kingdom takes part together.' },
];
function displayStatus(ev, now) {
  let key;
  if (ev.kind === 'announcement') {
    key = { draft: 'scheduled', scheduled: 'scheduled', posted: 'announced', failed: 'failed', cancelled: 'cancelled' }[ev.post_status] || 'scheduled';
  } else {
    key = { pending: 'scheduled', posted: 'announced', active: 'live', completed: 'completed', cancelled: 'cancelled', error: 'failed' }[ev.post_status] || 'scheduled';
    if (key === 'announced' || key === 'live') {
      if (now >= ev._end) key = 'completed';
      else if (now >= ev._start) key = 'live';
    }
  }
  return key;
}

// ── Alliances ───────────────────────────────────────────────────
function fallbackColor(slug) {
  const i = Math.max(0, ALLIANCES.findIndex((a) => a.slug === slug));
  return `oklch(0.72 0.14 ${[28, 235, 150, 300, 200, 60][i % 6]})`;
}
function allianceInfo(slug, name) {
  const a = ALLIANCES.find((x) => x.slug === slug) || {};
  return {
    slug, name: a.name || name || String(slug || '').toUpperCase(), icon: a.icon_image_data || '',
    color: safeColor(COLOR_MAP[slug] || a.color, fallbackColor(slug)),
  };
}
function initials(name) { return String(name || '').replace(/[^\p{L}\p{N}]/gu, '').slice(0, 3).toUpperCase(); }
function crestHtml(name, icon, color, size) {
  const c = safeColor(color, KINGDOM_COLOR);
  const inner = icon ? `<img src="${escapeHtml(icon)}" alt="">` : escapeHtml(initials(name));
  return `<span class="crest${size ? ' crest-' + size : ''}" data-vars="--c:${c};--ink-on:${inkOn(c)}" title="${escapeHtml(name)}">${inner}</span>`;
}
function evAlliances(ev) {
  if (ev.scope === 'kingdom-wide') return [{ name: 'Kingdom', icon: '', color: KINGDOM_COLOR, slug: '' }];
  const t = ev.targets && ev.targets.length ? ev.targets : (ev.tenant_slug ? [{ tenant_slug: ev.tenant_slug, tenant_name: ev.tenant_name }] : []);
  if (!t.length) return [{ name: 'Kingdom', icon: '', color: KINGDOM_COLOR, slug: '' }];
  return t.map((x) => allianceInfo(x.tenant_slug, x.tenant_name));
}
function matchesFilter(ev) {
  if (FILTER === 'all' || ev.scope === 'kingdom-wide') return true;
  const slugs = ev.targets && ev.targets.length ? ev.targets.map((t) => t.tenant_slug) : [ev.tenant_slug];
  return slugs.includes(FILTER);
}

// The combined API returns one row per (item, target alliance). Collapse them to one card per item.
function groupCombinedFanoutRows(events) {
  if (!COMBINED_MODE) return events;
  const grouped = [], index = new Map();
  events.forEach((ev) => {
    if (ev.scope === 'kingdom-wide' || !ev.tenant_slug) { grouped.push(ev); return; }
    const key = `${ev.kind}:${ev.id}`;
    const target = { tenant_name: ev.tenant_name, tenant_slug: ev.tenant_slug, tenant_color: ev.tenant_color, notification_channel_name: ev.notification_channel_name };
    if (index.has(key)) { grouped[index.get(key)].targets.push(target); return; }
    index.set(key, grouped.length);
    grouped.push({ ...ev, targets: [target] });
  });
  grouped.forEach((ev) => { if (ev.targets && ev.targets.length > 1) ev.targets.sort((a, b) => a.tenant_name.localeCompare(b.tenant_name)); });
  return grouped;
}

// Days an event touches, in the display zone. Multi-day events return several keys.
function daySpan(ev, tz) {
  const first = dayKey(ev._start, tz);
  const last = ev._end > ev._start ? dayKey(ev._end - 1, tz) : first;
  const keys = [first];
  for (let t = ev._start + DAY; keys.length < 21; t += DAY) {
    const k = dayKey(t, tz);
    if (k > last) break;
    if (k !== keys[keys.length - 1]) keys.push(k);
  }
  if (keys[keys.length - 1] !== last && last > first) keys.push(last);
  return keys;
}
function buildDayMap(list, tz) {
  const map = new Map();
  list.forEach((ev) => {
    const span = daySpan(ev, tz);
    span.forEach((k, i) => {
      if (!map.has(k)) map.set(k, []);
      map.get(k).push({ ev, cont: i > 0, last: i === span.length - 1 });
    });
  });
  map.forEach((arr) => arr.sort((a, b) => (a.cont === b.cont ? a.ev._start - b.ev._start : (a.cont ? -1 : 1))));
  return map;
}

// Everything above this line is pure/DOM-independent; everything below
// wires up real page elements and fetches. Under test, stop here and hand
// the pure functions to the test file instead of running any of that
// against a document that isn't the real events.html.
if (SAMAYA_TEST) {
  globalThis.__SAMAYA_TEST_EXPORTS__ = {
    escapeHtml, safeColor, inkOn, brandInk, pad2, parseIso, hm, dayKey, fmtDay, tzShort, cd, rel, formatDuration,
    displayStatus, fallbackColor, allianceInfo, initials, crestHtml, evAlliances, matchesFilter,
    groupCombinedFanoutRows, daySpan, buildDayMap,
  };
  return;
}

// ── Brand variables: markup carries data-vars, painted through the CSSOM ──
new MutationObserver((ms) => ms.forEach((m) => m.addedNodes.forEach((n) => {
  if (n.nodeType !== 1) return;
  if (n.dataset.vars) paintOne(n);
  n.querySelectorAll('[data-vars]').forEach(paintOne);
}))).observe(document.body, { childList: true, subtree: true });

// ── Modals ──────────────────────────────────────────────────────
let lastFocus = null;
function openModal(id) {
  const m = $(id);
  lastFocus = document.activeElement;
  m.hidden = false;
  const f = m.querySelector('select, textarea, input, button:not(.modal-x)');
  if (f) f.focus();
}
function closeModal(id) {
  const m = $(id);
  if (!m || m.hidden) return;
  if (id === 'timezoneModal' && !localStorage.getItem(TZ_KEY)) setDisplayTz(getDisplayTz());
  m.hidden = true;
  if (lastFocus && lastFocus.focus) lastFocus.focus();
}
document.querySelectorAll('.modal').forEach((m) => {
  m.addEventListener('click', (e) => { if (e.target === m || e.target.closest('[data-close]')) closeModal(m.id); });
});
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  const open = document.querySelector('.modal:not([hidden])');
  if (open) closeModal(open.id);
  closeCalMenu();
});

function openTimezoneModal(firstVisit) {
  const current = getDisplayTz();
  $('timezoneModalIntro').textContent = firstVisit
    ? `Event times are shown in UTC and your local time so nothing is missed across time zones. We detected ${current} from your browser. Pick a different one below if that is wrong, or close this if it looks right.`
    : 'Sets the local half of every time shown next to UTC on this page.';
  const zones = allTimeZones();
  const list = zones.includes(current) ? zones : [current, ...zones];
  $('timezoneSelect').innerHTML = list.map((z) => `<option value="${escapeHtml(z)}"${z === current ? ' selected' : ''}>${escapeHtml(z)}</option>`).join('');
  openModal('timezoneModal');
}
$('timezoneSelect').addEventListener('change', (e) => { setDisplayTz(e.target.value); closeModal('timezoneModal'); tickClock(); renderAll(); });
$('clockBtn').addEventListener('click', () => openTimezoneModal(false));

function tickClock() {
  const now = Date.now(), tz = getDisplayTz();
  $('clockBtn').textContent = tz === 'UTC' ? `${hm(now, 'UTC')} UTC` : `${hm(now, 'UTC')} UTC · ${hm(now, tz)} ${tzShort(tz)}`;
}

// ── Add to calendar menu ────────────────────────────────────────
function buildCalMenu() {
  const scope = COMBINED_MODE
    ? 'Subscribing adds every alliance’s events, combined into one calendar.'
    : `Subscribing adds only ${escapeHtml(TENANT_SLUG.toUpperCase())}’s events. Switch alliances for a different feed.`;
  const title = COMBINED_MODE ? 'Kingshot Event Schedule' : `${TENANT_SLUG.toUpperCase()} — Kingshot Event Schedule`;
  const google = `https://calendar.google.com/calendar/render?cid=${encodeURIComponent(ICS_ABSOLUTE_URL)}`;
  const outlook = `https://outlook.live.com/calendar/0/addfromweb?url=${encodeURIComponent(ICS_ABSOLUTE_URL)}&name=${encodeURIComponent(title)}`;
  $('calMenu').innerHTML = `<div class="menu-note">${scope}</div>
    <a role="menuitem" href="${google}" target="_blank" rel="noopener">Google Calendar<small>Opens Google Calendar and subscribes</small></a>
    <a role="menuitem" href="${ICS_WEBCAL_URL}">Apple Calendar / Outlook desktop<small>Uses your device’s default calendar app</small></a>
    <a role="menuitem" href="${outlook}" target="_blank" rel="noopener">Outlook.com<small>Opens Outlook on the web and subscribes</small></a>
    <a role="menuitem" href="${ICS_ABSOLUTE_URL}">Download .ics file<small>For manual import into any other app</small></a>`;
}
function closeCalMenu() { $('calMenu').hidden = true; $('calMenuBtn').setAttribute('aria-expanded', 'false'); }
$('calMenuBtn').addEventListener('click', (e) => {
  e.stopPropagation();
  const open = $('calMenu').hidden;
  $('calMenu').hidden = !open;
  $('calMenuBtn').setAttribute('aria-expanded', String(open));
});
document.addEventListener('click', (e) => { if (!e.target.closest('.menu-wrap')) closeCalMenu(); });

// ── Header, chips, key ──────────────────────────────────────────
function renderHeader() {
  const cur = COMBINED_MODE ? null : allianceInfo(TENANT_SLUG);
  $('pageKicker').textContent = COMBINED_MODE ? 'Kingshot · All alliances' : `Kingshot · ${cur.name}`;
  $('pageTitle').textContent = SITE_TITLE || 'Event Schedule';
  document.title = `${COMBINED_MODE ? '' : cur.name + ' · '}${SITE_TITLE || 'Kingshot Event Schedule'}`;
}
function renderChips() {
  const all = { slug: 'all', name: 'All alliances', icon: '', color: KINGDOM_COLOR };
  const items = [all].concat(ALLIANCES.map((a) => allianceInfo(a.slug, a.name)));
  $('allianceChips').innerHTML = items.map((a) => {
    const crest = '';
    const cc = safeColor(a.color, KINGDOM_COLOR);
    const style = `data-vars="--c:${cc};--ink-on:${inkOn(cc)}"`;
    if (COMBINED_MODE) {
      return `<button type="button" class="chip" ${style} data-filter="${escapeHtml(a.slug)}" aria-pressed="${FILTER === a.slug}">${crest}${escapeHtml(a.name)}</button>`;
    }
    const href = a.slug === 'all' ? '/events' : `/t/${encodeURIComponent(a.slug)}/events`;
    const current = a.slug === TENANT_SLUG;
    return `<a class="chip" ${style} href="${href}"${current ? ' aria-current="page"' : ''}>${crest}${escapeHtml(a.name)}</a>`;
  }).join('');
}
$('allianceChips').addEventListener('click', (e) => {
  const b = e.target.closest('[data-filter]');
  if (!b) return;
  FILTER = b.dataset.filter;
  renderAll();
});
function renderKey() {
  $('legendPanel').innerHTML = KINDS.concat(Object.values(STATUS)).map((i) =>
    `<div class="legend-item"><span class="legend-name" data-vars="--s:${i.color}"><i class="swatch"></i>${i.text}</span><span>${i.desc}</span></div>`).join('');
}
$('keyBtn').addEventListener('click', () => {
  const open = $('legendPanel').hidden;
  $('legendPanel').hidden = !open;
  $('keyBtn').setAttribute('aria-expanded', String(open));
  $('keyBtn').textContent = open ? 'Key ▴' : 'Key ▾';
});

// ── Hero ────────────────────────────────────────────────────────
function renderHero() {
  const now = Date.now(), tz = getDisplayTz();
  const list = EVENTS.filter(matchesFilter).filter((e) => {
    const s = displayStatus(e, now);
    return s !== 'cancelled' && s !== 'failed' && s !== 'completed' && e._end > now;
  }).sort((a, b) => a._start - b._start);
  const evList = list.filter((e) => e.kind === 'event');
  const h = evList.find((e) => displayStatus(e, now) === 'live') || evList[0];
  let html = '';
  if (h) {
    const live = displayStatus(h, now) === 'live';
    const a = evAlliances(h)[0];
    html += `<div class="hero${live ? ' is-live' : ''}" data-vars="--c:${safeColor(a.color, KINGDOM_COLOR)}">
      <div class="hero-main">
        <div class="hero-label">${live ? '<span class="dot"></span>Live now' : 'Next up'}</div>
        <div class="hero-name">${escapeHtml(h.event_name)}</div>
        <div class="hero-meta">${escapeHtml(evAlliances(h).map((x) => x.name).join(', '))} · ${live ? 'started ' : ''}${hm(h._start, tz)} ${escapeHtml(tzShort(tz))} · ${hm(h._start, 'UTC')} UTC${h.duration_hours ? ' · ' + formatDuration(h.duration_hours) : ''}</div>
      </div>
      <div class="hero-cd"><small>${live ? 'Ends in' : 'Starts in'}</small><b id="heroCd" data-target="${live ? h._end : h._start}">${cd((live ? h._end : h._start) - now)}</b></div>
      ${live ? `<span class="hero-bar" id="heroBar" data-start="${h._start}" data-end="${h._end}" data-vars="--p:${progressPct(h._start, h._end, now)}%"></span>` : ''}
    </div>`;
  } else {
    html += `<div class="hero"><div class="hero-main"><div class="hero-label">No events</div><div class="hero-name">Nothing scheduled</div></div></div>`;
  }
  const then = list.filter((e) => e !== h && displayStatus(e, now) !== 'live').slice(0, 3);
  html += `<aside class="then"><h2>Then</h2>${then.length ? then.map((e) =>
    `<div class="then-item"><span class="then-time">${hm(e._start, tz)}</span><span class="then-name">${escapeHtml(e.event_name)}</span><span class="then-when">${escapeHtml(fmtDay(e._start, tz, { weekday: 'short' }))} · ${rel(e._start - now)}</span></div>`).join('') : '<p class="muted">Nothing else coming up.</p>'}</aside>`;
  $('hero').innerHTML = html;
}
function progressPct(start, end, now) { return Math.max(0, Math.min(100, Math.round(((now - start) / (end - start)) * 100))); }
function tickCountdown() {
  const el = $('heroCd');
  if (el) el.textContent = cd(Number(el.dataset.target) - Date.now());
  const bar = $('heroBar');
  if (bar) bar.style.setProperty('--p', progressPct(Number(bar.dataset.start), Number(bar.dataset.end), Date.now()) + '%');
}

// ── Rows and days ───────────────────────────────────────────────
function notifyText(ev) {
  const m = ev.kind === 'announcement' ? ev.event_offset_minutes : ev.notify_minutes_before;
  if (!m) return 'None';
  return ev.kind === 'announcement' ? `Sent ${m} min before the event` : `${m} min before`;
}
function rowHtml(ev, cont, last, now, tz) {
  const key = displayStatus(ev, now), st = STATUS[key];
  const als = evAlliances(ev), open = OPEN.has(ev._k);
  const isAnn = ev.kind === 'announcement';
  const multi = daySpan(ev, tz).length > 1;
  const endDay = fmtDay(ev._end, tz, { weekday: 'short' });
  const timeBlock = cont
    ? (last ? `<b>↳</b><small>until ${hm(ev._end, tz)}</small>` : `<b>↳</b><small>all day</small>`)
    : `<b>${hm(ev._start, tz)}</b>${tz === 'UTC' ? '' : `<small>${hm(ev._start, 'UTC')} UTC</small>`}`;
  const dur = isAnn ? '' : (multi ? `${formatDuration(ev.duration_hours)} · ends ${escapeHtml(endDay)} ${hm(ev._end, tz)}` : formatDuration(ev.duration_hours));
  const relText = cont ? 'continues' : (key === 'live' ? `ends ${rel(ev._end - now)}` : (ev._start > now ? `${isAnn ? 'sends' : 'starts'} ${rel(ev._start - now)}` : ''));
  const kind = (ev.type && ev.type.name ? ev.type.name : (isAnn ? 'Message' : 'Event'));
  const id = `d-${ev._k.replace(/\W/g, '_')}${cont ? '-c' : ''}`;
  const color = safeColor(ev.type && ev.type.color ? ev.type.color : als[0].color, KINGDOM_COLOR);
  return `<article class="row${key === 'live' ? ' is-live' : ''}${key === 'completed' || key === 'cancelled' ? ' is-done' : ''}" data-vars="--c:${color}" data-key="${escapeHtml(ev._k)}">
    <button type="button" class="row-head" data-action="toggle" aria-expanded="${open}" aria-controls="${id}">
      <span class="row-time">${timeBlock}</span>
      <span class="row-main"><span class="row-name">${escapeHtml(ev.event_name)}</span>
        <span class="row-meta"><span>${escapeHtml(als.slice(0, 2).map((a) => a.name).join(', '))}${als.length > 2 ? ` +${als.length - 2}` : ''}</span><span>${kind}</span>${dur ? `<span>${dur}</span>` : ''}</span></span>
      <span class="row-side">
        <span class="row-rel">${relText}</span>
        <span class="status" data-vars="--s:${st.color}"><i class="swatch"></i>${st.text}</span>
      </span>
      <span class="chev" aria-hidden="true">${open ? '▲' : '▼'}</span>
    </button>
    <div class="row-detail" id="${id}"${open ? '' : ' hidden'}>
      <dl class="facts">
        <div><dt>Alliance</dt><dd>${escapeHtml(als.map((a) => a.name).join(', '))}</dd></div>
        <div><dt>Discord channel</dt><dd>${escapeHtml(ev.discord_channel || 'Not set')}</dd></div>
        <div><dt>Reminder</dt><dd>${escapeHtml(notifyText(ev))}</dd></div>
      </dl>
      ${ev.description ? `<p class="desc">${escapeHtml(ev.description)}</p>` : ''}
      <div class="row-actions">
        <button type="button" class="btn-plain" data-action="preview">Discord preview</button>
        <button type="button" class="btn-plain" data-action="report">Report an issue</button>
      </div>
    </div>
  </article>`;
}
function dayHeader(k, tz, now) {
  const [y, m, d] = k.split('-').map(Number);
  const ms = Date.UTC(y, m - 1, d, 12);
  const today = k === dayKey(now, tz), tom = k === dayKey(now + DAY, tz);
  const w = new Intl.DateTimeFormat(undefined, { weekday: 'long', timeZone: 'UTC' }).format(ms);
  const dt = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'long', timeZone: 'UTC' }).format(ms);
  return `<div class="day-head"><h2${today ? ' class="is-today"' : ''}>${today ? 'Today' : tom ? 'Tomorrow' : escapeHtml(w)}</h2><span>${escapeHtml(dt)}</span></div>`;
}
function renderSchedule() {
  const now = Date.now(), tz = getDisplayTz();
  const map = buildDayMap(EVENTS.filter(matchesFilter), tz);
  let keys = [...map.keys()].sort();
  if (VIEW === 'calendar') keys = keys.filter((k) => k === SEL_DAY);
  if (!keys.length) {
    $('schedule').innerHTML = `<p class="empty">${ALL_LOADED ? (VIEW === 'calendar' ? 'Nothing scheduled on this day.' : 'No events scheduled. Check back soon, or subscribe to the calendar feed to be notified automatically.') : 'Loading events…'}</p>`;
    return;
  }
  $('schedule').innerHTML = keys.map((k) =>
    `<section class="day">${dayHeader(k, tz, now)}<div class="rows">${map.get(k).map((x) => rowHtml(x.ev, x.cont, x.last, now, tz)).join('')}</div></section>`).join('');
}
$('schedule').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-action]');
  if (!btn) return;
  const row = btn.closest('.row');
  const ev = EVENTS.find((x) => x._k === row.dataset.key);
  if (!ev) return;
  if (btn.dataset.action === 'toggle') {
    if (OPEN.has(ev._k)) OPEN.delete(ev._k); else OPEN.add(ev._k);
    renderSchedule();
    const again = $('schedule').querySelector(`[data-key="${CSS.escape(ev._k)}"] .row-head`);
    if (again) again.focus();
  } else if (btn.dataset.action === 'preview') openDiscordPreview(ev);
  else if (btn.dataset.action === 'report') openReportIssueModal(ev);
});

// ── Calendar ────────────────────────────────────────────────────
function initCal() {
  if (CAL_YEAR !== null) return;
  const [y, m] = dayKey(Date.now(), getDisplayTz()).split('-').map(Number);
  CAL_YEAR = y; CAL_MONTH = m - 1;
  SEL_DAY = dayKey(Date.now(), getDisplayTz());
}
function monthName(y, m, opts) { return new Intl.DateTimeFormat(undefined, Object.assign({ timeZone: 'UTC' }, opts)).format(Date.UTC(y, m, 1)); }
function renderCalendar() {
  initCal();
  const tz = getDisplayTz(), todayKey = dayKey(Date.now(), tz);
  $('calMonthLabel').textContent = monthName(CAL_YEAR, CAL_MONTH, { month: 'long', year: 'numeric' });
  const pm = new Date(Date.UTC(CAL_YEAR, CAL_MONTH - 1, 1)), nm = new Date(Date.UTC(CAL_YEAR, CAL_MONTH + 1, 1));
  $('calPrevBtn').textContent = `◀ ${monthName(pm.getUTCFullYear(), pm.getUTCMonth(), { month: 'short' })}`;
  $('calNextBtn').textContent = `${monthName(nm.getUTCFullYear(), nm.getUTCMonth(), { month: 'short' })} ▶`;
  const map = buildDayMap(EVENTS.filter(matchesFilter), tz);
  const lead = (new Date(Date.UTC(CAL_YEAR, CAL_MONTH, 1)).getUTCDay() + 6) % 7;
  const dim = new Date(Date.UTC(CAL_YEAR, CAL_MONTH + 1, 0)).getUTCDate();
  const total = Math.ceil((lead + dim) / 7) * 7;
  let html = Array.from({ length: 7 }, (_, i) => `<div class="cal-dow">${escapeHtml(new Intl.DateTimeFormat(undefined, { weekday: 'short', timeZone: 'UTC' }).format(Date.UTC(2024, 0, 1 + i)))}</div>`).join('');
  for (let i = 0; i < total; i++) {
    const dt = new Date(Date.UTC(CAL_YEAR, CAL_MONTH, 1 - lead + i)), k = dt.toISOString().slice(0, 10);
    const items = map.get(k) || [], inMonth = dt.getUTCMonth() === CAL_MONTH;
    const chips = items.slice(0, 3).map(({ ev, cont }) => {
      const a = evAlliances(ev)[0], c = safeColor(a.color, KINGDOM_COLOR);
      return `<span class="cal-chip${cont ? ' is-cont' : ''}" data-vars="--c:${c};--ink-on:${inkOn(c)}">${cont ? '◂ ' : hm(ev._start, tz) + ' '}${escapeHtml(ev.event_name)}</span>`;
    }).join('');
    const label = `${fmtDay(dt.getTime(), 'UTC', { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })}${items.length ? `, ${items.length} item${items.length === 1 ? '' : 's'}` : ''}`;
    html += `<button type="button" class="cal-cell${inMonth ? '' : ' is-adjacent'}${k === todayKey ? ' is-today' : ''}${k === SEL_DAY ? ' is-selected' : ''}" data-day="${k}" aria-label="${escapeHtml(label)}" aria-pressed="${k === SEL_DAY}"><span class="cal-num">${dt.getUTCDate()}</span>${chips}${items.length > 3 ? `<span class="cal-more">+${items.length - 3} more</span>` : ''}</button>`;
  }
  $('calGrid').innerHTML = html;
}
$('calGrid').addEventListener('click', (e) => {
  const b = e.target.closest('[data-day]');
  if (!b) return;
  SEL_DAY = b.dataset.day;
  renderCalendar(); renderSchedule();
  const s = $('calGrid').querySelector(`[data-day="${SEL_DAY}"]`);
  if (s) s.focus();
});
function shiftMonth(n) { initCal(); const d = new Date(Date.UTC(CAL_YEAR, CAL_MONTH + n, 1)); CAL_YEAR = d.getUTCFullYear(); CAL_MONTH = d.getUTCMonth(); renderCalendar(); }
$('calPrevBtn').addEventListener('click', () => shiftMonth(-1));
$('calNextBtn').addEventListener('click', () => shiftMonth(1));
$('calTodayBtn').addEventListener('click', () => { CAL_YEAR = null; initCal(); renderCalendar(); renderSchedule(); });

function setView(v) {
  VIEW = v;
  localStorage.setItem('samaya_events_view', v);
  $('calendarWrap').hidden = v !== 'calendar';
  $('viewListBtn').setAttribute('aria-pressed', String(v === 'list'));
  $('viewCalendarBtn').setAttribute('aria-pressed', String(v === 'calendar'));
  if (v === 'calendar') renderCalendar();
  renderSchedule();
}
$('viewListBtn').addEventListener('click', () => setView('list'));
$('viewCalendarBtn').addEventListener('click', () => setView('calendar'));

function renderAll() {
  const bs = currentBrandSlug();
  applyBrand(bs ? allianceInfo(bs).color : KINGDOM_HEX);
  renderChips(); renderHero();
  if (VIEW === 'calendar') renderCalendar();
  renderSchedule();
}

// ── Discord preview ─────────────────────────────────────────────
function formatDiscordAbsolutePreview(date) {
  return date.toLocaleString(undefined, { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric', hour: 'numeric', minute: '2-digit' }) + ' (preview)';
}
function formatDiscordRelativePreview(date) {
  const mins = Math.round((date.getTime() - Date.now()) / 60000), abs = Math.abs(mins);
  const text = abs < 60 ? `${abs} min` : abs < 1440 ? `${Math.round(abs / 60)} hr` : `${Math.round(abs / 1440)} day${Math.round(abs / 1440) === 1 ? '' : 's'}`;
  return (mins >= 0 ? `in ${text}` : `${text} ago`) + ' (preview)';
}
function clientRenderPlaceholders(text, o) {
  const eventTime = new Date(o.scheduledFor.getTime() + (o.eventOffsetMinutes || 0) * 60000);
  const values = {
    alliance_name: o.allianceName || '', kingdom_name: o.kingdomName || '',
    send_time: formatDiscordAbsolutePreview(o.scheduledFor), send_time_relative: formatDiscordRelativePreview(o.scheduledFor),
    event_time: formatDiscordAbsolutePreview(eventTime), event_time_relative: formatDiscordRelativePreview(eventTime),
  };
  return text.replace(/\{(alliance_name|kingdom_name|send_time|send_time_relative|event_time|event_time_relative)\}/g, (m, n) => (values[n] !== undefined ? values[n] : m));
}
function renderDiscordMarkdownPreview(text) {
  let html = escapeHtml(text);
  const stash = [];
  html = html.replace(/```([\s\S]*?)```/g, (m, code) => { stash.push('<pre class="preview-codeblock"><code>' + code.replace(/^\n/, '') + '</code></pre>'); return `\u0000CODEBLOCK${stash.length - 1}\u0000`; });
  html = html.replace(/`([^`\n]+)`/g, (m, code) => { stash.push('<code class="preview-inline-code">' + code + '</code>'); return `\u0000CODEBLOCK${stash.length - 1}\u0000`; });
  html = html.replace(/^(?:&gt; ?.*(?:\n|$))+/gm, (m) => '<blockquote class="preview-quote">' + m.replace(/^&gt; ?/gm, '').trim() + '</blockquote>');
  html = html.replace(/^### (.*)\n?/gm, '<h3 class="preview-h">$1</h3>').replace(/^## (.*)\n?/gm, '<h2 class="preview-h">$1</h2>').replace(/^# (.*)\n?/gm, '<h1 class="preview-h">$1</h1>');
  html = html.replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>').replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>').replace(/__([^_]+)__/g, '<u>$1</u>');
  html = html.replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g, '<em>$1</em>').replace(/~~([^~]+)~~/g, '<s>$1</s>');
  html = html.replace(/\|\|([^|]+)\|\|/g, '<span class="preview-spoiler" onclick="this.classList.add(\'revealed\')">$1</span>');
  html = html.replace(/&lt;@&amp;(\d+)&gt;/g, '<span class="preview-mention">@role mention</span>').replace(/&lt;#(\d+)&gt;/g, '<span class="preview-mention">#channel mention</span>');
  html = html.replace(/^- (.*)$/gm, '&nbsp;&nbsp;• $1').replace(/\n/g, '<br>');
  html = html.replace(/\u0000CODEBLOCK(\d+)\u0000/g, (m, i) => stash[Number(i)]);
  return html || '<span class="discord-preview-muted">(nothing to preview yet)</span>';
}
function openDiscordPreview(ev) {
  const pane = $('discordPreviewModalBody');
  const isAnn = ev.kind === 'announcement';
  const allianceName = ev.tenant_name || (!COMBINED_MODE ? allianceInfo(TENANT_SLUG).name : '') || '';
  let scheduledFor, eventOffsetMinutes;
  if (isAnn) { scheduledFor = new Date(ev._start); eventOffsetMinutes = ev.event_offset_minutes || 0; }
  else { scheduledFor = new Date(); eventOffsetMinutes = Math.round((ev._start - Date.now()) / 60000); }
  const html = renderDiscordMarkdownPreview(clientRenderPlaceholders(ev.description || '', { allianceName, kingdomName: '', scheduledFor, eventOffsetMinutes }));
  if (isAnn) {
    const multi = ev.targets && ev.targets.length > 1;
    const note = multi ? `<p class="discord-preview-note">Sent independently to ${ev.targets.map((t) => escapeHtml(t.tenant_name)).join(', ')}. Preview uses ${escapeHtml(ev.targets[0].tenant_name)}’s name; each alliance’s actual post fills in its own name and channel.</p>` : '';
    pane.innerHTML = `<div class="discord-preview-msg"><div class="discord-preview-avatar">S</div><div class="discord-preview-body">
      <div class="discord-preview-header"><span class="discord-preview-name">Samaya</span><span class="discord-preview-bot-tag">BOT</span><span class="discord-preview-timestamp">${escapeHtml(formatDiscordAbsolutePreview(scheduledFor))}</span></div>
      <div class="discord-preview-text">${html}</div></div></div>${note}`;
  } else {
    const start = new Date(scheduledFor.getTime() + eventOffsetMinutes * 60000);
    pane.innerHTML = `<div class="discord-preview-event">${ev.cover_image_data ? `<img class="discord-preview-event-cover" src="${escapeHtml(ev.cover_image_data)}" alt="">` : ''}
      <div class="discord-preview-event-body"><div class="discord-preview-event-title">${escapeHtml(ev.event_name)}</div>
      <div class="discord-preview-event-time">${escapeHtml(formatDiscordAbsolutePreview(start))} <span class="discord-preview-muted">(${escapeHtml(formatDiscordRelativePreview(start))})</span></div>
      <div class="discord-preview-event-meta">${ev.duration_hours ? formatDuration(ev.duration_hours) : ''}${ev.discord_channel ? (ev.duration_hours ? ' · ' : '') + escapeHtml(ev.discord_channel) : ''}</div>
      <div class="discord-preview-text discord-preview-event-desc">${html}</div>
      <button type="button" class="discord-preview-event-interested" disabled>✓ Interested</button></div></div>`;
  }
  openModal('discordPreviewModal');
}

// ── Report an issue ─────────────────────────────────────────────
let REPORT_TARGET = null;
function openReportIssueModal(ev) {
  REPORT_TARGET = ev;
  $('reportIssueModalSubject').textContent = `About: ${ev.event_name} (${ev.occurrence_date || dayKey(ev._start, 'UTC')})`;
  $('riErrorType').selectedIndex = 0; $('riDescription').value = ''; $('riContact').value = ''; $('riNote').textContent = '';
  $('riSubmit').disabled = false;
  openModal('reportIssueModal');
}
$('riSubmit').addEventListener('click', async () => {
  const ev = REPORT_TARGET;
  if (!ev) return;
  const description = $('riDescription').value.trim();
  if (!description) { $('riNote').textContent = 'Please describe what looks wrong.'; $('riDescription').focus(); return; }
  const payload = {
    kind: 'error', error_type: $('riErrorType').value, title: `Issue: ${ev.event_name}`, description,
    tenant_slug: ev.tenant_slug || TENANT_SLUG || null, submitter_contact: $('riContact').value.trim() || null,
  };
  payload.related_occurrence_id = ev.id;
  $('riSubmit').disabled = true;
  try {
    const res = await fetch('/api/tickets', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    if (!res.ok) { const b = await res.json().catch(() => ({})); throw new Error(b.detail || res.statusText); }
    $('riNote').textContent = 'Thanks. Your report was submitted. You can follow its status on the Feedback page.';
    setTimeout(() => closeModal('reportIssueModal'), 1800);
  } catch (err) {
    $('riNote').textContent = `Could not submit that: ${err.message}`;
    $('riSubmit').disabled = false;
  }
});

// ── Data loading ────────────────────────────────────────────────
async function loadAlliances() {
  try { ALLIANCES = await (await fetch('/api/alliances')).json(); } catch (e) { ALLIANCES = []; }
  ALLIANCES.forEach((a) => { if (a.color) COLOR_MAP[a.slug] = a.color; });
  renderHeader(); renderChips(); renderAll();
}
async function loadEvents() {
  try {
    const raw = await (await fetch(API_URL)).json();
    EVENTS = groupCombinedFanoutRows(raw).map((ev) => {
      const start = parseIso(ev.start_datetime_utc);
      if (ev.tenant_slug && ev.tenant_color) COLOR_MAP[ev.tenant_slug] = ev.tenant_color;
      return Object.assign(ev, { _start: start, _end: start + (ev.kind === 'announcement' ? 0 : (ev.duration_hours || 0) * HOUR), _k: `${ev.kind}:${ev.id}:${start}` });
    });
    ALL_LOADED = true;
    renderAll();
  } catch (e) {
    ALL_LOADED = true; EVENTS = [];
    renderHero();
    $('schedule').innerHTML = `<p class="empty">Could not load events. ${escapeHtml(e.message)}</p>`;
  }
}
async function loadLastActivity() {
  const el = $('lastActivity');
  try {
    const a = await (await fetch(COMBINED_MODE ? '/api/last-activity' : `/t/${TENANT_SLUG}/api/last-activity`)).json();
    if (!a) { el.hidden = true; return; }
    const tz = getDisplayTz(), at = parseIso(a.at);
    el.innerHTML = `${a.kind === 'announcement' ? 'Last message' : 'Last event posted'}: <strong>${escapeHtml(a.name)}</strong>${COMBINED_MODE && a.tenant_name ? ' · ' + escapeHtml(a.tenant_name) : ''} · ${escapeHtml(fmtDay(at, tz, { day: 'numeric', month: 'short' }))} ${hm(at, tz)}`;
    el.hidden = false;
  } catch (e) { el.hidden = true; }
}
fetch('/api/kingdom-branding').then((r) => r.json()).then((b) => {
  if (b.public_site_title) { SITE_TITLE = b.public_site_title; renderHeader(); }
  if (/^#[0-9a-f]{6}$/i.test(b.color || '')) { KINGDOM_COLOR = KINGDOM_HEX = b.color; renderAll(); }
  if (b.theme_id && /^[\w-]+$/.test(b.theme_id)) document.documentElement.dataset.theme = b.theme_id;
  if (b.banner_url) {
    const img = new Image();
    img.onload = () => document.documentElement.style.setProperty('--banner', 'url("' + encodeURI(b.banner_url) + '")');
    img.src = b.banner_url;
  }
}).catch(() => {});

// ── Boot ────────────────────────────────────────────────────────
buildCalMenu();
$('icsLink').href = ICS_URL;
renderKey(); renderHeader(); tickClock(); setView(VIEW); renderAll();
loadAlliances(); loadEvents(); loadLastActivity();
setInterval(tickClock, 30000);
setInterval(tickCountdown, 1000);
setInterval(() => { renderAll(); }, 60000);
setInterval(loadEvents, 600000);
setInterval(loadLastActivity, 600000);
if (!localStorage.getItem(TZ_KEY)) openTimezoneModal(true);
})();
