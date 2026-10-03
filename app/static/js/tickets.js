// Feedback tab (#v-feedback, spec §66.7, §66.10): triage of the public
// /feedback board. Each ticket expands to its details, status, edit, dismiss
// and delete actions, and a thread of PUBLIC responses. Depends on common.js.
//
// GET /admin/api/tickets is Kingdom-wide: it ignores which alliance the
// X-Tenant-Slug header names (the header only proves the caller has access).

let TICKETS = [];
let TAGS = [];
const TICKETF = { editing: null, dragId: null };
const MAX_TAGS_PER_TICKET = 8;
let ticketTagFilter = '';
const NOTE_DRAFTS = {}; // unsaved internal notes by ticket id, so a re-render does not lose typing

const TICKET_KIND_META = {
  feedback:             { label: 'Feedback', color: 'pf-m-blue' },
  event_request:        { label: 'Event request', color: 'pf-m-green' },
  announcement_request: { label: 'Announcement request', color: 'pf-m-purple' },
  error:                { label: 'Issue', color: 'pf-m-red' },
};
const TICKET_STATUS_LABELS = {
  open: 'Open', planned: 'Planned', in_progress: 'In progress', done: 'Done', declined: 'Declined', dismissed: 'Dismissed',
};
const TICKET_STATUS_COLORS = {
  open: 'pf-m-gray', planned: 'pf-m-blue', in_progress: 'pf-m-purple', done: 'pf-m-green', declined: 'pf-m-orange', dismissed: 'pf-m-red',
};
const TICKET_ACTIVE = ['open', 'planned', 'in_progress'];
const TICKET_ARCHIVED = ['done', 'declined'];
const TICKET_FILTERS = [
  { value: 'active', label: 'Active' },
  { value: 'archived', label: 'Archived (done and declined)' },
  { value: 'dismissed', label: 'Dismissed' },
  { value: 'all', label: 'Everything' },
  ...Object.keys(TICKET_STATUS_LABELS).map((s) => ({ value: s, label: 'Only ' + TICKET_STATUS_LABELS[s].toLowerCase() })),
];

// Board (spec §76): one column per status, fixed. "dismissed" is not a column.
const BOARD_COLUMNS = ['open', 'planned', 'in_progress', 'done', 'declined'];
const TICKET_VIEW_KEY = 'samaya_ticket_view';
let ticketView = 'list';

function ticketsSlug() {
  return TENANTS[0] ? TENANTS[0].slug : '';
}

function canModerateTickets() {
  return canWriteAnywhere();
}

function ticketMatchesFilter(t, f) {
  if (f === 'all') return true;
  if (f === 'active') return TICKET_ACTIVE.includes(t.status);
  if (f === 'archived') return TICKET_ARCHIVED.includes(t.status);
  return t.status === f;
}

async function loadTickets() {
  const sel = byId('ticketFilter');
  if (!sel.options.length) {
    let saved = 'active';
    try { saved = localStorage.getItem('samaya_ticket_filter') || 'active'; } catch { /* storage blocked */ }
    sel.innerHTML = optionsHtml(TICKET_FILTERS, saved);
  }
  renderAuthorBanner();
  try { ticketView = localStorage.getItem(TICKET_VIEW_KEY) === 'board' ? 'board' : 'list'; } catch { ticketView = 'list'; }
  try {
    [TICKETS, TAGS] = await Promise.all([
      api('GET', '/api/tickets', null, false, ticketsSlug()),
      api('GET', '/api/ticket-tags', null, false, ticketsSlug()),
    ]);
    populateTagFilter();
    renderTagManager();
    byId('btnManageTags').classList.toggle('hidden', !canModerateTickets());
    applyTicketView();
  } catch (e) {
    toast(e.message, true);
    byId('ticketList').innerHTML = `<p class="samaya-empty">Could not load feedback: ${escapeHtml(e.message)}</p>`;
  }
}

function renderAuthorBanner() {
  const name = ME && ME.display_name ? ME.display_name.trim() : '';
  byId('ticketAuthor').innerHTML = name
    ? `Your responses are shown publicly as <strong>${escapeHtml(name)}</strong>. You can change this in <a href="#setup" data-goto="setup">Setup</a>.`
    : 'Your responses are shown publicly as <strong>Team</strong>, because you have not set a display name. Set one in <a href="#setup" data-goto="setup">Setup</a>.';
}

function ticketMeta(t) {
  const kind = TICKET_KIND_META[t.kind] || { label: t.kind, color: 'pf-m-gray' };
  const bits = [pfLabel(kind.label, kind.color), pfLabel(TICKET_STATUS_LABELS[t.status] || t.status, TICKET_STATUS_COLORS[t.status] || 'pf-m-gray')];
  const text = [`${t.upvote_count} upvote${t.upvote_count === 1 ? '' : 's'}`];
  if (t.tenant_name) text.push(t.tenant_name);
  if (t.related_name) text.push('about ' + t.related_name);
  if (t.created_at) text.push(fmtDateTime(t.created_at));
  return `<span class="label-stack">${bits.join(' ')}</span> <span class="samaya-muted">${escapeHtml(text.join(', '))}</span>`;
}

