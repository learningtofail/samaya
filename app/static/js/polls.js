// Polls tab (#v-polls, spec §84): propose UTC times, players vote with buttons in
// Discord, the leader reads the counts and applies a winner. State lives in POLLS;
// render*() functions draw from it. Counts only, never voters. Depends on common.js.

const POLLS = { rows: [], audiences: [], busy: false };
const POLL_MIN_SLOTS = 2;
const POLL_MAX_SLOTS = 6;
const POLL_WHEN = new Intl.DateTimeFormat('en-GB', {
  weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC',
});

function pollWhen(iso) { return POLL_WHEN.format(new Date(iso)) + ' UTC'; }

function pollStatusLabel(status) {
  const colors = { open: 'pf-m-green', closed: 'pf-m-blue', cancelled: 'pf-m-grey' };
  return pfLabel(status.charAt(0).toUpperCase() + status.slice(1), colors[status] || 'pf-m-grey');
}

async function loadPolls() {
  renderAllianceFilterSelect('pollsAlliance', 'polls', loadPolls);
  byId('pollsStatus').onchange = loadPolls;
  const status = byId('pollsStatus').value;
  try {
    POLLS.rows = await api('GET', '/api/time-polls' + (status ? '?status=' + status : ''), null, false, getTabFilter('polls'));
    renderPolls();
  } catch (e) {
    toast(e.message, true);
    byId('pollsCount').textContent = 'Could not load polls: ' + e.message;
    byId('pollsList').innerHTML = '';
  }
}

// ── List ─────────────────────────────────────────────────────
function pollSlotHtml(poll, slot, max) {
  const canWrite = pollCanWrite(poll);
  const mark = poll.status === 'closed' && poll.winner_slot_id === slot.id ? ' ' + pfLabel('Chosen', 'pf-m-green') : '';
  const lead = slot.leading && poll.total_votes ? ' ' + pfLabel('Most votes', 'pf-m-blue') : '';
  let action = '';
  if (canWrite && poll.status === 'open') {
    action = `<button type="button" class="pf-v6-c-button pf-m-secondary pf-m-small" data-action="close-with" data-id="${poll.id}" data-slot="${slot.id}">Close with this time</button>`;
  } else if (canWrite && poll.status === 'closed') {
    action = poll.occurrence_id
      ? `<button type="button" class="pf-v6-c-button pf-m-secondary pf-m-small" data-action="move-occurrence" data-id="${poll.id}" data-slot="${slot.id}">Move occurrence here</button>`
      : `<button type="button" class="pf-v6-c-button pf-m-secondary pf-m-small" data-action="new-event" data-id="${poll.id}" data-slot="${slot.id}">New event at this time</button>`;
  }
  return `<li class="poll__slot">
    <span class="poll__when">${escapeHtml(pollWhen(slot.starts_at))}${mark}${lead}</span>
    <progress class="poll__bar" max="${Math.max(max, 1)}" value="${slot.votes}" aria-label="Votes for ${escapeHtml(pollWhen(slot.starts_at))}"></progress>
    <span class="poll__votes"><strong>${slot.votes}</strong> ${slot.votes === 1 ? 'vote' : 'votes'}</span>
    <span class="poll__action">${action}</span>
  </li>`;
}

function pollCanWrite(poll) {
  if (poll.scope === 'kingdom-wide') return writableTenants().length > 0;
  return canWriteTenant(tenantBySlug(poll.alliance));
}

function pollCardHtml(poll) {
  const max = Math.max(0, ...poll.slots.map((s) => s.votes));
  const errors = poll.messages.filter((m) => m.error)
    .map((m) => `<p class="poll__error samaya-muted">Message in channel ${escapeHtml(m.channel_id)}: ${escapeHtml(m.error)}</p>`).join('');
  const closes = poll.status === 'open'
    ? `Closes ${escapeHtml(pollWhen(poll.closes_at))}`
    : `${poll.status === 'closed' ? 'Closed' : 'Cancelled'} ${escapeHtml(pollWhen(poll.closed_at || poll.closes_at))}`;
  const where = poll.scope === 'kingdom-wide' ? 'Kingdom-wide' : escapeHtml((tenantBySlug(poll.alliance) || {}).name || poll.alliance || '');
  const manage = pollCanWrite(poll) && poll.status === 'open'
    ? `<div class="row-actions">
        <button type="button" class="pf-v6-c-button pf-m-secondary pf-m-small" data-action="close" data-id="${poll.id}">Close poll</button>
        <button type="button" class="pf-v6-c-button pf-m-link pf-m-danger pf-m-small" data-action="cancel" data-id="${poll.id}">Cancel poll</button>
      </div>` : '';
  return `<section class="panel poll" aria-labelledby="poll-${poll.id}-title">
    <div class="poll__head">
      <h3 class="panel__title" id="poll-${poll.id}-title">${escapeHtml(poll.title)}</h3>
      ${pollStatusLabel(poll.status)}
    </div>
    <p class="samaya-muted">${where} · ${closes} · ${poll.total_votes} ${poll.total_votes === 1 ? 'vote' : 'votes'} in total</p>
    <ul class="poll__slots">${poll.slots.map((s) => pollSlotHtml(poll, s, max)).join('')}</ul>
    ${errors}${manage}
  </section>`;
}

