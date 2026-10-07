// Events tab (#v-events, spec §66.7): one list of every event and message, and
// one form for create and edit. Depends on common.js, pickers.js, composer.js
// and reminders.js; occurrence-level edits live in schedule.js.
//
// Editing a recurring event first asks for a scope (spec §66.4a):
//   this occurrence only  -> PATCH /api/occurrences/{id}   (schedule.js)
//   this and following    -> POST  /api/events/{id}/split
//   all occurrences       -> PATCH /api/events/{id}

// ── State ────────────────────────────────────────────────────
let EVENTS = [];

// kingdom_id -> event types (types belong to a Kingdom, not an alliance).
const EVENT_TYPES_BY_KINGDOM = {};

async function loadEventTypesFor(slug) {
  const tenant = tenantBySlug(slug);
  const key = tenant ? tenant.kingdom_id : slug;
  if (EVENT_TYPES_BY_KINGDOM[key]) return EVENT_TYPES_BY_KINGDOM[key];
  const types = await api('GET', '/api/event-types', null, false, slug);
  EVENT_TYPES_BY_KINGDOM[key] = types;
  return types;
}

function invalidateEventTypes() {
  Object.keys(EVENT_TYPES_BY_KINGDOM).forEach((k) => { delete EVENT_TYPES_BY_KINGDOM[k]; });
}

// ── List ─────────────────────────────────────────────────────

function canWriteEvent(ev) {
  const owner = tenantById(ev.owning_tenant_id);
  if (owner) return canWriteTenant(owner);
  return ev.scope === 'kingdom-wide' && canWriteAnywhere();
}

async function loadEvents() {
  renderAllianceFilterSelect('eventsAlliance', 'events', loadEvents);
  document.getElementById('btnNewEvent').classList.toggle('hidden', !canWriteAnywhere());
  document.getElementById('eventsViewerNote').classList.toggle('hidden', canWriteAnywhere());
  try {
    EVENTS = await api('GET', '/api/events', null, false, getTabFilter('events'));
    renderEventTypeFilter();
    renderEventsTable();
  } catch (e) {
    toast(e.message, true);
    document.getElementById('eventsBody').innerHTML = emptyRow(6, 'Could not load events: ' + e.message);
  }
}

function renderEventTypeFilter() {
  const select = document.getElementById('eventsType');
  const current = select.value;
  const seen = new Map();
  EVENTS.forEach((ev) => { if (ev.type) seen.set(ev.type.id, ev.type.name); });
  const options = [{ value: '', label: 'All types' }]
    .concat(Array.from(seen.entries()).sort((a, b) => a[1].localeCompare(b[1])).map(([id, name]) => ({ value: id, label: name })));
  select.innerHTML = optionsHtml(options, current);
}

function eventScheduleText(ev) {
  const first = `${ev.anchor_date}T${ev.start_time_utc}:00Z`;
  const when = dualTimeString(first);
  let text;
  if (ev.recurrence_kind === 'interval_days') {
    text = `${describeRecurrence(ev)} at ${when}, from ${ev.anchor_date}`;
    if (ev.until_date) text += ` until ${ev.until_date}`;
  } else {
    text = `${ev.anchor_date} at ${when}`;
  }
  return text;
}

// Two lines: the rule and time, then the start date and end date.
function eventScheduleHtml(ev) {
  const when = dualTimeString(`${ev.anchor_date}T${ev.start_time_utc}:00Z`);
  if (ev.recurrence_kind === 'interval_days') {
    let sub = `from ${ev.anchor_date}`;
    if (ev.until_date) sub += ` until ${ev.until_date}`;
    return `<div class="sched">${escapeHtml(describeRecurrence(ev))} at ${escapeHtml(when)}</div><div class="samaya-muted">${escapeHtml(sub)}</div>`;
  }
  return `<div class="sched">${escapeHtml(ev.anchor_date)} at ${escapeHtml(when)}</div>`;
}

function shortReminder(m) {
  if (m === 0) return 'start';
  if (m % 1440 === 0) return (m / 1440) + 'd';
  if (m % 60 === 0) return (m / 60) + 'h';
  return m + 'm';
}

// Alliance chips plus an audience and channel count and any warning (spec §68).
// `audience_tenant_ids` already expands a Kingdom-wide event to every alliance.
function eventAudienceHtml(ev) {
  const ids = ev.audience_tenant_ids && ev.audience_tenant_ids.length
    ? ev.audience_tenant_ids
    : Array.from(new Set([ev.owning_tenant_id].concat(ev.alliances.map((a) => a.tenant_id))));
  // Show the first few alliances; the rest collapse into a "+N" with the names as a tooltip.
  const names = ids.map((id) => tenantName(id));
  const shown = names.slice(0, ev.scope === 'kingdom-wide' ? 2 : 3);
  const chips = shown.map((n) => `<span class="audience-tag">${escapeHtml(n)}</span>`).join(' ');
  const more = names.length > shown.length
    ? `<span class="audience-more" title="${escapeHtml(names.slice(shown.length).join(', '))}">+${names.length - shown.length}</span>` : '';
  const kingdom = ev.scope === 'kingdom-wide' ? `${pfLabel('Kingdom-wide', 'pf-m-purple')} ` : '';
  return `<div class="audience-chips">${kingdom}${chips}${more}</div>${destinationBadgeHtml(ev)}`;
}

function destinationBadgeHtml(o) {
  if (o.audience_count === undefined) return '';
  const text = `${o.audience_count} audience${o.audience_count === 1 ? '' : 's'}, `
    + `${o.channel_count} channel${o.channel_count === 1 ? '' : 's'}`;
  const warn = (o.warnings || []).map((w) => `<div class="warn-mark" role="note">Warning: ${escapeHtml(w)}</div>`).join('');
  return `<div class="samaya-muted">${escapeHtml(text)}</div>${warn}`;
}

function seriesCount(ev) {
  if (!ev.series_id) return 1;
  return EVENTS.filter((e) => e.series_id === ev.series_id).length;
}