function buildResponseHtml(t, r) {
  const mine = ME && (isSuperadmin() || r.author_user_id === ME.id);
  const actions = mine && canModerateTickets()
    ? `<div class="row-actions">
        <button type="button" class="pf-v6-c-button pf-m-link pf-m-small" data-action="edit-response" data-ticket="${t.id}" data-id="${r.id}">Edit</button>
        <button type="button" class="pf-v6-c-button pf-m-link pf-m-danger pf-m-small" data-action="delete-response" data-ticket="${t.id}" data-id="${r.id}">Delete</button>
      </div>` : '';
  return `<li class="response" id="response-${r.id}">
    <div class="response__head"><strong>${escapeHtml(r.author)}</strong> <span class="samaya-muted">${escapeHtml(fmtDateTime(r.created_at))}${r.updated_at && r.updated_at !== r.created_at ? ' (edited)' : ''}</span></div>
    <p class="response__body">${escapeHtml(r.body)}</p>
    ${actions}
  </li>`;
}

// ── Tags, checklist and internal notes (spec §76.5) ──────────
// Everything here is admin only; none of it reaches the public board.

function tagChip(tag) {
  return `<span class="tag-chip" data-color="${escapeHtml(tag.color)}" data-ink="${escapeHtml(tag.ink)}">${escapeHtml(tag.name)}</span>`;
}

function ticketMatchesTag(t) {
  return !ticketTagFilter || t.tags.some((g) => String(g.id) === ticketTagFilter);
}

function populateTagFilter() {
  if (!TAGS.some((g) => String(g.id) === ticketTagFilter)) ticketTagFilter = '';
  byId('ticketTagFilter').innerHTML = optionsHtml(
    [{ value: '', label: 'All tags' }, ...TAGS.map((g) => ({ value: String(g.id), label: g.name }))], ticketTagFilter);
}

function ticketSection(title, inner) {
  return `<section class="ticket__section" aria-label="${escapeHtml(title)}"><h4 class="ticket__section-title">${escapeHtml(title)}</h4>${inner}</section>`;
}

function tagsHtml(t, moderate) {
  if (!moderate) return ticketSection('Tags', t.tags.length ? `<div class="tag-row">${t.tags.map(tagChip).join('')}</div>` : '<p class="samaya-muted">No tags.</p>');
  if (!TAGS.length) return ticketSection('Tags', '<p class="samaya-muted">No tags exist yet. Use Manage tags above to create one.</p>');
  const attached = new Set(t.tags.map((g) => g.id));
  const options = TAGS.map((g) => {
    const on = attached.has(g.id);
    const locked = !on && attached.size >= MAX_TAGS_PER_TICKET;
    return `<label class="tag-picker__option"><input type="checkbox" data-action-change="tag" data-id="${t.id}" value="${g.id}"${on ? ' checked' : ''}${locked ? ' disabled' : ''}> ${tagChip(g)}</label>`;
  }).join('');
  return ticketSection('Tags', `<fieldset class="tag-picker"><legend class="sr-only">Tags on ${escapeHtml(t.title)} (at most ${MAX_TAGS_PER_TICKET})</legend>${options}</fieldset>`);
}

function checklistHtml(t, moderate) {
  const done = t.checklist.filter((i) => i.done).length;
  const items = t.checklist.map((i) => `<li class="checklist__item">
      <label class="checklist__label"><input type="checkbox" id="ckItem${i.id}" data-action-change="check" data-ticket="${t.id}" data-id="${i.id}"${i.done ? ' checked' : ''}${moderate ? '' : ' disabled'}> <span class="checklist__text${i.done ? ' checklist__text--done' : ''}">${escapeHtml(i.body)}</span></label>
      ${moderate ? `<span class="row-actions">
        <button type="button" class="pf-v6-c-button pf-m-link pf-m-small" data-action="edit-item" data-ticket="${t.id}" data-id="${i.id}">Edit</button>
        <button type="button" class="pf-v6-c-button pf-m-link pf-m-danger pf-m-small" data-action="delete-item" data-ticket="${t.id}" data-id="${i.id}">Delete</button>
      </span>` : ''}
    </li>`).join('');
  const add = moderate
    ? `<form class="checklist-form" data-ticket="${t.id}">
        <label class="sr-only" for="ckNew${t.id}">New checklist item</label>
        <input class="pf-v6-c-form-control" type="text" id="ckNew${t.id}" maxlength="200" placeholder="Add an item">
        <button type="submit" class="pf-v6-c-button pf-m-secondary pf-m-small">Add</button>
      </form>` : '';
  const title = t.checklist.length ? `Checklist (${done}/${t.checklist.length} done)` : 'Checklist';
  return ticketSection(title, `${t.checklist.length ? `<ul class="checklist">${items}</ul>` : '<p class="samaya-muted">No items.</p>'}${add}`);
}