function renderPolls() {
  const n = POLLS.rows.length;
  byId('pollsCount').textContent = n ? `${n} poll${n === 1 ? '' : 's'}` : 'No polls here yet.';
  byId('pollsList').innerHTML = POLLS.rows.map(pollCardHtml).join('');
}

function pollById(id) { return POLLS.rows.find((p) => String(p.id) === String(id)); }
function pollWriteSlug(poll) {
  if (poll.alliance) return poll.alliance;
  const any = writableTenants()[0];
  return any ? any.slug : '';
}

async function pollPost(poll, action, body, doneMessage) {
  if (POLLS.busy) return;
  POLLS.busy = true;
  try {
    await api('POST', `/api/time-polls/${poll.id}/${action}`, body, false, pollWriteSlug(poll));
    toast(doneMessage);
    await loadPolls();
  } catch (e) {
    toast(e.message, true);
  } finally {
    POLLS.busy = false;
  }
}

async function moveOccurrenceToSlot(poll, slot) {
  if (!confirm(`Move the linked occurrence to ${pollWhen(slot.starts_at)}? Its reminders move with it.`)) return;
  try {
    const res = await api('PATCH', `/api/occurrences/${poll.occurrence_id}`, { start_datetime_utc: slot.starts_at }, false, pollWriteSlug(poll));
    toastDiscordErrors(res.discord_errors, 'Occurrence moved');
  } catch (e) {
    toast(e.message, true);
  }
}

async function newEventAtSlot(slot) {
  showView('events');
  await openEventForm({ mode: 'create' });
  const when = new Date(slot.starts_at);
  byId('evAnchor').value = when.toISOString().slice(0, 10);
  byId('evStart').value = when.toISOString().slice(11, 16);
}

bindActions(byId('pollsList'), {
  close: (el) => {
    const poll = pollById(el.dataset.id);
    if (poll && confirm('Close this poll? The buttons are removed and the final tally is posted. Nothing changes on the schedule.')) pollPost(poll, 'close', {}, 'Poll closed');
  },
  'close-with': (el) => {
    const poll = pollById(el.dataset.id);
    const slot = poll && poll.slots.find((s) => String(s.id) === el.dataset.slot);
    if (slot && confirm(`Close the poll with ${pollWhen(slot.starts_at)} as the chosen time? Nothing changes on the schedule yet.`)) {
      pollPost(poll, 'close', { winner_slot_id: slot.id }, 'Poll closed');
    }
  },
  cancel: (el) => {
    const poll = pollById(el.dataset.id);
    if (poll && confirm('Cancel this poll? The buttons are removed and the votes are kept for 90 days but not counted for anything.')) pollPost(poll, 'cancel', undefined, 'Poll cancelled');
  },
  'move-occurrence': (el) => {
    const poll = pollById(el.dataset.id);
    const slot = poll && poll.slots.find((s) => String(s.id) === el.dataset.slot);
    if (slot) moveOccurrenceToSlot(poll, slot);
  },
  'new-event': (el) => {
    const poll = pollById(el.dataset.id);
    const slot = poll && poll.slots.find((s) => String(s.id) === el.dataset.slot);
    if (slot) newEventAtSlot(slot);
  },
});

// ── New poll form ────────────────────────────────────────────
function pollSlotInputs() { return Array.from(byId('pollSlots').querySelectorAll('input[type="datetime-local"]')); }

function pollAddSlot(value) {
  const rows = pollSlotInputs().length;
  if (rows >= POLL_MAX_SLOTS) return;
  const wrap = document.createElement('div');
  wrap.className = 'poll-slots__row';
  const input = document.createElement('input');
  input.type = 'datetime-local';
  input.className = 'pf-v6-c-form-control';
  input.setAttribute('aria-label', `Time ${rows + 1} (UTC)`);
  if (value) input.value = value;
  const remove = document.createElement('button');
  remove.type = 'button';
  remove.className = 'pf-v6-c-button pf-m-link pf-m-small';
  remove.textContent = 'Remove';
  remove.addEventListener('click', () => { wrap.remove(); syncPollSlotButtons(); });
  wrap.append(input, remove);
  byId('pollSlots').appendChild(wrap);
  syncPollSlotButtons();
}