function buildEventRow(ev) {
  const writable = canWriteEvent(ev);
  const labels = [];
  labels.push(ev.active ? pfLabel('Active', 'pf-m-green') : pfLabel('Inactive', 'pf-m-gray'));
  if (ev.leadership_only) labels.push(pfLabel('Leadership only', 'pf-m-orange'));
  if (!ev.has_calendar_entry) labels.push(pfLabel('Message only', 'pf-m-blue'));
  const parts = seriesCount(ev);
  if (parts > 1) labels.push(pfLabel(`Series, ${parts} parts`, 'pf-m-gray'));
  const reminders = ev.reminder_minutes.length
    ? ev.reminder_minutes.map((m) => `<span class="audience-tag">${escapeHtml(shortReminder(m))}</span>`).join(' ')
    : '<span class="samaya-muted">None</span>';
  const duration = ev.has_calendar_entry
    ? `<div class="samaya-muted">${ev.duration_hours} h calendar entry</div>`
    : '<div class="samaya-muted">No calendar entry</div>';
  const actions = writable
    ? `<div class="row-actions">
        <button type="button" class="pf-v6-c-button pf-m-secondary pf-m-small" data-action="edit" data-id="${ev.id}">Edit</button>
        <details class="menu">
          <summary class="menu__btn" aria-label="More actions for ${escapeHtml(ev.name)}">&#8943;</summary>
          <div class="menu__panel">
            <button type="button" class="menu__item" data-action="duplicate" data-id="${ev.id}">Duplicate</button>
            <button type="button" class="menu__item" data-action="toggle" data-id="${ev.id}">${ev.active ? 'Deactivate' : 'Activate'}</button>
            <hr class="menu__sep">
            <button type="button" class="menu__item menu__item--danger" data-action="delete" data-id="${ev.id}">Delete&hellip;</button>
          </div>
        </details>
      </div>`
    : '<span class="samaya-muted">Read only</span>';
  return `<tr class="pf-v6-c-table__tr">
    <td class="pf-v6-c-table__td" data-label="Event"><strong>${escapeHtml(ev.name)}</strong><div>${typeChip(ev.type)}</div></td>
    <td class="pf-v6-c-table__td" data-label="Schedule">${eventScheduleHtml(ev)}${duration}</td>
    <td class="pf-v6-c-table__td" data-label="Audience">${eventAudienceHtml(ev)}</td>
    <td class="pf-v6-c-table__td" data-label="Reminders">${reminders}</td>
    <td class="pf-v6-c-table__td" data-label="Status"><div class="label-stack">${labels.join(' ')}</div></td>
    <td class="pf-v6-c-table__td" data-label="Actions">${actions}</td>
  </tr>`;
}

function renderEventsTable() {
  const typeId = document.getElementById('eventsType').value;
  const scope = document.getElementById('eventsScope').value;
  const status = document.getElementById('eventsStatus').value;
  const q = document.getElementById('eventsSearch').value.trim().toLowerCase();
  const rows = EVENTS.filter((ev) => {
    if (typeId && String(ev.type_id) !== typeId) return false;
    if (scope && ev.scope !== scope) return false;
    if (status === 'active' && !ev.active) return false;
    if (status === 'inactive' && ev.active) return false;
    if (q && !ev.name.toLowerCase().includes(q)) return false;
    return true;
  });
  const body = document.getElementById('eventsBody');
  body.innerHTML = rows.length
    ? rows.map(buildEventRow).join('')
    : emptyRow(6, EVENTS.length ? 'No events match these filters.' : 'No events yet.');
  applyTypeColors(body);
  document.getElementById('eventsCount').textContent = `${rows.length} of ${EVENTS.length} shown`;
}

async function toggleEventActive(ev) {
  const turningOff = ev.active;
  if (turningOff && !confirm(`Deactivate "${ev.name}"? Its pending reminders are cancelled and any Discord events it created are removed. You can activate it again later.`)) return;
  try {
    const res = await api('PATCH', `/api/events/${ev.id}`, { active: !ev.active }, false, writeSlugForEvent(ev));
    toastDiscordErrors(res.discord_errors, turningOff ? 'Event deactivated.' : 'Event activated.');
    loadEvents();
  } catch (e) { toast(e.message, true); }
}

async function deleteEvent(ev) {
  if (!confirm(`Delete "${ev.name}" permanently? This also removes the Discord events it created, cancels its pending reminders and deletes its schedule. This cannot be undone.`)) return;
  try {
    await api('DELETE', `/api/events/${ev.id}`, null, false, writeSlugForEvent(ev));
    toast('Event deleted.');
    loadEvents();
  } catch (e) { toast(e.message, true); }
}

function eventById(id) {
  return EVENTS.find((e) => e.id === id) || null;
}

bindActions(document.getElementById('eventsBody'), {
  edit(btn) { const ev = eventById(parseInt(btn.dataset.id, 10)); if (ev) startEditEvent(ev); },
  duplicate(btn) {
    const ev = eventById(parseInt(btn.dataset.id, 10));
    if (ev) openEventForm({ mode: 'create', event: ev, duplicate: true });
  },
  toggle(btn) { const ev = eventById(parseInt(btn.dataset.id, 10)); if (ev) toggleEventActive(ev); },
  delete(btn) { const ev = eventById(parseInt(btn.dataset.id, 10)); if (ev) deleteEvent(ev); },
});

// Close the row menu after a choice, on an outside click, or on Escape.
document.addEventListener('click', (e) => {
  document.querySelectorAll('.menu[open]').forEach((m) => {
    if (!m.contains(e.target) || e.target.closest('.menu__item')) m.removeAttribute('open');
  });
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') document.querySelectorAll('.menu[open]').forEach((m) => m.removeAttribute('open'));
});

// Row density (compact or comfortable), remembered per browser.
const DENSITY_KEY = 'samaya_admin_density';
function setDensity(mode) {
  const comfy = mode === 'comfy';
  document.body.classList.toggle('density-comfy', comfy);
  document.getElementById('densityCompact').setAttribute('aria-pressed', String(!comfy));
  document.getElementById('densityComfy').setAttribute('aria-pressed', String(comfy));
  try { localStorage.setItem(DENSITY_KEY, mode); } catch { /* not fatal */ }
}
document.getElementById('densityCompact').addEventListener('click', () => setDensity('compact'));
document.getElementById('densityComfy').addEventListener('click', () => setDensity('comfy'));
setDensity((() => { try { return localStorage.getItem(DENSITY_KEY) === 'comfy' ? 'comfy' : 'compact'; } catch { return 'compact'; } })());