function notesHtml(t, moderate) {
  const note = Object.prototype.hasOwnProperty.call(NOTE_DRAFTS, t.id) ? NOTE_DRAFTS[t.id] : (t.internal_notes || '');
  const intro = '<p class="ticket__private-note">Private to the team. Never shown on the public page.</p>';
  if (!moderate) {
    return ticketSection('Internal notes', intro + (t.internal_notes ? `<div class="notes__rendered">${renderMarkdownSafe(t.internal_notes)}</div>` : '<p class="samaya-muted">No notes.</p>'));
  }
  return ticketSection('Internal notes', `${intro}<form class="notes-form" data-ticket="${t.id}">
      <label class="pf-v6-c-form__label" for="notes${t.id}"><span class="pf-v6-c-form__label-text">Notes (Markdown)</span></label>
      <textarea class="pf-v6-c-form-control" id="notes${t.id}" rows="5" maxlength="5000">${escapeHtml(note)}</textarea>
      <div class="notes__rendered" id="notesPreview${t.id}">${note.trim() ? renderMarkdownSafe(note) : ''}</div>
      <button type="submit" class="pf-v6-c-button pf-m-secondary pf-m-small">Save notes</button>
    </form>`);
}

function buildTicketHtml(t) {
  const moderate = canModerateTickets();
  const statusSelect = moderate
    ? `<div class="ticket__status">
        <label class="pf-v6-c-form__label" for="ticketStatus${t.id}"><span class="pf-v6-c-form__label-text">Status</span></label>
        <select class="pf-v6-c-form-control" id="ticketStatus${t.id}" data-action-change="status" data-id="${t.id}">
          ${optionsHtml(Object.keys(TICKET_STATUS_LABELS).map((s) => ({ value: s, label: TICKET_STATUS_LABELS[s] })), t.status)}
        </select>
      </div>` : '';
  const dismissed = t.status === 'dismissed';
  const buttons = moderate
    ? `<div class="row-actions">
        <button type="button" class="pf-v6-c-button pf-m-secondary pf-m-small" data-action="edit" data-id="${t.id}">Edit</button>
        ${dismissed
    ? `<button type="button" class="pf-v6-c-button pf-m-secondary pf-m-small" data-action="restore" data-id="${t.id}">Restore</button>`
    : `<button type="button" class="pf-v6-c-button pf-m-secondary pf-m-small" data-action="dismiss" data-id="${t.id}">Dismiss</button>`}
        ${isSuperadmin() ? `<details class="menu"><summary class="menu__btn" aria-label="More actions">&#8943;</summary><div class="menu__panel"><button type="button" class="menu__item menu__item--danger" data-action="delete" data-id="${t.id}">Delete&hellip;</button></div></details>` : ''}
      </div>` : '';
  const respond = moderate
    ? `<form class="response-form" data-ticket="${t.id}">
        <label class="pf-v6-c-form__label" for="respBody${t.id}"><span class="pf-v6-c-form__label-text">Add a public response</span></label>
        <textarea class="pf-v6-c-form-control" id="respBody${t.id}" rows="3" maxlength="2000"></textarea>
        <button type="submit" class="pf-v6-c-button pf-m-primary pf-m-small">Post response</button>
      </form>` : '';
  const responses = (t.responses || []);
  return `<details class="ticket" data-ticket-id="${t.id}">
    <summary class="ticket__summary"><span class="ticket__title">${escapeHtml(t.title)}</span> ${ticketMeta(t)}</summary>
    <div class="ticket__body">
      <p class="ticket__desc">${escapeHtml(t.description)}</p>
      <p class="ticket__contact"><strong>Submitter contact:</strong> ${t.submitter_contact ? escapeHtml(t.submitter_contact) : '<span class="samaya-muted">None given</span>'} <span class="samaya-muted">(private, never shown publicly)</span></p>
      ${statusSelect}
      ${buttons}
      ${tagsHtml(t, moderate)}
      ${checklistHtml(t, moderate)}
      ${notesHtml(t, moderate)}
      <section class="ticket__thread" aria-label="Public responses to ${escapeHtml(t.title)}">
        <h4 class="ticket__thread-title">Public responses (${responses.length})</h4>
        <p class="ticket__public-note">Everything written here is shown on the public feedback page, with the author name below.</p>
        <ul class="responses" id="responses${t.id}">${responses.length ? responses.map((r) => buildResponseHtml(t, r)).join('') : '<li class="samaya-muted">No responses yet.</li>'}</ul>
        ${respond}
      </section>
    </div>
  </details>`;
}

function renderTickets() {
  if (ticketView === 'board') renderBoard();
  else renderTicketList();
}

function renderTicketList() {
  const f = byId('ticketFilter').value;
  const openIds = new Set(Array.from(document.querySelectorAll('#ticketList details[open]')).map((d) => d.dataset.ticketId));
  const shown = TICKETS.filter((t) => ticketMatchesFilter(t, f) && ticketMatchesTag(t));
  byId('ticketCount').textContent = `${shown.length} of ${TICKETS.length} ticket${TICKETS.length === 1 ? '' : 's'}`;
  const host = byId('ticketList');
  if (!shown.length) {
    host.innerHTML = '<p class="samaya-empty">No tickets match this filter.</p>';
    return;
  }
  const groups = [
    { title: 'Active', test: (t) => TICKET_ACTIVE.includes(t.status) },
    { title: 'Archived', test: (t) => TICKET_ARCHIVED.includes(t.status) },
    { title: 'Dismissed', test: (t) => t.status === 'dismissed' },
  ];
  host.innerHTML = groups.map((g) => {
    const rows = shown.filter(g.test);
    if (!rows.length) return '';
    return `<section class="ticket-group"><h3 class="ticket-group__title">${g.title} <span class="samaya-muted">${rows.length}</span></h3>${rows.map(buildTicketHtml).join('')}</section>`;
  }).join('');
  host.querySelectorAll('details.ticket').forEach((d) => { if (openIds.has(d.dataset.ticketId)) d.open = true; });
  applyTypeColors(host);
}

function replaceTicket(updated) {
  const i = TICKETS.findIndex((t) => t.id === updated.id);
  if (i >= 0) TICKETS[i] = updated;
  renderTickets();
  const d = document.querySelector(`#ticketList details[data-ticket-id="${updated.id}"]`);
  if (d) d.open = true;
}

