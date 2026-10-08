// Schedule tab (#v-schedule, spec §66.7): upcoming occurrences as a table and
// as a day-by-day timeline, with per-occurrence actions (cancel, restore, move,
// change the message). The occurrence form also serves the Events tab's "this
// occurrence only" edit scope (spec §66.4a). Depends on common.js and
// composer.js.

let SCHED_OCC = [];
let SCHED_EVENTS = {};       // event id -> event dict, for owner and start time
let SCHED_RANGE_FROM = '';
let SCHED_RANGE_DAYS = 28;

const SCHED_RANGES = [7, 14, 28, 56];

function getScheduleLayout() {
  let v = null;
  try { v = localStorage.getItem('samaya_schedule_layout'); } catch { /* storage blocked */ }
  return v === 'timeline' ? 'timeline' : 'table';
}

function setScheduleLayout(layout) {
  try { localStorage.setItem('samaya_schedule_layout', layout); } catch { /* storage blocked */ }
  byId('scheduleTableWrap').classList.toggle('hidden', layout !== 'table');
  byId('scheduleTimelineWrap').classList.toggle('hidden', layout !== 'timeline');
  byId('scheduleLayoutTable').setAttribute('aria-pressed', String(layout === 'table'));
  byId('scheduleLayoutTimeline').setAttribute('aria-pressed', String(layout === 'timeline'));
}

function getScheduleDays() {
  let v = null;
  try { v = parseInt(localStorage.getItem('samaya_schedule_days'), 10); } catch { /* storage blocked */ }
  return SCHED_RANGES.includes(v) ? v : 28;
}

function addDays(dateKey, n) {
  const d = new Date(dateKey + 'T00:00:00Z');
  d.setUTCDate(d.getUTCDate() + n);
  return utcDateKey(d);
}

async function loadSchedule() {
  renderAllianceFilterSelect('scheduleAlliance', 'schedule', loadSchedule);
  SCHED_RANGE_DAYS = getScheduleDays();
  byId('scheduleRange').innerHTML = optionsHtml(SCHED_RANGES.map((d) => ({ value: d, label: `Next ${d} days` })), SCHED_RANGE_DAYS);
  setScheduleLayout(getScheduleLayout());
  SCHED_RANGE_FROM = utcDateKey(new Date());
  const to = addDays(SCHED_RANGE_FROM, SCHED_RANGE_DAYS - 1);
  const filter = getTabFilter('schedule');
  try {
    const [occs, events] = await Promise.all([
      api('GET', `/api/occurrences?from=${SCHED_RANGE_FROM}&to=${to}`, null, false, filter),
      api('GET', '/api/events', null, false, filter),
    ]);
    SCHED_OCC = occs;
    SCHED_EVENTS = {};
    events.forEach((ev) => { SCHED_EVENTS[ev.id] = ev; });
    renderSchedule();
  } catch (e) {
    toast(e.message, true);
    byId('scheduleBody').innerHTML = emptyRow(7, 'Could not load the schedule: ' + e.message);
  }
}

function occWritable(occ) {
  const ev = SCHED_EVENTS[occ.event_id];
  return ev ? canWriteEvent(ev) : canWriteAnywhere();
}

function occWriteSlug(occ) {
  const ev = SCHED_EVENTS[occ.event_id];
  return ev ? writeSlugForEvent(ev) : (writableTenants()[0] || TENANTS[0] || {}).slug;
}

// One chip per audience alliance (a Kingdom-wide event lists them all), with
// the destination count and any warning from the engine (spec §67.6).
function occAudienceHtml(occ) {
  const ev = SCHED_EVENTS[occ.event_id];
  const ids = occ.audience_tenant_ids || (ev ? [ev.owning_tenant_id] : []);
  const chips = ids.map((id) => `<span class="audience-tag">${escapeHtml(tenantName(id))}</span>`).join(' ');
  const kingdom = ev && ev.scope === 'kingdom-wide' ? `${pfLabel('Kingdom-wide', 'pf-m-purple')} ` : '';
  return `<div class="audience-chips">${kingdom}${chips}</div>${destinationBadgeHtml(occ)}`;
}