['eventsType', 'eventsScope', 'eventsStatus'].forEach((id) => {
  document.getElementById(id).addEventListener('change', renderEventsTable);
});
document.getElementById('eventsSearch').addEventListener('input', renderEventsTable);
document.getElementById('btnNewEvent').addEventListener('click', () => openEventForm({ mode: 'create' }));

// ── Edit scope (spec §66.4a) ─────────────────────────────────
let SCOPE_EVENT = null;

// The next dates a repeating event occurs on after today, from the recurrence
// rule itself (generated occurrences only cover about four weeks ahead).
function upcomingOccurrenceDates(ev, count) {
  const out = [];
  if (ev.recurrence_kind !== 'interval_days' || !ev.interval_days) return out;
  const anchor = new Date(ev.anchor_date + 'T00:00:00Z');
  const today = new Date(utcDateKey(new Date()) + 'T00:00:00Z');
  const step = ev.interval_days * 86400000;
  let k = Math.max(1, Math.ceil((today.getTime() - anchor.getTime()) / step));
  const until = ev.until_date ? new Date(ev.until_date + 'T00:00:00Z') : null;
  while (out.length < count) {
    const d = new Date(anchor.getTime() + k * step);
    if (until && d > until) break;
    out.push(utcDateKey(d));
    k += 1;
  }
  return out;
}

function startEditEvent(ev) {
  if (ev.recurrence_kind !== 'interval_days') {
    openEventForm({ mode: 'edit', event: ev });
    return;
  }
  SCOPE_EVENT = ev;
  document.getElementById('scopeEventName').textContent = ev.name;
  document.getElementById('scopeAll').checked = true;
  const dates = upcomingOccurrenceDates(ev, 26);
  const followingRadio = document.getElementById('scopeFollowing');
  followingRadio.disabled = dates.length === 0;
  document.getElementById('scopeFollowingHelp').textContent = dates.length
    ? ''
    : 'There is no later occurrence to start from, so this choice is not available.';
  document.getElementById('scopeFollowingDate').innerHTML = optionsHtml(dates.map((d) => ({ value: d, label: d })), dates[0]);
  syncScopeChoice();
  openModalById('scopeModal');
}

function syncScopeChoice() {
  const following = document.getElementById('scopeFollowing').checked;
  document.getElementById('scopeFollowingPick').classList.toggle('hidden', !following);
}

function closeScopeModal() {
  closeModalById('scopeModal');
}

document.querySelectorAll('input[name="editScope"]').forEach((r) => r.addEventListener('change', syncScopeChoice));

document.getElementById('btnScopeContinue').addEventListener('click', async () => {
  const ev = SCOPE_EVENT;
  if (!ev) return;
  const choice = document.querySelector('input[name="editScope"]:checked').value;
  closeScopeModal();
  if (choice === 'all') {
    openEventForm({ mode: 'edit', event: ev });
  } else if (choice === 'following') {
    openEventForm({ mode: 'split', event: ev, splitFrom: document.getElementById('scopeFollowingDate').value });
  } else {
    openOccurrenceModal({ eventId: ev.id });
  }
});

// ── Event form ───────────────────────────────────────────────

const EVF = {
  mode: 'create',        // create | edit | split
  event: null,           // the event being edited or split, or the source of a duplicate
  splitFrom: null,       // YYYY-MM-DD, split mode only
  touched: new Set(),    // fields the user changed, so a type's defaults never overwrite them
  composer: null,
  reminders: null,
  types: [],
  aud: new Map(),        // slug -> audience row state
  audiencesAll: [],      // every Audience of the Kingdom (spec §68)
  audChecked: new Map(), // Audience id -> ticked, only for ones the user touched or the event stored
  cover: { original: '', current: '' },
  saving: false,
};


function ownerSlugValue() {
  return byId('evOwner').value;
}

function ownerTenant() {
  return tenantBySlug(ownerSlugValue());
}

function eventStartDate() {
  const d = byId('evAnchor').value;
  const t = normalizeTime(byId('evStart').value);
  if (!d || !t) return null;
  const dt = new Date(`${d}T${t}:00Z`);
  return Number.isNaN(dt.getTime()) ? null : dt;
}

// "9:30" -> "09:30"; anything else that is not a 24-hour time -> ''.
function normalizeTime(value) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(String(value || '').trim());
  if (!m) return '';
  const h = parseInt(m[1], 10);
  const mi = parseInt(m[2], 10);
  if (h > 23 || mi > 59) return '';
  return String(h).padStart(2, '0') + ':' + String(mi).padStart(2, '0');
}

function showFormErrors(errors) {
  const box = byId('evErrors');
  document.querySelectorAll('#eventForm [aria-invalid="true"]').forEach((n) => n.removeAttribute('aria-invalid'));
  if (!errors.length) {
    box.classList.add('hidden');
    box.innerHTML = '';
    return;
  }
  box.innerHTML = '<p class="form-errors__title">Please fix the following:</p><ul>'
    + errors.map((x) => `<li>${escapeHtml(x.msg)}</li>`).join('') + '</ul>';
  box.classList.remove('hidden');
  errors.forEach((x) => { if (x.field && byId(x.field)) byId(x.field).setAttribute('aria-invalid', 'true'); });
  const first = errors.find((x) => x.field && byId(x.field));
  if (first) byId(first.field).focus();
  else box.scrollIntoView({ block: 'nearest' });
}

function syncCalendarGroup() {
  byId('evDurationGroup').classList.toggle('hidden', !byId('evHasCalendar').checked);
}

function syncRecurrenceGroups() {
  const repeats = byId('evRecurrence').value === 'interval_days';
  byId('evIntervalGroup').classList.toggle('hidden', !repeats);
  byId('evUntilGroup').classList.toggle('hidden', !repeats);
}

function syncScopeGroups() {
  const kingdomWide = byId('evScope').value === 'kingdom-wide';
  byId('evKingdomNote').classList.toggle('hidden', !kingdomWide);
  byId('evOwnerLabel').textContent = kingdomWide ? 'Anchor alliance' : 'Owning alliance';
  byId('evAudienceLegend').textContent = kingdomWide ? 'Per-alliance messages' : 'Alliances';
  byId('evAudienceIntro').textContent = kingdomWide
    ? 'Every alliance in the Kingdom takes part. You can still give an alliance its own message.'
    : 'Pick the alliances that take part. The owning alliance is always included.';
  renderAudience();
}

