// Delivery log tab (#v-delivery, spec §66.7 and §67.3): a health card for the
// delivery engine and a filterable table of every Discord post it has made or
// tried to make, with the reason on each row and Retry for ones that failed.
// One line is one send. When several alliances' destinations shared a channel
// (or a Discord event) the engine sent once, and the line lists the merged
// destinations underneath.
// Depends on common.js.

let UPCOMING_DELIVERIES = [];
let PAST_DELIVERIES = [];
const OPEN_DELIVERY_STATES = ['pending', 'sending'];

const DELIVERY_KIND_LABELS = { discord_event: 'Discord event', reminder: 'Reminder', announcement: 'Message' };

function deliveryKindText(d) {
  if (d.kind === 'reminder' && d.reminder_minutes !== null && d.reminder_minutes !== undefined) {
    return 'Reminder, ' + describeReminder(d.reminder_minutes);
  }
  return DELIVERY_KIND_LABELS[d.kind] || d.kind;
}

async function loadDelivery() {
  renderAllianceFilterSelect('deliveryAlliance', 'delivery', loadDelivery);
  const filter = getTabFilter('delivery');
  const params = new URLSearchParams();
  const status = byId('deliveryStatus').value;
  const kind = byId('deliveryKind').value;
  const eventId = byId('deliveryEvent').value;
  if (status) params.set('status', status);
  if (kind) params.set('kind', kind);
  if (eventId) params.set('event_id', eventId);
  params.set('limit', '200');
  params.set('days', byId('deliveryDays').value || '7');
  const fetchSection = (section) => {
    const wanted = !status || (section === 'upcoming') === OPEN_DELIVERY_STATES.includes(status);
    if (!wanted) return Promise.resolve([]);
    const p = new URLSearchParams(params);
    p.set('section', section);
    return api('GET', '/api/deliveries?' + p.toString(), null, false, filter);
  };
  try {
    const [health, upcoming, past, events] = await Promise.all([
      api('GET', '/api/delivery-health', null, false, filter),
      fetchSection('upcoming'),
      fetchSection('past'),
      api('GET', '/api/events', null, false, filter),
    ]);
    renderDeliveryHealth(health);
    renderDeliveryEventFilter(events, eventId);
    UPCOMING_DELIVERIES = upcoming;
    PAST_DELIVERIES = past;
    renderDeliveries();
  } catch (e) {
    toast(e.message, true);
    const failed = emptyRow(6, 'Could not load the delivery log: ' + e.message);
    byId('deliveryUpcomingBody').innerHTML = failed;
    byId('deliveryPastBody').innerHTML = failed;
  }
}

function renderDeliveryEventFilter(events, selected) {
  const sel = byId('deliveryEvent');
  const options = [{ value: '', label: 'Any event' }].concat(
    events.slice().sort((a, b) => a.name.localeCompare(b.name)).map((e) => ({ value: e.id, label: e.name })));
  sel.innerHTML = optionsHtml(options, selected);
}

function renderDeliveryHealth(h) {
  const counts = h.counts || {};
  const order = [['posted', 'Posted', 'pf-m-green'], ['pending', 'Pending', 'pf-m-gray'], ['sending', 'Sending', 'pf-m-blue'],
    ['error', 'Errors', 'pf-m-red'], ['cancelled', 'Cancelled', 'pf-m-orange']];
  const overdue = h.oldest_pending_overdue_minutes || 0;
  byId('deliveryHealth').innerHTML = `
    <div class="health__state ${h.healthy ? 'health__state--ok' : 'health__state--bad'}" role="status">
      ${h.healthy ? 'Healthy' : 'Needs attention'}
    </div>
    <ul class="health__counts" aria-label="Deliveries due in the last ${h.window_days} days">
      ${order.map(([key, label, color]) => `<li class="health__count">${pfLabel(label, color)} <strong>${counts[key] || 0}</strong></li>`).join('')}
    </ul>
    <p class="health__note">Last ${h.window_days} days. ${overdue > 5
    ? `The oldest pending delivery is ${overdue} minutes overdue, which means the engine is not sending.`
    : 'Nothing is waiting longer than it should.'}</p>`;
}