async function patchTicket(id, payload, done) {
  try {
    const updated = await api('PATCH', `/api/tickets/${id}`, payload, false, ticketsSlug());
    replaceTicket(updated);
    toast(done);
    return true;
  } catch (e) { toast(e.message, true); return false; }
}

// One write that returns the whole ticket; a failure redraws from state so a
// toggled checkbox does not stay out of step with the server.
async function ticketMutate(method, path, body, done) {
  try {
    const updated = await api(method, path, body, false, ticketsSlug());
    replaceTicket(updated);
    if (done) toast(done);
    return updated;
  } catch (e) {
    toast(e.message, true);
    renderTickets();
    return null;
  }
}

function ticketById(id) {
  return TICKETS.find((t) => t.id === parseInt(id, 10));
}

const TICKET_ACTIONS = {
  dismiss(btn) { patchTicket(parseInt(btn.dataset.id, 10), { status: 'dismissed' }, 'Ticket dismissed. It is hidden from the public board.'); },
  restore(btn) { patchTicket(parseInt(btn.dataset.id, 10), { status: 'open' }, 'Ticket restored as open.'); },
  edit(btn) { const t = ticketById(btn.dataset.id); if (t) openTicketModal(t); },
  async delete(btn) {
    const t = ticketById(btn.dataset.id);
    if (!t) return;
    if (!confirm(`Permanently delete "${t.title}" with its votes and ${t.responses.length} response(s)? This cannot be undone. Dismiss it instead to hide it.`)) return;
    try {
      await api('DELETE', `/api/tickets/${t.id}`, null, false, ticketsSlug());
      TICKETS = TICKETS.filter((x) => x.id !== t.id);
      renderTickets();
      toast('Ticket deleted.');
    } catch (e) { toast(e.message, true); }
  },
  async 'edit-item'(btn) {
    const t = ticketById(btn.dataset.ticket);
    const item = t && t.checklist.find((i) => i.id === parseInt(btn.dataset.id, 10));
    if (!item) return;
    const body = prompt('Edit this checklist item:', item.body);
    if (body === null || !body.trim() || body.trim() === item.body) return;
    await ticketMutate('PATCH', `/api/tickets/${t.id}/checklist/${item.id}`, { body });
  },
  async 'delete-item'(btn) {
    const t = ticketById(btn.dataset.ticket);
    if (!t) return;
    await ticketMutate('DELETE', `/api/tickets/${t.id}/checklist/${btn.dataset.id}`);
  },
  async 'edit-response'(btn) {
    const t = ticketById(btn.dataset.ticket);
    const r = t && t.responses.find((x) => x.id === parseInt(btn.dataset.id, 10));
    if (!r) return;
    const body = prompt('Edit this public response:', r.body);
    if (body === null || !body.trim() || body.trim() === r.body) return;
    try {
      const updated = await api('PATCH', `/api/tickets/${t.id}/responses/${r.id}`, { body }, false, ticketsSlug());
      t.responses = t.responses.map((x) => (x.id === r.id ? updated : x));
      replaceTicket(t);
      toast('Response updated.');
    } catch (e) { toast(e.message, true); }
  },
  async 'delete-response'(btn) {
    const t = ticketById(btn.dataset.ticket);
    if (!t || !confirm('Delete this public response?')) return;
    try {
      await api('DELETE', `/api/tickets/${t.id}/responses/${btn.dataset.id}`, null, false, ticketsSlug());
      t.responses = t.responses.filter((x) => x.id !== parseInt(btn.dataset.id, 10));
      replaceTicket(t);
      toast('Response deleted.');
    } catch (e) { toast(e.message, true); }
  },
};