function syncPollSlotButtons() {
  const n = pollSlotInputs().length;
  byId('btnAddPollSlot').disabled = n >= POLL_MAX_SLOTS;
  byId('pollSlots').querySelectorAll('button').forEach((b) => { b.disabled = n <= POLL_MIN_SLOTS; });
}

function pollLocalValue(iso) { return new Date(iso).toISOString().slice(0, 16); }

async function loadPollAudiences() {
  const slug = byId('pollAlliance').value;
  const box = byId('pollAudiences');
  const legend = box.querySelector('legend');
  box.innerHTML = '';
  box.appendChild(legend);
  if (!slug) return;
  try {
    const all = await api('GET', '/api/audiences', null, false, slug);
    const tenant = tenantBySlug(slug);
    POLLS.audiences = all.filter((a) => a.links.some((l) => tenant && l.tenant_id === tenant.id));
  } catch (e) {
    POLLS.audiences = [];
    toast(e.message, true);
  }
  box.insertAdjacentHTML('beforeend', POLLS.audiences.length
    ? POLLS.audiences.map((a) => `<label class="check"><input type="checkbox" value="${a.id}"> ${escapeHtml(a.label)}</label>`).join('')
    : '<p class="samaya-muted">This alliance has no audiences to post to. Add one in Setup first.</p>');
}

function openPollForm() {
  byId('pollForm').reset();
  byId('pollSlots').innerHTML = '';
  pollAddSlot();
  pollAddSlot();
  const writable = writableTenants();
  const current = getTabFilter('polls');
  byId('pollAlliance').innerHTML = optionsHtml(writable.map((t) => ({ value: t.slug, label: t.name })), writable.some((t) => t.slug === current) ? current : (writable[0] || {}).slug);
  byId('pollAlliance').onchange = loadPollAudiences;
  showPollError([]);
  openModalById('pollModal');
  loadPollAudiences();
}

function closePollModal() { closeModalById('pollModal'); }

function showPollError(list) {
  const box = byId('pollError');
  box.classList.toggle('hidden', !list.length);
  box.textContent = list.join(' ');
}

async function suggestPollSlots() {
  const slug = byId('pollAlliance').value;
  try {
    const found = await api('GET', '/api/analytics/free-slots?duration=60&days=7&from_hour=12&to_hour=23', null, false, slug);
    if (!found.slots.length) { showPollError(['No free slots found in the next 7 days between 12:00 and 23:00 UTC.']); return; }
    showPollError([]);
    byId('pollSlots').innerHTML = '';
    found.slots.slice(0, 4).forEach((s) => pollAddSlot(pollLocalValue(s.start)));
    while (pollSlotInputs().length < POLL_MIN_SLOTS) pollAddSlot();
  } catch (e) {
    showPollError([e.message]);
  }
}

async function savePoll() {
  const slots = pollSlotInputs().map((i) => i.value).filter(Boolean).map((v) => v + ':00Z');
  const audienceIds = Array.from(byId('pollAudiences').querySelectorAll('input:checked')).map((i) => Number(i.value));
  const problems = [];
  if (!byId('pollTitle').value.trim()) problems.push('Give the poll a question.');
  if (slots.length < POLL_MIN_SLOTS) problems.push('Add at least two times.');
  if (new Set(slots).size !== slots.length) problems.push('Each time must be different.');
  if (!audienceIds.length) problems.push('Choose at least one audience to post to.');
  showPollError(problems);
  if (problems.length || POLLS.busy) return;
  POLLS.busy = true;
  const button = byId('btnSavePoll');
  button.disabled = true;
  button.textContent = 'Posting...';
  try {
    const res = await api('POST', '/api/time-polls', {
      title: byId('pollTitle').value.trim(), slots, audience_ids: audienceIds, closes_in_hours: Number(byId('pollCloses').value),
    }, false, byId('pollAlliance').value);
    closePollModal();
    toastDiscordErrors(res.discord_errors, 'Poll posted');
    await loadPolls();
  } catch (e) {
    showPollError([e.message]);
  } finally {
    POLLS.busy = false;
    button.disabled = false;
    button.textContent = 'Post poll';
  }
}

byId('btnNewPoll').addEventListener('click', openPollForm);
byId('btnAddPollSlot').addEventListener('click', () => pollAddSlot());
byId('btnSuggestSlots').addEventListener('click', suggestPollSlots);
byId('btnSavePoll').addEventListener('click', savePoll);

VIEW_LOADERS.polls = loadPolls;