const DELIVERY_STATUS_COLOR = {
  pending: 'pf-m-gray', sending: 'pf-m-blue', posted: 'pf-m-green', error: 'pf-m-red', cancelled: 'pf-m-orange',
};

function deliveryCountsHtml(counts) {
  const order = ['error', 'sending', 'pending', 'posted', 'cancelled'];
  const parts = order.filter((s) => counts && counts[s]).map((s) => pfLabel(`${counts[s]} ${s}`, DELIVERY_STATUS_COLOR[s]));
  return parts.length ? `<div class="label-stack">${parts.join(' ')}</div>` : '<span class="samaya-muted">None yet</span>';
}

function occStatusHtml(occ) {
  const labels = [];
  if (occ.status === 'cancelled') labels.push(pfLabel('Cancelled', 'pf-m-red'));
  else labels.push(pfLabel('Scheduled', 'pf-m-gray'));
  if (occ.is_moved) labels.push(pfLabel('Moved', 'pf-m-blue'));
  if (occ.message_override) labels.push(pfLabel('Own message', 'pf-m-purple'));
  if (occ.leadership_only) labels.push(pfLabel('Leadership only', 'pf-m-orange'));
  if (occ.rsvp_enabled && occ.rsvp_count > 0) labels.push(pfLabel(`${occ.rsvp_count} in`, 'pf-m-green'));
  return `<div class="label-stack">${labels.join(' ')}</div>`;
}

// "2026-10-03 02:00 UTC · Oct 2, 10:00 PM EDT" becomes two lines.
function whenHtml(iso) {
  const parts = fmtDateTime(iso).split(' · ');
  return `<div class="sched">${escapeHtml(parts[0])}</div>${parts[1] ? `<div class="samaya-muted">${escapeHtml(parts.slice(1).join(' · '))}</div>` : ''}`;
}