const ticketList = byId('ticketList');
bindActions(ticketList, TICKET_ACTIONS);
ticketList.addEventListener('change', async (e) => {
  const el = e.target.closest('[data-action-change]');
  if (!el) return;
  const kind = el.dataset.actionChange;
  if (kind === 'status') {
    patchTicket(parseInt(el.dataset.id, 10), { status: el.value }, 'Status updated.');
  } else if (kind === 'check') {
    const updated = await ticketMutate('PATCH', `/api/tickets/${el.dataset.ticket}/checklist/${el.dataset.id}`, { done: el.checked });
    if (updated) { const box = byId('ckItem' + el.dataset.id); if (box) box.focus(); }
  } else if (kind === 'tag') {
    const t = ticketById(el.dataset.id);
    if (!t) return;
    const id = parseInt(el.value, 10);
    const ids = t.tags.map((g) => g.id).filter((x) => x !== id);
    if (el.checked) ids.push(id);
    await ticketMutate('PUT', `/api/tickets/${t.id}/tags`, { tag_ids: ids });
  }
});
ticketList.addEventListener('input', (e) => {
  const ta = e.target.closest('.notes-form textarea');
  if (!ta) return;
  const id = ta.closest('.notes-form').dataset.ticket;
  NOTE_DRAFTS[id] = ta.value;
  byId('notesPreview' + id).innerHTML = ta.value.trim() ? renderMarkdownSafe(ta.value) : '';
});
ticketList.addEventListener('submit', async (e) => {
  const checklistForm = e.target.closest('.checklist-form');
  const notesForm = e.target.closest('.notes-form');
  if (checklistForm || notesForm) {
    e.preventDefault();
    const id = (checklistForm || notesForm).dataset.ticket;
    if (checklistForm) {
      const input = checklistForm.querySelector('input');
      const body = input.value.trim();
      if (!body) { toast('Write the item first.', true); input.focus(); return; }
      if (await ticketMutate('POST', `/api/tickets/${id}/checklist`, { body })) byId('ckNew' + id).focus();
    } else if (await ticketMutate('PATCH', `/api/tickets/${id}`, { internal_notes: notesForm.querySelector('textarea').value }, 'Notes saved. They stay private.')) {
      delete NOTE_DRAFTS[id];
      renderTickets();
    }
    return;
  }
  const form = e.target.closest('.response-form');
  if (!form) return;
  e.preventDefault();
  const t = ticketById(form.dataset.ticket);
  const ta = form.querySelector('textarea');
  const body = ta.value.trim();
  if (!t || !body) { toast('Write a response first.', true); ta.focus(); return; }
  try {
    const created = await api('POST', `/api/tickets/${t.id}/responses`, { body }, false, ticketsSlug());
    t.responses = t.responses.concat([created]);
    replaceTicket(t);
    toast('Response posted publicly.');
  } catch (err) { toast(err.message, true); }
});
byId('v-feedback').addEventListener('click', (e) => {
  const link = e.target.closest('[data-goto]');
  if (!link) return;
  e.preventDefault();
  showView(link.dataset.goto);
});
byId('ticketFilter').addEventListener('change', (e) => {
  try { localStorage.setItem('samaya_ticket_filter', e.target.value); } catch { /* storage blocked */ }
  renderTickets();
});

byId('ticketTagFilter').addEventListener('change', (e) => {
  ticketTagFilter = e.target.value;
  renderTickets();
});

// ── Manage tags modal ────────────────────────────────────────

async function refreshTicketData() {
  [TICKETS, TAGS] = await Promise.all([
    api('GET', '/api/tickets', null, false, ticketsSlug()),
    api('GET', '/api/ticket-tags', null, false, ticketsSlug()),
  ]);
  populateTagFilter();
  renderTagManager();
  renderTickets();
}

function renderTagManager() {
  byId('tagManagerList').innerHTML = TAGS.length ? TAGS.map((g) => `<li class="tag-manager__row">
      <label class="sr-only" for="tagName${g.id}">Name of tag ${escapeHtml(g.name)}</label>
      <input class="pf-v6-c-form-control" type="text" id="tagName${g.id}" value="${escapeHtml(g.name)}" maxlength="24">
      <label class="sr-only" for="tagColor${g.id}">Color of tag ${escapeHtml(g.name)}</label>
      <input class="tag-manager__color" type="color" id="tagColor${g.id}" value="${escapeHtml(g.color)}">
      <span class="samaya-muted tag-manager__count">${g.ticket_count} ticket${g.ticket_count === 1 ? '' : 's'}</span>
      <span class="row-actions">
        <button type="button" class="pf-v6-c-button pf-m-secondary pf-m-small" data-action="tag-save" data-id="${g.id}">Save</button>
        <button type="button" class="pf-v6-c-button pf-m-link pf-m-danger pf-m-small" data-action="tag-delete" data-id="${g.id}">Delete</button>
      </span>
    </li>`).join('') : '<li class="samaya-muted">No tags yet.</li>';
}