function syncLeadershipNote() {
  byId('evLeadershipNote').classList.toggle('hidden', !byId('evLeadershipOnly').checked);
}

function setCoverPreview(dataUri) {
  EVF.cover.current = dataUri || '';
  const img = byId('evCoverPreview');
  if (dataUri) {
    img.src = dataUri;
    img.classList.remove('hidden');
    byId('btnRemoveCover').classList.remove('hidden');
  } else {
    img.removeAttribute('src');
    img.classList.add('hidden');
    byId('btnRemoveCover').classList.add('hidden');
  }
}

function applyTypeDefaults() {
  const type = EVF.types.find((t) => String(t.id) === byId('evType').value);
  if (!type) return;
  if (!EVF.touched.has('message')) EVF.composer.setValue(type.default_message || '');
  if (!EVF.touched.has('calendar')) {
    byId('evHasCalendar').checked = type.default_duration_hours != null;
    byId('evDuration').value = type.default_duration_hours != null ? type.default_duration_hours : '';
    syncCalendarGroup();
  }
  if (!EVF.touched.has('recurrence')) {
    const repeats = type.default_interval_days != null;
    byId('evRecurrence').value = repeats ? 'interval_days' : 'none';
    byId('evInterval').value = repeats ? type.default_interval_days : '';
    syncRecurrenceGroups();
  }
  if (!EVF.touched.has('reminders')) EVF.reminders.setValue(type.default_reminder_minutes || []);
  if (!EVF.touched.has('mention')) byId('evMentionRole').checked = !!type.default_mention_role;
}

// ── Audience rows ────────────────────────────────────────────

function audienceTenants() {
  const owner = ownerTenant();
  const kingdomId = owner ? owner.kingdom_id : (TENANTS[0] ? TENANTS[0].kingdom_id : null);
  return TENANTS.filter((t) => t.kingdom_id === kingdomId);
}

function newAudRow(slug) {
  return { slug, included: false, open: false, message: '', composer: null };
}

// Reads the live message widget back into the row state before the list is
// rebuilt or saved.
function syncAudRow(row) {
  if (row.composer) row.message = row.composer.value();
}

function audRowHasOverrides(row) {
  return !!row.message.trim();
}

function renderAudience() {
  const list = byId('evAudienceList');
  const kingdomWide = byId('evScope').value === 'kingdom-wide';
  const ownerSlug = ownerSlugValue();
  const tenants = audienceTenants();
  tenants.forEach((t) => { if (!EVF.aud.has(t.slug)) EVF.aud.set(t.slug, newAudRow(t.slug)); });
  EVF.aud.forEach((row) => {
    syncAudRow(row);
    // The widget is about to be rebuilt; its value now lives in the row state.
    row.composer = null;
  });
  list.innerHTML = tenants.map((t) => {
    const row = EVF.aud.get(t.slug);
    const isOwner = t.slug === ownerSlug;
    const included = isOwner || row.included;
    const count = row.message.trim() ? 1 : 0;
    const checkbox = kingdomWide
      ? ''
      : `<input type="checkbox" id="audInc_${escapeHtml(t.slug)}" data-slug="${escapeHtml(t.slug)}" ${included ? 'checked' : ''} ${isOwner ? 'disabled' : ''}>`;
    const name = kingdomWide
      ? `<span class="audience__name">${escapeHtml(t.name)}</span>`
      : `<label class="audience__name" for="audInc_${escapeHtml(t.slug)}">${escapeHtml(t.name)}${isOwner ? ' <span class="samaya-muted">(owner, always included)</span>' : ''}</label>`;
    return `<li class="audience__item" data-slug="${escapeHtml(t.slug)}">
      <div class="audience__head">${checkbox}${name}</div>
      <details class="audience__overrides" data-slug="${escapeHtml(t.slug)}" ${row.open ? 'open' : ''}>
        <summary>Own message for ${escapeHtml(t.name)}${count ? ' (set)' : ''}</summary>
        <div class="audience__body" id="audBody_${escapeHtml(t.slug)}"></div>
      </details>
    </li>`;
  }).join('');
  list.querySelectorAll('details.audience__overrides').forEach((d) => {
    const slug = d.dataset.slug;
    if (d.open) mountAudRow(EVF.aud.get(slug));
    d.addEventListener('toggle', () => {
      const row = EVF.aud.get(slug);
      row.open = d.open;
      if (d.open && !row.composer) mountAudRow(row);
    });
  });
  const unknown = EVF.event ? EVF.event.alliances.filter((a) => !tenantById(a.tenant_id)).length : 0;
  const note = byId('evAudienceNote');
  note.textContent = unknown
    ? `This event also includes ${unknown} alliance${unknown === 1 ? '' : 's'} you cannot see. Changing the audience here removes ${unknown === 1 ? 'it' : 'them'}.`
    : '';
  note.classList.toggle('hidden', !unknown);
  renderEventAudiences();
}

function mountAudRow(row) {
  const prefix = 'evOv' + row.slug.replace(/[^a-zA-Z0-9]/g, '');
  const body = byId('audBody_' + row.slug);
  if (!body) return;
  body.innerHTML = `<div id="${prefix}MsgHost"></div>`;
  row.composer = createComposer(byId(prefix + 'MsgHost'), {
    idPrefix: prefix + 'Msg', label: 'Message for this alliance', value: row.message, rows: 4,
    helper: 'Leave empty to use the event message. Two alliances that share a channel must not have different messages.',
    previewSlug: () => row.slug,
    eventStart: eventStartDate,
  });
  row.composer.setPreviewSlug(row.slug);
}

byId('evAudienceList').addEventListener('change', (e) => {
  const box = e.target.closest('input[type="checkbox"][data-slug]');
  if (!box) return;
  EVF.aud.get(box.dataset.slug).included = box.checked;
  renderEventAudiences();
});
byId('evAudienceList').addEventListener('input', () => schedulePreview());