function deliveryStatusLabel(status) {
  const color = { pending: 'pf-m-gray', sending: 'pf-m-blue', posted: 'pf-m-green', error: 'pf-m-red', cancelled: 'pf-m-orange' }[status] || 'pf-m-gray';
  return pfLabel(status, color);
}

function deliveryMembersHtml(d) {
  if (!d.members || !d.members.length) return '';
  const items = d.members.map((m) => `<li>${escapeHtml(m.tenant_name)}${m.destination_label ? `, ${escapeHtml(m.destination_label)}` : ''}: ${escapeHtml(m.status)}${m.detail ? ` (${escapeHtml(m.detail)})` : ''}</li>`).join('');
  return `<details class="delivery-merged"><summary>Merged with ${d.members.length} other destination${d.members.length === 1 ? '' : 's'}</summary><ul class="delivery-members">${items}</ul></details>`;
}

function buildDeliveryRow(d) {
  const canRetry = d.status === 'error' && canWriteAnywhere();
  const action = canRetry
    ? `<button type="button" class="pf-v6-c-button pf-m-secondary pf-m-small" data-action="retry" data-id="${d.id}" data-slug="${escapeHtml(d.tenant_slug)}">Retry</button>`
    : '';
  const merged = d.members && d.members.length;
  const names = (d.alliance_names && d.alliance_names.length ? d.alliance_names : [d.tenant_name]).join(', ');
  const target = d.destination_label ? `<div class="samaya-muted">${escapeHtml(d.destination_label)}</div>` : '';
  return `<tr class="pf-v6-c-table__tr">
    <td class="pf-v6-c-table__td" data-label="Due">${escapeHtml(fmtDateTime(d.due_at_utc))}</td>
    <td class="pf-v6-c-table__td" data-label="Event"><strong>${escapeHtml(d.event_name)}</strong><div class="samaya-muted">${escapeHtml(d.occurrence_date)}</div></td>
    <td class="pf-v6-c-table__td" data-label="Alliance and kind">${escapeHtml(names)}${merged ? ` ${pfLabel('Merged', 'pf-m-blue')}` : ''}${target}<div class="samaya-muted">${escapeHtml(deliveryKindText(d))}</div></td>
    <td class="pf-v6-c-table__td" data-label="Status">${deliveryStatusLabel(d.status)}</td>
    <td class="pf-v6-c-table__td" data-label="Detail">${d.detail ? escapeHtml(d.detail) : '<span class="samaya-muted">None</span>'}${deliveryMembersHtml(d)}</td>
    <td class="pf-v6-c-table__td" data-label="Actions">${action}</td>
  </tr>`;
}

function renderDeliverySection(rows, bodyId, countId, emptyText, countText) {
  byId(bodyId).innerHTML = rows.length ? rows.map(buildDeliveryRow).join('') : emptyRow(6, emptyText);
  byId(countId).textContent = rows.length ? countText(rows.length) : '';
}

function renderDeliveries() {
  renderDeliverySection(UPCOMING_DELIVERIES, 'deliveryUpcomingBody', 'deliveryUpcomingCount',
    'Nothing is waiting to be sent.', (n) => `${n} shown, soonest first (up to 200).`);
  renderDeliverySection(PAST_DELIVERIES, 'deliveryPastBody', 'deliveryPastCount',
    'No past deliveries match these filters.', (n) => `${n} shown, newest first (up to 200).`);
}

const deliveryActions = {
  async retry(btn) {
    btn.disabled = true;
    try {
      await api('POST', `/api/deliveries/${btn.dataset.id}/retry`, null, false, btn.dataset.slug);
      toast('Retry queued. The engine sends it on its next minute.');
      loadDelivery();
    } catch (e) {
      toast(e.message, true);
      btn.disabled = false;
    }
  },
};
bindActions(byId('deliveryUpcomingBody'), deliveryActions);
bindActions(byId('deliveryPastBody'), deliveryActions);
['deliveryStatus', 'deliveryKind', 'deliveryEvent', 'deliveryDays'].forEach((id) => byId(id).addEventListener('change', loadDelivery));

VIEW_LOADERS.delivery = loadDelivery;