function showTagError(message) {
  const box = byId('tagErrors');
  box.textContent = message || '';
  box.classList.toggle('hidden', !message);
}

function closeTagModal() {
  closeModalById('tagModal');
}

byId('btnManageTags').addEventListener('click', () => {
  showTagError('');
  renderTagManager();
  openModalById('tagModal');
});

bindActions(byId('tagManagerList'), {
  async 'tag-save'(btn) {
    const id = btn.dataset.id;
    showTagError('');
    try {
      await api('PATCH', `/api/ticket-tags/${id}`, { name: byId('tagName' + id).value, color: byId('tagColor' + id).value }, false, ticketsSlug());
      await refreshTicketData();
      toast('Tag saved.');
    } catch (e) { showTagError(e.message); }
  },
  async 'tag-delete'(btn) {
    const g = TAGS.find((x) => x.id === parseInt(btn.dataset.id, 10));
    if (!g) return;
    const used = g.ticket_count ? ` It is on ${g.ticket_count} ticket${g.ticket_count === 1 ? '' : 's'} and will be removed from ${g.ticket_count === 1 ? 'it' : 'them'}.` : '';
    if (!confirm(`Delete the tag "${g.name}"?${used}`)) return;
    showTagError('');
    try {
      await api('DELETE', `/api/ticket-tags/${g.id}`, null, false, ticketsSlug());
      await refreshTicketData();
      toast('Tag deleted.');
    } catch (e) { showTagError(e.message); }
  },
});

byId('tagCreateForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const name = byId('tagNewName');
  showTagError('');
  try {
    await api('POST', '/api/ticket-tags', { name: name.value, color: byId('tagNewColor').value }, false, ticketsSlug());
    name.value = '';
    await refreshTicketData();
    toast('Tag created.');
    name.focus();
  } catch (err) { showTagError(err.message); }
});

// ── Export (spec §76.5) ──────────────────────────────────────
// A fetch rather than a link: the download needs the X-Tenant-Slug header.

async function exportTickets(format) {
  const withContact = byId('exportContact').checked ? 1 : 0;
  try {
    const res = await fetch(`/admin/api/tickets/export?format=${format}&include_contact=${withContact}`, {
      credentials: 'same-origin', headers: { 'X-Tenant-Slug': ticketsSlug() },
    });
    if (res.status === 401) { window.location.href = '/auth/login'; return; }
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(describeApiError(err, res.statusText));
    }
    const blob = await res.blob();
    const named = /filename="?([^";]+)"?/.exec(res.headers.get('Content-Disposition') || '');
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = named ? named[1] : `samaya-tickets.${format === 'json' ? 'json' : 'zip'}`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast(withContact ? 'Exported with submitter contact details. Keep the file private.' : 'Export downloaded.');
  } catch (e) { toast(e.message, true); }
}

byId('btnExportJson').addEventListener('click', () => exportTickets('json'));
byId('btnExportMarkdown').addEventListener('click', () => exportTickets('markdown'));

// ── Edit modal ───────────────────────────────────────────────

function openTicketModal(t) {
  TICKETF.editing = t;
  byId('tkTitle').value = t.title;
  byId('tkDescription').value = t.description;
  byId('tkKind').value = t.kind;
  byId('tkErrors').classList.add('hidden');
  openModalById('ticketModal');
}

function closeTicketModal() {
  closeModalById('ticketModal');
}

byId('btnSaveTicket').addEventListener('click', async () => {
  const t = TICKETF.editing;
  if (!t) return;
  const title = byId('tkTitle').value.trim();
  const description = byId('tkDescription').value.trim();
  const kind = byId('tkKind').value;
  const box = byId('tkErrors');
  if (!title || !description) {
    box.textContent = 'Title and description cannot be blank.';
    box.classList.remove('hidden');
    return;
  }
  const payload = {};
  if (title !== t.title) payload.title = title;
  if (description !== t.description) payload.description = description;
  if (kind !== t.kind) payload.kind = kind;
  if (!Object.keys(payload).length) { closeTicketModal(); return; }
  if (await patchTicket(t.id, payload, 'Ticket updated. The submitter is not told.')) closeTicketModal();
});

// ── Board view (spec §76) ────────────────────────────────────
// State is TICKETS (status and position); the DOM is rebuilt from it. A move
// updates TICKETS at once, asks the server, and restores a snapshot if the
// server refuses (last write wins on the server, no offline queue).

