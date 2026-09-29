// Tickets view (#v-tickets, spec §40-43): the admin triage list over the
// public /feedback board's Ticket rows. Kingdom-wide (no per-alliance
// filter — GET /admin/api/tickets ignores which alliance the X-Tenant-Slug
// header names, it's only there because every admin route requires one),
// so this reuses whichever tenant the caller has access to first, the
// same pattern audit.js's auditTenantSlug() already established for a
// tenant-header-required-but-not-actually-scoping-the-data call.
function ticketsTenantSlug() {
  return TENANTS[0] ? TENANTS[0].slug : '';
}

const TICKET_KIND_META = {
  feedback:              { label: 'Feedback',      color: 'pf-m-blue' },
  event_request:         { label: 'Event Request',        color: 'pf-m-green' },
  announcement_request:  { label: 'Announcement Request', color: 'pf-m-purple' },
  error:                 { label: 'Issue',          color: 'pf-m-red' },
};

const TICKET_STATUSES = ['open', 'planned', 'in_progress', 'done', 'declined'];

async function loadTickets() {
  try {
    TICKETS_CACHE = await api('GET', '/api/tickets', null, false, ticketsTenantSlug());
    renderTickets();
  } catch(e) { toast(e.message, true); }
}

let TICKETS_CACHE = [];

function buildTicketRow(t) {
  const kindMeta = TICKET_KIND_META[t.kind] || { label: t.kind, color: 'pf-m-gray' };
  const alliance = t.tenant_name ? escapeHtml(t.tenant_name) : '<span style="color:var(--muted)">Kingdom-wide</span>';
  const related = t.related_name ? `<div style="color:var(--muted);font-size:var(--fs-sm)">Re: ${escapeHtml(t.related_name)}</div>` : '';
  const statusOptions = TICKET_STATUSES.map(s =>
    `<option value="${s}" ${s === t.status ? 'selected' : ''}>${s.replace('_',' ')}</option>`
  ).join('');
  return '<tr class="pf-v6-c-table__tr">'
    + '<td class="pf-v6-c-table__td">' + pfLabel(kindMeta.label, kindMeta.color) + (t.error_type ? `<div style="color:var(--muted);font-size:var(--fs-sm)">${escapeHtml(t.error_type)}</div>` : '') + '</td>'
    + '<td class="pf-v6-c-table__td"><strong>' + escapeHtml(t.title) + '</strong>' + related + `<div style="color:var(--muted);font-size:var(--fs-sm);white-space:pre-wrap;max-width:360px">${escapeHtml(t.description)}</div>` + '</td>'
    + '<td class="pf-v6-c-table__td">' + alliance + '</td>'
    + '<td class="pf-v6-c-table__td">▲ ' + t.upvote_count + '</td>'
    + '<td class="pf-v6-c-table__td" style="color:var(--muted);font-size:var(--fs-sm)">' + (t.created_at ? formatRelativeTime(new Date(t.created_at)) : '') + '</td>'
    + '<td class="pf-v6-c-table__td" style="color:var(--muted);font-size:var(--fs-sm)">' + (t.submitter_contact ? escapeHtml(t.submitter_contact) : '—') + '</td>'
    + '<td class="pf-v6-c-table__td"><select class="pf-v6-c-form-control pf-m-small" onchange="updateTicketStatus(' + t.id + ', this.value)">' + statusOptions + '</select></td>'
    + '</tr>';
}

function renderTickets() {
  const tbody = document.getElementById('ticketsBody');
  tbody.innerHTML = TICKETS_CACHE.length
    ? TICKETS_CACHE.map(buildTicketRow).join('')
    : '<tr class="pf-v6-c-table__tr"><td class="pf-v6-c-table__td" colspan="7" style="color:var(--muted);padding:20px">No tickets yet.</td></tr>';
}

async function updateTicketStatus(id, status) {
  try {
    await api('PATCH', `/api/tickets/${id}`, { status }, false, ticketsTenantSlug());
    const t = TICKETS_CACHE.find(x => x.id === id);
    if (t) t.status = status;
    toast('Status updated');
  } catch(e) { toast(e.message, true); loadTickets(); }
}