// The audience rows to send. An alliance event lists every included alliance
// (the owner always); a Kingdom-wide event only lists alliances with their own
// message.
function collectAudience(scope) {
  const ownerSlug = ownerSlugValue();
  const out = [];
  audienceTenants().forEach((t) => {
    const row = EVF.aud.get(t.slug);
    if (!row) return;
    syncAudRow(row);
    const included = t.slug === ownerSlug || row.included;
    const has = audRowHasOverrides(row);
    if (scope === 'kingdom-wide' ? !has : !included) return;
    out.push({ tenant_slug: t.slug, message_override: row.message.trim() || null });
  });
  return out;
}

// ── Destinations and groups (spec §67) ───────────────────────

// Alliances whose destinations the event can use: everyone in a Kingdom-wide
// event, otherwise the owner and the ticked alliances.
function audienceTenantIds() {
  const kingdomWide = byId('evScope').value === 'kingdom-wide';
  const ownerSlug = ownerSlugValue();
  return audienceTenants()
    .filter((t) => kingdomWide || t.slug === ownerSlug || (EVF.aud.get(t.slug) && EVF.aud.get(t.slug).included))
    .map((t) => t.id);
}

// An Audience is on by default when an alliance taking part in the event
// uses it by default.
function audienceDefault(a) {
  const ids = new Set(audienceTenantIds());
  return a.links.some((l) => ids.has(l.tenant_id) && l.post_by_default);
}

function audienceChecked(a) {
  return EVF.audChecked.has(a.id) ? EVF.audChecked.get(a.id) : audienceDefault(a);
}

// Only Audiences an alliance in the event uses can be chosen.
function audienceChoices() {
  const ids = new Set(audienceTenantIds());
  return EVF.audiencesAll.filter((a) => a.links.some((l) => ids.has(l.tenant_id)));
}

// A leadership event only posts to leadership-only audiences, and the
// reverse, so the other kind is shown but cannot be ticked.
function audienceUsable(a) {
  return a.leadership_only === byId('evLeadershipOnly').checked;
}

function renderEventAudiences() {
  const host = byId('evAudiencePanel');
  if (!host) return;
  const slug = ownerSlugValue();
  const choices = audienceChoices();
  host.innerHTML = choices.length
    ? `<ul class="dest-list">${choices.map((a) => {
      const usable = audienceUsable(a);
      const note = usable ? '' : `<span class="samaya-muted">${a.leadership_only ? 'Leadership events only' : 'Not used by a leadership event'}</span>`;
      return `<li class="dest-list__item dest-list__item--stack">
        <label class="check"><input type="checkbox" data-audience="${a.id}" ${usable && audienceChecked(a) ? 'checked' : ''} ${usable ? '' : 'disabled'}> <strong>${escapeHtml(a.label)}</strong></label>
        <span class="dest-list__destinations">${a.destinations.map((d) => destinationHtml(d, slug)).join(' ')}</span>
        ${note}
        <span class="samaya-muted" id="evAudShare${a.id}"></span>
      </li>`;
    }).join('')}</ul>`
    : '<p class="samaya-muted">None of the selected alliances uses an audience yet. Choose one in Setup.</p>';
  resolveDestinationNames(host);
  schedulePreview();
}

byId('evAudiencePanel').addEventListener('change', (e) => {
  const box = e.target.closest('input[data-audience]');
  if (!box) return;
  EVF.audChecked.set(parseInt(box.dataset.audience, 10), box.checked);
  schedulePreview();
});

// The Audience rows that differ from each Audience's default, plus any rows
// for Audiences this user cannot see (kept so a save does not drop them).
function collectAudienceChanges() {
  const visible = new Set(EVF.audiencesAll.map((a) => a.id));
  const out = [];
  audienceChoices().filter(audienceUsable).forEach((a) => {
    const on = audienceChecked(a);
    if (on !== audienceDefault(a)) out.push({ audience_id: a.id, included: on });
  });
  (EVF.event ? EVF.event.audience_changes : []).forEach((c) => {
    if (!visible.has(c.audience_id)) out.push({ audience_id: c.audience_id, included: c.included });
  });
  return out.sort((a, b) => a.audience_id - b.audience_id);
}

let PREVIEW_TIMER = null;
let PREVIEW_SEQ = 0;

function schedulePreview() {
  if (PREVIEW_TIMER) clearTimeout(PREVIEW_TIMER);
  PREVIEW_TIMER = setTimeout(runPreview, 300);
}

async function runPreview() {
  PREVIEW_TIMER = null;
  if (!byId('eventModal').classList.contains('open')) return;
  const seq = ++PREVIEW_SEQ;
  const scope = byId('evScope').value;
  const body = {
    scope, leadership_only: byId('evLeadershipOnly').checked, message: EVF.composer ? EVF.composer.value() : '',
    alliances: collectAudience(scope), audience_changes: collectAudienceChanges(),
  };
  try {
    const res = await api('POST', '/api/events/preview-destinations', body, false, ownerSlugValue());
    if (seq === PREVIEW_SEQ) renderPreview(res);
  } catch (e) {
    if (seq === PREVIEW_SEQ) renderPreview({ audiences: [], destination_count: 0, channel_count: 0, conflicts: [], problems: [e.message], dropped: [] });
  }
}

function renderPreview(res) {
  const n = res.audiences.length;
  byId('evDestSummary').textContent = n
    ? `Posts to ${n} audience${n === 1 ? '' : 's'} in ${res.channel_count} channel${res.channel_count === 1 ? '' : 's'}.`
    : 'Posts nowhere yet.';
  const issues = res.problems.concat(res.conflicts);
  const box = byId('evDestProblems');
  box.innerHTML = issues.map((m) => `<p class="dest-warning" role="alert">${escapeHtml(m)}</p>`).join('');
  box.classList.toggle('hidden', !issues.length);
  EVF.audiencesAll.forEach((a) => { const el = byId('evAudShare' + a.id); if (el) el.textContent = ''; });
  res.audiences.forEach((a) => {
    const el = byId('evAudShare' + a.id);
    if (el && a.shares_channel_with.length) el.textContent = `Same channel as ${a.shares_channel_with.join(', ')}: one message, roles combined.`;
  });
}

// ── Open the form ────────────────────────────────────────────

