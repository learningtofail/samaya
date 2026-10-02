// Feedback tab (#v-feedback, spec §66.7, §66.10): triage of the public
// /feedback board. Each ticket expands to its details, status, edit, dismiss
// and delete actions, and a thread of PUBLIC responses. Depends on common.js.
//
// GET /admin/api/tickets is Kingdom-wide: it ignores which alliance the
// X-Tenant-Slug header names (the header only proves the caller has access).

let TICKETS = [];
const TICKETF = { editing: null };

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
  try {
    TICKETS = await api('GET', '/api/tickets', null, false, ticketsSlug());
    renderTickets();
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
  const f = byId('ticketFilter').value;
  const openIds = new Set(Array.from(document.querySelectorAll('#ticketList details[open]')).map((d) => d.dataset.ticketId));
  const shown = TICKETS.filter((t) => ticketMatchesFilter(t, f));
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
ticketList.addEventListener('change', (e) => {
  const sel = e.target.closest('[data-action-change="status"]');
  if (sel) patchTicket(parseInt(sel.dataset.id, 10), { status: sel.value }, 'Status updated.');
});
ticketList.addEventListener('submit', async (e) => {
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

VIEW_LOADERS.feedback = loadTickets;