function setTicketView(view) {
  ticketView = view;
  try { localStorage.setItem(TICKET_VIEW_KEY, view); } catch { /* storage blocked */ }
  applyTicketView();
}

function applyTicketView() {
  const board = ticketView === 'board';
  byId('ticketList').classList.toggle('hidden', board);
  byId('ticketBoardWrap').classList.toggle('hidden', !board);
  byId('ticketFilterItem').classList.toggle('hidden', board);
  byId('ticketViewList').setAttribute('aria-pressed', String(!board));
  byId('ticketViewBoard').setAttribute('aria-pressed', String(board));
  renderTickets();
}

// Same order as services/ticket_board.py: placed tickets by position, then votes, newest, id.
function boardCompare(a, b) {
  if ((a.position === null) !== (b.position === null)) return a.position === null ? 1 : -1;
  if (a.position !== null && a.position !== b.position) return a.position - b.position;
  if (a.upvote_count !== b.upvote_count) return b.upvote_count - a.upvote_count;
  const ta = Date.parse(a.created_at) || 0;
  const tb = Date.parse(b.created_at) || 0;
  if (ta !== tb) return tb - ta;
  return b.id - a.id;
}

function boardColumn(status, exceptId) {
  return TICKETS.filter((t) => t.status === status && t.id !== exceptId).sort(boardCompare);
}

function boardCardHtml(t, index, count) {
  const kind = TICKET_KIND_META[t.kind] || { label: t.kind, color: 'pf-m-gray' };
  const moderate = canModerateTickets();
  const meta = [`${t.upvote_count} upvote${t.upvote_count === 1 ? '' : 's'}`];
  if (t.tenant_name) meta.push(t.tenant_name);
  if (t.checklist.length) meta.push(`checklist ${t.checklist.filter((i) => i.done).length}/${t.checklist.length}`);
  let move = '';
  if (moderate) {
    const options = BOARD_COLUMNS.filter((s) => s !== t.status)
      .map((s) => ({ value: 'to:' + s, label: 'Move to ' + TICKET_STATUS_LABELS[s] }));
    if (index > 0) options.push({ value: 'up', label: 'Move up' });
    if (index < count - 1) options.push({ value: 'down', label: 'Move down' });
    move = `<label class="sr-only" for="boardMove${t.id}">Move ${escapeHtml(t.title)}</label>
      <select class="pf-v6-c-form-control board-card__move" id="boardMove${t.id}" data-board-move="${t.id}">
        <option value="">Move&hellip;</option>${optionsHtml(options, '')}
      </select>`;
  }
  return `<li class="board-card" data-ticket-id="${t.id}"${moderate ? ' draggable="true"' : ''}>
    <button type="button" class="board-card__title" data-action="board-open" data-id="${t.id}">${escapeHtml(t.title)}</button>
    <div class="board-card__meta">${pfLabel(kind.label, kind.color)} <span class="samaya-muted">${escapeHtml(meta.join(', '))}</span></div>
    ${t.tags.length ? `<div class="tag-row">${t.tags.map(tagChip).join('')}</div>` : ''}
    ${move}
  </li>`;
}

function renderBoard() {
  byId('ticketBoard').innerHTML = BOARD_COLUMNS.map((status) => {
    const column = boardColumn(status).filter(ticketMatchesTag);
    const cards = column.length
      ? column.map((t, i) => boardCardHtml(t, i, column.length)).join('')
      : '<li class="board__empty samaya-muted">No tickets</li>';
    return `<section class="board__col" data-status="${status}" aria-labelledby="boardHead-${status}">
      <h3 class="board__head" id="boardHead-${status}">${escapeHtml(TICKET_STATUS_LABELS[status])} <span class="samaya-muted">${column.length}</span></h3>
      <ul class="board__cards">${cards}</ul>
    </section>`;
  }).join('');
  applyTypeColors(byId('ticketBoard'));
  const dismissed = TICKETS.filter((t) => t.status === 'dismissed').length;
  const onBoard = TICKETS.filter((t) => t.status !== 'dismissed' && ticketMatchesTag(t)).length;
  byId('ticketCount').textContent = `${onBoard} ticket${onBoard === 1 ? '' : 's'} on the board`;
  byId('ticketBoardNote').textContent = dismissed
    ? `${dismissed} dismissed ticket${dismissed === 1 ? ' is' : 's are'} hidden from the board. Switch to List to see ${dismissed === 1 ? 'it' : 'them'}.`
    : '';
}

// Mirrors services/ticket_board.place(): the whole destination column is renumbered.
function placeLocal(ticket, status, index) {
  const others = boardColumn(status, ticket.id);
  others.splice(Math.max(0, Math.min(index, others.length)), 0, ticket);
  ticket.status = status;
  others.forEach((t, n) => { t.position = n; });
}