// ── Notify me and I'm in (spec §87) ──────────────────────────
function syncSignupLeadership() {
  const leadership = byId('evLeadershipOnly').checked;
  ['evSignupEnabled', 'evSignupMention', 'evRsvpEnabled', 'evSignupGroup'].forEach((id) => { byId(id).disabled = leadership; });
  if (leadership) ['evSignupEnabled', 'evSignupMention', 'evRsvpEnabled'].forEach((id) => { byId(id).checked = false; });
}

async function setupSignupFields(event, duplicate, mode) {
  const source = event || null;
  byId('evSignupEnabled').checked = source ? !!source.signup_enabled : true;
  byId('evSignupMention').checked = source ? !!source.signup_mention : false;
  byId('evRsvpEnabled').checked = source ? !!source.rsvp_enabled : true;
  const wanted = source ? source.signup_group_id : null;
  byId('evSignupGroup').innerHTML = groupSelectHtml(wanted);
  syncSignupLeadership();
  const host = byId('evSignupRoles');
  host.innerHTML = '';
  const showRoles = mode === 'edit' && !duplicate && !!event;
  byId('evSignupRolesNew').classList.toggle('hidden', showRoles || mode === 'split');
  const slug = event && !duplicate ? writeSlugForEvent(event) : ownerSlugValue();
  try {
    await loadSignupGroups(slug);
    byId('evSignupGroup').innerHTML = groupSelectHtml(wanted);
    syncSignupLeadership();
  } catch (e) {
    toast(e.message, true);
  }
  if (showRoles) createEventSignupRoles(host, event, canWriteEvent(event));
}

async function openEventForm({ mode, event, splitFrom, duplicate }) {
  EVF.mode = mode;
  EVF.event = event || null;
  EVF.splitFrom = splitFrom || null;
  EVF.aud = new Map();
  EVF.touched = new Set();
  EVF.audChecked = new Map();
  if (event) event.audience_changes.forEach((c) => EVF.audChecked.set(c.audience_id, c.included));
  const editing = mode === 'edit' || mode === 'split';
  if (editing || duplicate) ['message', 'calendar', 'recurrence', 'reminders', 'mention'].forEach((f) => EVF.touched.add(f));

  showFormErrors([]);
  byId('eventForm').reset();
  byId('eventModalTitle').textContent = editing ? 'Edit event' : (duplicate ? 'Duplicate event' : 'New event');
  const banner = byId('evBanner');
  if (mode === 'split') {
    banner.textContent = `This change applies from ${splitFrom} onward. The current series ends the day before, and a new series starts on ${splitFrom} with your changes. Earlier dates stay as they are.`;
  } else if (mode === 'edit' && event.recurrence_kind === 'interval_days') {
    banner.textContent = 'This change applies to every occurrence. Dates that already happened stay as history; upcoming reminders and Discord events are rebuilt.';
  } else {
    banner.textContent = '';
  }
  banner.classList.toggle('hidden', !banner.textContent);
  byId('btnSaveEvent').textContent = mode === 'split' ? 'Split and save' : (editing ? 'Save changes' : 'Create event');

  // Owner. An existing event keeps its owner; a new one picks among the
  // alliances the caller can write for.
  const ownerSelect = byId('evOwner');
  if (editing) {
    const owner = tenantById(event.owning_tenant_id);
    ownerSelect.innerHTML = optionsHtml([{ value: owner ? owner.slug : writeSlugForEvent(event), label: owner ? owner.name : tenantName(event.owning_tenant_id) }], '');
    ownerSelect.disabled = true;
  } else {
    const filter = getTabFilter('events');
    const writable = writableTenants();
    const defaultOwner = (duplicate && tenantById(event.owning_tenant_id) && canWriteTenant(tenantById(event.owning_tenant_id)))
      ? tenantById(event.owning_tenant_id).slug
      : (writable.find((t) => t.slug === filter) || writable[0] || {}).slug;
    ownerSelect.innerHTML = optionsHtml(writable.map((t) => ({ value: t.slug, label: t.name })), defaultOwner);
    ownerSelect.disabled = false;
  }

  try {
    EVF.types = await loadEventTypesFor(ownerSlugValue());
    await loadAudienceChoices();
  } catch (e) {
    toast('Could not load the form: ' + e.message, true);
    return;
  }
  byId('evType').innerHTML = optionsHtml(EVF.types.map((t) => ({ value: t.id, label: t.name })),
    event ? event.type_id : (EVF.types[0] && EVF.types[0].id));

  EVF.composer = createComposer(byId('evMessageHost'), {
    idPrefix: 'evMsg', label: 'Message', value: event ? event.message : '', rows: 6,
    helper: 'Posted at each reminder that has no message of its own (set those under Reminders). If empty, a reminder says "1 hour until Event Name" (and "Event Name is starting now" at the start).',
    previewSlug: ownerSlugValue,
    eventStart: eventStartDate,
  });
  EVF.composer.setPreviewSlug(ownerSlugValue());
  byId('evMsgBody').addEventListener('input', () => EVF.touched.add('message'));

  EVF.reminders = createReminderEditor(byId('evReminderHost'), {
    idPrefix: 'evRem', value: event ? event.reminder_minutes : [],
    withMessages: true, messages: event ? event.reminder_messages : {},
    onChange: () => EVF.touched.add('reminders'),
  });

  // Plain fields.
  byId('evName').value = event ? (duplicate ? event.name + ' (copy)' : event.name) : '';
  byId('evLocation').value = event ? (event.location || '') : '';
  byId('evAnchor').value = event ? event.anchor_date : utcDateKey(new Date());
  byId('evStart').value = event ? event.start_time_utc : '';
  byId('evHasCalendar').checked = event ? event.has_calendar_entry : false;
  byId('evDuration').value = event && event.duration_hours != null ? event.duration_hours : '';
  byId('evRecurrence').value = event && event.recurrence_kind === 'interval_days' ? 'interval_days' : 'none';
  byId('evInterval').value = event && event.interval_days ? event.interval_days : '';
  byId('evUntil').value = event && event.until_date ? event.until_date : '';
  byId('evScope').value = event ? event.scope : 'alliance';
  byId('evMentionRole').checked = event ? event.mention_role : false;
  byId('evCleanUp').checked = event ? !!event.clean_up_reminders : false;
  byId('evLeadershipOnly').checked = event ? event.leadership_only : false;
  setupSignupFields(event, duplicate, mode);
  byId('evCoverFile').value = '';
  EVF.cover.original = event && !duplicate ? (event.cover_image_data || '') : '';
  setCoverPreview(event ? (event.cover_image_data || '') : '');
  if (mode === 'split') {
    byId('evAnchor').value = splitFrom;
    byId('evAnchor').disabled = true;
    byId('evAnchorHelp').textContent = 'Fixed to the date this change starts from.';
  } else {
    byId('evAnchor').disabled = false;
    byId('evAnchorHelp').textContent = 'The first (or only) date. Repeats count from here.';
  }
  // A series cannot be turned into a one-off while splitting it, and a one-off cannot split.
  byId('evScope').disabled = false;

  // Audience state from the event's own rows.
  if (event) {
    event.alliances.forEach((a) => {
      const t = tenantById(a.tenant_id);
      if (!t) return;
      const row = newAudRow(t.slug);
      row.included = true;
      row.message = a.message_override || '';
      row.open = audRowHasOverrides(row);
      EVF.aud.set(t.slug, row);
    });
  }

  syncCalendarGroup();
  syncRecurrenceGroups();
  syncLeadershipNote();
  syncScopeGroups();
  if (!editing && !duplicate) applyTypeDefaults();
  openModalById('eventModal');
}