function buildScheduleRow(occ) {
  const cancelled = occ.status === 'cancelled';
  let actions = '<span class="samaya-muted">Read only</span>';
  if (occWritable(occ)) {
    actions = `<div class="row-actions">
      <button type="button" class="pf-v6-c-button pf-m-secondary pf-m-small" data-action="edit" data-id="${occ.id}" title="Move this date or give it its own message">Move or edit</button>
      ${cancelled
        ? `<button type="button" class="pf-v6-c-button pf-m-secondary pf-m-small" data-action="restore" data-id="${occ.id}">Restore</button>`
        : `<details class="menu"><summary class="menu__btn" aria-label="More actions for ${escapeHtml(occ.event_name)}">&#8943;</summary><div class="menu__panel"><button type="button" class="menu__item menu__item--danger" data-action="cancel" data-id="${occ.id}">Cancel this date&hellip;</button></div></details>`}
    </div>`;
  }
  return `<tr class="pf-v6-c-table__tr${cancelled ? ' is-cancelled' : ''}">
    <td class="pf-v6-c-table__td" data-label="When" data-sort="${escapeHtml(occ.start_datetime_utc)}">${whenHtml(occ.start_datetime_utc)}</td>
    <td class="pf-v6-c-table__td" data-label="Event"><strong>${escapeHtml(occ.event_name)}</strong><div>${typeChip(occ.type)}</div></td>
    <td class="pf-v6-c-table__td" data-label="Alliance">${occAudienceHtml(occ)}</td>
    <td class="pf-v6-c-table__td" data-label="Status">${occStatusHtml(occ)}</td>
    <td class="pf-v6-c-table__td" data-label="Deliveries">${deliveryCountsHtml(occ.delivery_counts)}</td>
    <td class="pf-v6-c-table__td" data-label="Actions">${actions}</td>
  </tr>`;
}

function renderSchedule() {
  const body = byId('scheduleBody');
  body.innerHTML = SCHED_OCC.length
    ? SCHED_OCC.map(buildScheduleRow).join('')
    : emptyRow(6, 'Nothing is scheduled in this range.');
  applyTypeColors(body);
  const to = addDays(SCHED_RANGE_FROM, SCHED_RANGE_DAYS - 1);
  byId('scheduleSummary').textContent = `${SCHED_OCC.length} occurrence${SCHED_OCC.length === 1 ? '' : 's'} from ${SCHED_RANGE_FROM} to ${to}. Occurrences are generated about four weeks ahead.`;
  renderTimeline();
}

// ── Timeline ─────────────────────────────────────────────────

function renderTimeline() {
  const host = byId('scheduleTimeline');
  if (!SCHED_OCC.length) {
    host.innerHTML = '<p class="samaya-empty">Nothing is scheduled in this range.</p>';
    return;
  }
  const dates = [];
  for (let i = 0; i < SCHED_RANGE_DAYS; i += 1) dates.push(addDays(SCHED_RANGE_FROM, i));
  const today = utcDateKey(new Date());
  const groups = new Map();
  SCHED_OCC.forEach((occ) => {
    if (!groups.has(occ.event_id)) groups.set(occ.event_id, { name: occ.event_name, type: occ.type, occs: [] });
    groups.get(occ.event_id).occs.push(occ);
  });
  const head = dates.map((d, i) => {
    const dt = new Date(d + 'T00:00:00Z');
    const dow = new Intl.DateTimeFormat('en-US', { weekday: 'short', timeZone: 'UTC' }).format(dt);
    const day = dt.getUTCDate();
    const month = (i === 0 || day === 1) ? new Intl.DateTimeFormat('en-US', { month: 'short', timeZone: 'UTC' }).format(dt) : '';
    const cls = ['timeline__day'];
    if (d === today) cls.push('is-today');
    if (dt.getUTCDay() === 0 || dt.getUTCDay() === 6) cls.push('is-weekend');
    return `<th scope="col" class="${cls.join(' ')}"><span class="timeline__month">${month}</span><span class="timeline__dow">${dow}</span><span class="timeline__num">${day}</span></th>`;
  }).join('');
  const rows = Array.from(groups.values()).map((g) => {
    const cells = dates.map((d) => {
      const marks = g.occs.filter((o) => utcDateKey(toUtcDate(o.start_datetime_utc)) === d).map((o) => {
        const time = utcTimeKey(toUtcDate(o.start_datetime_utc));
        const cancelled = o.status === 'cancelled';
        const cls = ['timeline__mark'];
        if (cancelled) cls.push('is-cancelled');
        if (o.is_moved) cls.push('is-moved');
        const state = cancelled ? 'cancelled' : (o.is_moved ? 'moved' : 'scheduled');
        const warned = (o.warnings || []).length > 0;
        const label = `${o.event_name} on ${d} at ${time} UTC, ${state}${warned ? ', has a warning' : ''}`;
        const attrs = occWritable(o) ? `data-action="edit" data-id="${o.id}"` : 'disabled';
        return `<button type="button" class="${cls.join(' ')}" data-accent="${escapeHtml(g.type ? g.type.color : '#475569')}" ${attrs} aria-label="${escapeHtml(label)}" title="${escapeHtml(label)}">${time}${warned ? ' !' : ''}</button>`;
      }).join('');
      const cls = d === today ? ' class="is-today"' : '';
      return `<td${cls}>${marks}</td>`;
    }).join('');
    return `<tr><th scope="row" class="timeline__event">${typeChip(g.type)} ${escapeHtml(g.name)}</th>${cells}</tr>`;
  }).join('');
  host.innerHTML = `<table class="timeline">
    <caption class="sr-only">Occurrences by day, times in UTC. Activate a time to move it or change its message.</caption>
    <thead><tr><th scope="col" class="timeline__corner">Event</th>${head}</tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
  applyTypeColors(host);
}

// ── Row actions ──────────────────────────────────────────────

async function patchOccurrence(occ, payload, doneMessage) {
  const res = await api('PATCH', `/api/occurrences/${occ.id}`, payload, false, occWriteSlug(occ));
  toastDiscordErrors(res.discord_errors, doneMessage);
  return res;
}

const SCHED_ACTIONS = {
  async cancel(btn) {
    const occ = SCHED_OCC.find((o) => o.id === parseInt(btn.dataset.id, 10));
    if (!occ) return;
    if (!confirm(`Cancel "${occ.event_name}" on ${occ.occurrence_date}? Its pending reminders are cancelled and its Discord event, if one exists, is removed. The rest of the series is not affected, and you can restore it later.`)) return;
    try { await patchOccurrence(occ, { cancelled: true }, 'Occurrence cancelled.'); loadSchedule(); } catch (e) { toast(e.message, true); }
  },
  async restore(btn) {
    const occ = SCHED_OCC.find((o) => o.id === parseInt(btn.dataset.id, 10));
    if (!occ) return;
    try { await patchOccurrence(occ, { cancelled: false }, 'Occurrence restored.'); loadSchedule(); } catch (e) { toast(e.message, true); }
  },
  edit(btn) {
    const occ = SCHED_OCC.find((o) => o.id === parseInt(btn.dataset.id, 10));
    if (occ) openOccurrenceModal({ occurrence: occ });
  },
};

bindActions(byId('scheduleBody'), SCHED_ACTIONS);
bindActions(byId('scheduleTimeline'), SCHED_ACTIONS);
byId('scheduleRange').addEventListener('change', (e) => {
  try { localStorage.setItem('samaya_schedule_days', e.target.value); } catch { /* storage blocked */ }
  loadSchedule();
});
byId('scheduleLayoutTable').addEventListener('click', () => setScheduleLayout('table'));
byId('scheduleLayoutTimeline').addEventListener('click', () => setScheduleLayout('timeline'));

// ── Occurrence form (this occurrence only) ───────────────────

const OCC = {
  list: [],          // occurrences the picker offers
  current: null,     // the selected occurrence dict
  event: null,       // its event dict (for the original start time and owner)
  composer: null,
  resetMove: false,
  saving: false,
};

function occOriginalStart(occ, ev) {
  return ev ? `${occ.occurrence_date}T${ev.start_time_utc}:00Z` : occ.start_datetime_utc;
}

function occMoveStartDate() {
  const d = byId('occMoveDate').value;
  const t = normalizeTime(byId('occMoveTime').value);
  if (!d || !t) return null;
  const dt = new Date(`${d}T${t}:00Z`);
  return Number.isNaN(dt.getTime()) ? null : dt;
}

function showOccErrors(messages) {
  const box = byId('occErrors');
  box.innerHTML = messages.length
    ? '<ul>' + messages.map((m) => `<li>${escapeHtml(m)}</li>`).join('') + '</ul>'
    : '';
  box.classList.toggle('hidden', !messages.length);
}

function syncOccMoveEnabled() {
  const cancelled = byId('occCancelled').checked;
  byId('occMoveDate').disabled = cancelled;
  byId('occMoveTime').disabled = cancelled;
  byId('btnOccResetMove').disabled = cancelled;
}

function fillOccurrenceFields(occ) {
  OCC.current = occ;
  OCC.event = (typeof SCHED_EVENTS !== 'undefined' && SCHED_EVENTS[occ.event_id]) || eventById(occ.event_id) || null;
  OCC.resetMove = false;
  const start = toUtcDate(occ.start_datetime_utc);
  byId('occCancelled').checked = occ.status === 'cancelled';
  byId('occMoveDate').value = utcDateKey(start);
  byId('occMoveTime').value = utcTimeKey(start);
  OCC.composer.setValue(occ.message_override || '');
  const bits = [`Normally ${fmtDateTime(occOriginalStart(occ, OCC.event))}`];
  if (occ.is_moved) bits.push('currently moved');
  byId('occInfo').textContent = bits.join(', ') + '.';
  syncOccMoveEnabled();
  showOccErrors([]);
}

// opts: { occurrence } (from the Schedule) or { eventId } (from the Events
// tab's edit scope: the picker lists that event's upcoming occurrences).
async function openOccurrenceModal(opts) {
  let list;
  let eventDict;
  if (opts.occurrence) {
    list = [opts.occurrence];
    eventDict = SCHED_EVENTS[opts.occurrence.event_id] || eventById(opts.occurrence.event_id);
  } else {
    eventDict = eventById(opts.eventId);
    const from = utcDateKey(new Date());
    try {
      const all = await api('GET', `/api/occurrences?from=${from}&to=${addDays(from, 56)}`, null, false, writeSlugForEvent(eventDict));
      list = all.filter((o) => o.event_id === opts.eventId);
    } catch (e) { toast(e.message, true); return; }
    if (!list.length) {
      toast('This event has no generated occurrences in the next eight weeks, so there is nothing to change one at a time.', true);
      return;
    }
  }
  OCC.list = list;
  byId('occEvent').textContent = list[0].event_name;
  byId('occPick').innerHTML = optionsHtml(list.map((o) => ({
    value: o.id,
    label: `${fmtDateTime(o.start_datetime_utc)}${o.status === 'cancelled' ? ' (cancelled)' : ''}`,
  })), list[0].id);
  byId('occPick').disabled = list.length === 1;
  const ownerSlug = eventDict ? writeSlugForEvent(eventDict) : (TENANTS[0] || {}).slug;
  OCC.composer = createComposer(byId('occMessageHost'), {
    idPrefix: 'occMsg', label: 'Message for this occurrence only', value: '', rows: 4,
    helper: 'Replaces the event message for this date. Leave empty to use the event message.',
    previewSlug: () => ownerSlug,
    eventStart: occMoveStartDate,
  });
  OCC.composer.setPreviewSlug(ownerSlug);
  fillOccurrenceFields(list[0]);
  openModalById('occurrenceModal');
}

function closeOccurrenceModal() {
  closeModalById('occurrenceModal');
}

byId('occPick').addEventListener('change', (e) => {
  const occ = OCC.list.find((o) => o.id === parseInt(e.target.value, 10));
  if (occ) fillOccurrenceFields(occ);
});
byId('occCancelled').addEventListener('change', syncOccMoveEnabled);
['occMoveDate', 'occMoveTime'].forEach((id) => byId(id).addEventListener('input', () => {
  OCC.resetMove = false;
  if (OCC.composer) OCC.composer.refresh();
}));
byId('btnOccResetMove').addEventListener('click', () => {
  const original = toUtcDate(occOriginalStart(OCC.current, OCC.event));
  byId('occMoveDate').value = utcDateKey(original);
  byId('occMoveTime').value = utcTimeKey(original);
  OCC.resetMove = true;
});
byId('btnOccClearMessage').addEventListener('click', () => OCC.composer.setValue(''));

byId('btnSaveOccurrence').addEventListener('click', async () => {
  if (OCC.saving || !OCC.current) return;
  const occ = OCC.current;
  const payload = {};
  const nowCancelled = byId('occCancelled').checked;
  const wasCancelled = occ.status === 'cancelled';
  if (nowCancelled !== wasCancelled) payload.cancelled = nowCancelled;
  if (!nowCancelled) {
    if (OCC.resetMove) {
      if (occ.is_moved) payload.clear_move = true;
    } else {
      const start = occMoveStartDate();
      if (!start) { showOccErrors(['Enter the new date and a 24-hour UTC time such as 19:00, or leave them as they are.']); return; }
      if (start.getTime() !== toUtcDate(occ.start_datetime_utc).getTime()) payload.start_datetime_utc = start.toISOString();
    }
  }
  const message = OCC.composer.value().trim();
  if (message !== (occ.message_override || '')) payload.message_override = message;
  if (!Object.keys(payload).length) { toast('Nothing was changed.'); closeOccurrenceModal(); return; }
  OCC.saving = true;
  byId('btnSaveOccurrence').disabled = true;
  try {
    await patchOccurrence({ id: occ.id, event_id: occ.event_id }, payload, 'Occurrence updated.');
    closeOccurrenceModal();
    if (byId('v-schedule').classList.contains('active')) loadSchedule();
  } catch (e) {
    showOccErrors([e.message]);
  } finally {
    OCC.saving = false;
    byId('btnSaveOccurrence').disabled = false;
  }
});

VIEW_LOADERS.schedule = loadSchedule;