// Where "Move up" or "Move down" puts a card, counted in the whole column even when a
// tag filter hides some neighbours: it jumps over the next card that is on screen.
function shiftIndex(ticket, direction) {
  const visible = boardColumn(ticket.status).filter(ticketMatchesTag);
  const neighbour = visible[visible.findIndex((t) => t.id === ticket.id) + direction];
  if (!neighbour) return null;
  return boardColumn(ticket.status, ticket.id).findIndex((t) => t.id === neighbour.id) + (direction > 0 ? 1 : 0);
}

function focusBoardMove(id) {
  const el = byId('boardMove' + id);
  if (el) el.focus();
}

async function moveTicket(ticket, status, index) {
  if (ticket.status === status && index === boardColumn(status).findIndex((t) => t.id === ticket.id)) return;
  if (TICKET_ARCHIVED.includes(status) && !TICKET_ARCHIVED.includes(ticket.status)
      && !confirm(`Move "${ticket.title}" to ${TICKET_STATUS_LABELS[status]}? It moves to the public archive and voting closes.`)) return;
  const snapshot = TICKETS.map((t) => ({ ...t }));
  placeLocal(ticket, status, index);
  renderBoard();
  focusBoardMove(ticket.id);
  try {
    const res = await api('POST', `/api/tickets/${ticket.id}/move`, { status, index }, false, ticketsSlug());
    TICKETS = TICKETS.map((t) => {
      if (t.id === res.ticket.id) return res.ticket;
      return res.positions[t.id] === undefined ? t : { ...t, position: res.positions[t.id] };
    });
    renderBoard();
    focusBoardMove(ticket.id);
    toast(`Moved "${ticket.title}" to ${TICKET_STATUS_LABELS[status]}.`);
  } catch (e) {
    TICKETS = snapshot;
    renderBoard();
    toast(e.message, true);
  }
}

function showTicketInList(id) {
  byId('ticketFilter').value = 'all';
  ticketView = 'list';
  applyTicketView();
  const details = document.querySelector(`#ticketList details[data-ticket-id="${id}"]`);
  if (!details) return;
  details.open = true;
  details.scrollIntoView({ block: 'center' });
  details.querySelector('summary').focus();
}

const ticketBoard = byId('ticketBoard');
bindActions(ticketBoard, {
  'board-open'(btn) { showTicketInList(parseInt(btn.dataset.id, 10)); },
});
byId('ticketViewList').addEventListener('click', () => setTicketView('list'));
byId('ticketViewBoard').addEventListener('click', () => setTicketView('board'));

ticketBoard.addEventListener('change', (e) => {
  const select = e.target.closest('[data-board-move]');
  if (!select || !select.value) return;
  const ticket = ticketById(select.dataset.boardMove);
  const choice = select.value;
  select.value = '';
  if (!ticket) return;
  if (choice === 'up' || choice === 'down') {
    const index = shiftIndex(ticket, choice === 'up' ? -1 : 1);
    if (index !== null) moveTicket(ticket, ticket.status, index);
  } else if (choice.startsWith('to:')) moveTicket(ticket, choice.slice(3), boardColumn(choice.slice(3)).length);
});

// Mouse drag and drop. Touch screens and keyboards use each card's Move menu.
function clearBoardDrag() {
  TICKETF.dragId = null;
  ticketBoard.querySelectorAll('.board-card--dragging, .board__col--over').forEach((el) => {
    el.classList.remove('board-card--dragging', 'board__col--over');
  });
}

ticketBoard.addEventListener('dragstart', (e) => {
  const card = e.target.closest('.board-card');
  if (!card) return;
  TICKETF.dragId = parseInt(card.dataset.ticketId, 10);
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', String(TICKETF.dragId));
  card.classList.add('board-card--dragging');
});
ticketBoard.addEventListener('dragover', (e) => {
  const column = e.target.closest('.board__col');
  if (TICKETF.dragId === null || !column) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  ticketBoard.querySelectorAll('.board__col--over').forEach((el) => { if (el !== column) el.classList.remove('board__col--over'); });
  column.classList.add('board__col--over');
});
ticketBoard.addEventListener('dragend', clearBoardDrag);
ticketBoard.addEventListener('drop', (e) => {
  const column = e.target.closest('.board__col');
  const id = TICKETF.dragId;
  clearBoardDrag();
  const ticket = column && id !== null ? ticketById(id) : null;
  if (!ticket) return;
  e.preventDefault();
  const status = column.dataset.status;
  const ids = boardColumn(status, id).map((t) => t.id);
  const over = e.target.closest('.board-card');
  let index = ids.length;
  if (over) {
    const overId = parseInt(over.dataset.ticketId, 10);
    if (overId === id) return;
    const rect = over.getBoundingClientRect();
    index = ids.indexOf(overId) + (e.clientY > rect.top + rect.height / 2 ? 1 : 0);
  }
  moveTicket(ticket, status, index);
});

VIEW_LOADERS.feedback = loadTickets;