async function loadAudienceChoices() {
  EVF.audiencesAll = await api('GET', '/api/audiences', null, false, ownerSlugValue());
}

function closeEventModal() {
  closeModalById('eventModal');
}

// Wire the static form controls once.
byId('evType').addEventListener('change', applyTypeDefaults);
byId('evOwner').addEventListener('change', async () => {
  const slug = ownerSlugValue();
  EVF.composer.setPreviewSlug(slug);
  try {
    const types = await loadEventTypesFor(slug);
    const keep = byId('evType').value;
    EVF.types = types;
    byId('evType').innerHTML = optionsHtml(types.map((t) => ({ value: t.id, label: t.name })), keep);
    applyTypeDefaults();
    await loadAudienceChoices();
  } catch (e) { toast('Could not load event types: ' + e.message, true); }
  EVF.audChecked = new Map();
  renderAudience();
});
byId('evScope').addEventListener('change', syncScopeGroups);
byId('evHasCalendar').addEventListener('change', () => { EVF.touched.add('calendar'); syncCalendarGroup(); });
byId('evDuration').addEventListener('input', () => EVF.touched.add('calendar'));
byId('evRecurrence').addEventListener('change', () => { EVF.touched.add('recurrence'); syncRecurrenceGroups(); });
byId('evInterval').addEventListener('input', () => EVF.touched.add('recurrence'));
byId('evMentionRole').addEventListener('change', () => EVF.touched.add('mention'));
byId('evLeadershipOnly').addEventListener('change', () => { syncLeadershipNote(); syncSignupLeadership(); renderEventAudiences(); });
['evAnchor', 'evStart'].forEach((id) => byId(id).addEventListener('input', () => {
  if (EVF.composer) EVF.composer.refresh();
}));
byId('evCoverFile').addEventListener('change', async (e) => {
  const dataUri = await readImageFile(e.target.files && e.target.files[0], 8 * 1024 * 1024);
  if (dataUri) setCoverPreview(dataUri);
  else e.target.value = '';
});
byId('btnRemoveCover').addEventListener('click', () => { setCoverPreview(''); byId('evCoverFile').value = ''; });
byId('eventForm').addEventListener('input', (e) => { if (e.target.removeAttribute) e.target.removeAttribute('aria-invalid'); });
byId('eventForm').addEventListener('submit', (e) => { e.preventDefault(); saveEventForm(); });
byId('btnSaveEvent').addEventListener('click', saveEventForm);

// ── Read, validate, diff, save ───────────────────────────────

// Returns the form's values, or null after showing what is wrong.
function collectEventForm() {
  const errors = [];
  const name = byId('evName').value.trim();
  if (!name) errors.push({ field: 'evName', msg: 'Give the event a name.' });
  const typeId = parseInt(byId('evType').value, 10);
  if (Number.isNaN(typeId)) errors.push({ field: 'evType', msg: 'Pick an event type.' });
  const anchor = byId('evAnchor').value;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(anchor)) errors.push({ field: 'evAnchor', msg: 'Enter the first date as YYYY-MM-DD.' });
  const start = normalizeTime(byId('evStart').value);
  if (!start) errors.push({ field: 'evStart', msg: 'Enter the start time in UTC as 24-hour HH:MM, for example 19:00.' });

  const hasCalendar = byId('evHasCalendar').checked;
  let duration = null;
  if (hasCalendar) {
    duration = parseFloat(byId('evDuration').value);
    if (!(duration > 0)) errors.push({ field: 'evDuration', msg: 'A calendar entry needs a duration greater than 0 hours.' });
  }
  const repeats = byId('evRecurrence').value === 'interval_days';
  let interval = null;
  let until = null;
  if (repeats) {
    interval = parseInt(byId('evInterval').value, 10);
    if (!(interval >= 1)) errors.push({ field: 'evInterval', msg: 'A repeating event needs an interval of at least 1 day.' });
    until = byId('evUntil').value || null;
    if (until && /^\d{4}-\d{2}-\d{2}$/.test(anchor) && until < anchor) {
      errors.push({ field: 'evUntil', msg: 'The end date cannot be before the first date.' });
    }
  }
  const scope = byId('evScope').value;
  const audience = collectAudience(scope);
  const tooLong = audience.find((a) => a.message_override && a.message_override.length > COMPOSER_MAX_CHARS);
  if (tooLong) errors.push({ msg: `The message for ${tooLong.tenant_slug} is longer than ${COMPOSER_MAX_CHARS} characters.` });

  if (errors.length) { showFormErrors(errors); return null; }
  showFormErrors([]);
  return {
    type_id: typeId, name, location: byId('evLocation').value.trim(), scope,
    leadership_only: byId('evLeadershipOnly').checked, mention_role: byId('evMentionRole').checked,
    clean_up_reminders: byId('evCleanUp').checked,
    signup_enabled: byId('evSignupEnabled').checked, signup_mention: byId('evSignupMention').checked,
    rsvp_enabled: byId('evRsvpEnabled').checked, signup_group_id: byId('evSignupGroup').value ? parseInt(byId('evSignupGroup').value, 10) : null,
    message: EVF.composer.value(), anchor_date: anchor, start_time_utc: start,
    duration_hours: duration, recurrence_kind: repeats ? 'interval_days' : 'none',
    interval_days: interval, until_date: until, reminder_minutes: EVF.reminders.value(),
    reminder_messages: EVF.reminders.messages(), alliances: audience, audience_changes: collectAudienceChanges(),
    ownerSlug: ownerSlugValue(),
  };
}

function audienceKey(rows) {
  return JSON.stringify(rows.map((r) => [r.tenantId, r.message || '']).sort());
}

function origAudienceKey(ev, scope) {
  const rows = ev.alliances.map((a) => ({ tenantId: a.tenant_id, message: a.message_override }))
    .filter((r) => scope !== 'kingdom-wide' || r.message);
  return audienceKey(rows);
}

function formAudienceKey(f) {
  const rows = f.alliances.map((a) => {
    const t = tenantBySlug(a.tenant_slug);
    return { tenantId: t ? t.id : 0, message: a.message_override };
  });
  return audienceKey(rows);
}

// The EventPatch fields that differ from the event as loaded.
function buildEventChanges(orig, f, skipAnchor) {
  const c = {};
  if (f.type_id !== orig.type_id) c.type_id = f.type_id;
  if (f.name !== orig.name) c.name = f.name;
  if (f.location !== (orig.location || '')) c.location = f.location;
  if (f.scope !== orig.scope) c.scope = f.scope;
  if (f.leadership_only !== orig.leadership_only) c.leadership_only = f.leadership_only;
  if (f.mention_role !== orig.mention_role) c.mention_role = f.mention_role;
  if (f.clean_up_reminders !== !!orig.clean_up_reminders) c.clean_up_reminders = f.clean_up_reminders;
  ['signup_enabled', 'signup_mention', 'rsvp_enabled'].forEach((k) => { if (f[k] !== !!orig[k]) c[k] = f[k]; });
  if (f.signup_group_id !== (orig.signup_group_id == null ? null : orig.signup_group_id)) c.signup_group_id = f.signup_group_id;
  if (f.message !== (orig.message || '')) c.message = f.message;
  if (!skipAnchor && f.anchor_date !== orig.anchor_date) c.anchor_date = f.anchor_date;
  if (f.start_time_utc !== orig.start_time_utc) c.start_time_utc = f.start_time_utc;
  if (f.duration_hours !== orig.duration_hours) c.duration_hours = f.duration_hours;
  if (f.recurrence_kind !== orig.recurrence_kind || f.interval_days !== orig.interval_days) {
    c.recurrence_kind = f.recurrence_kind;
    c.interval_days = f.interval_days;
  }
  if ((f.until_date || null) !== (orig.until_date || null)) c.until_date = f.until_date;
  if (JSON.stringify(f.reminder_minutes) !== JSON.stringify(orig.reminder_minutes)) c.reminder_minutes = f.reminder_minutes;
  if (JSON.stringify(f.reminder_messages) !== JSON.stringify(orig.reminder_messages || {})) c.reminder_messages = f.reminder_messages;
  if (formAudienceKey(f) !== origAudienceKey(orig, f.scope)) c.alliances = f.alliances;
  if (JSON.stringify(f.audience_changes) !== JSON.stringify(orig.audience_changes)) c.audience_changes = f.audience_changes;
  if (EVF.cover.current !== (orig.cover_image_data || '')) c.cover_image_data = EVF.cover.current;
  return c;
}

async function saveEventForm() {
  if (EVF.saving) return;
  const f = collectEventForm();
  if (!f) return;
  const saveBtn = byId('btnSaveEvent');
  EVF.saving = true;
  saveBtn.disabled = true;
  try {
    if (EVF.mode === 'create') {
      const payload = {
        type_id: f.type_id, name: f.name, scope: f.scope, leadership_only: f.leadership_only,
        start_time_utc: f.start_time_utc, anchor_date: f.anchor_date, location: f.location,
        message: f.message, duration_hours: f.duration_hours, recurrence_kind: f.recurrence_kind,
        interval_days: f.interval_days, until_date: f.until_date, mention_role: f.mention_role, clean_up_reminders: f.clean_up_reminders,
        signup_enabled: f.signup_enabled, signup_mention: f.signup_mention, rsvp_enabled: f.rsvp_enabled, signup_group_id: f.signup_group_id,
        reminder_minutes: f.reminder_minutes, reminder_messages: f.reminder_messages,
        alliances: f.alliances, audience_changes: f.audience_changes,
      };
      if (EVF.cover.current) payload.cover_image_data = EVF.cover.current;
      await api('POST', '/api/events', payload, false, f.ownerSlug);
      toast('Event created.');
    } else if (EVF.mode === 'edit') {
      const changes = buildEventChanges(EVF.event, f, false);
      if (!Object.keys(changes).length) { toast('Nothing was changed.'); closeEventModal(); return; }
      const res = await api('PATCH', `/api/events/${EVF.event.id}`, changes, false, writeSlugForEvent(EVF.event));
      toastDiscordErrors(res.discord_errors, 'Changes saved to every occurrence.');
    } else {
      const changes = buildEventChanges(EVF.event, f, true);
      if (!Object.keys(changes).length) {
        showFormErrors([{ msg: 'Change at least one field. Otherwise there is nothing to split.' }]);
        return;
      }
      const res = await api('POST', `/api/events/${EVF.event.id}/split`,
        { from_date: EVF.splitFrom, changes }, false, writeSlugForEvent(EVF.event));
      toastDiscordErrors(res.discord_errors, `Series split. The new version starts on ${EVF.splitFrom}.`);
    }
    closeEventModal();
    invalidateEventTypes();
    loadEvents();
  } catch (e) {
    showFormErrors([{ msg: e.message }]);
    toast(e.message, true);
  } finally {
    EVF.saving = false;
    saveBtn.disabled = false;
  }
}

VIEW_LOADERS.events = loadEvents;
