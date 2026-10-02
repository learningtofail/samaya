// The standalone public feedback/request board at /feedback (spec §40-43).
// Extracted out of feedback.html's own inline <script> block (Phase 3 audit
// remediation) into this file, as one IIFE with no globals — same
// convention events-public.js already established (spec §62) — and with
// every onclick="..." attribute replaced by addEventListener wiring.
// No shared JS with admin.html/events-public.js (§22's precedent).
(function () {
'use strict';

// ── Voter identity (spec §43.2) — a per-browser, not per-person, token ──
function getVoterId() {
  try {
    let id = localStorage.getItem('samaya_voter_id');
    if (!id) {
      id = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random());
      localStorage.setItem('samaya_voter_id', id);
    }
    return id;
  } catch (e) {
    return '';
  }
}
const VOTER_ID = getVoterId();

// Interface strings and locale-aware formatting (spec §72), read from the page's i18n block.
const I18N = SamayaI18n.fromDocument(document);
const t = I18N.t;
const FB = 'public.feedback.';

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

async function api(method, path, body) {
  const headers = { 'Content-Type': 'application/json', 'X-Voter-Id': VOTER_ID };
  const res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
  }
  return res.status === 204 ? null : res.json();
}

// Labels come from the catalogue (public.feedback.kind.<key>, .status.<key>).
// Only these values reach a class name (the API value is never trusted in markup).
const KINDS = ['feedback', 'event_request', 'announcement_request', 'error'];
const STATUSES = ['open', 'planned', 'in_progress', 'done', 'declined', 'dismissed'];
function labelFor(group, key) {
  const k = `${FB}${group}.${key}`;
  return I18N.has(k) ? t(k) : key;
}

function formatRelativeTime(iso) {
  const diffMs = Date.now() - new Date(iso).getTime();
  const mins = Math.round(diffMs / 60000);
  if (mins < 1) return t(FB + 'justNow');
  if (mins < 60) return I18N.relative(-mins, 'minute');
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return I18N.relative(-hrs, 'hour');
  return I18N.relative(-Math.round(hrs / 24), 'day');
}

let TICKETS = { active: [], archived: [] };

async function loadTickets() {
  try {
    TICKETS = await api('GET', '/api/tickets');
    renderTickets();
  } catch (e) {
    document.getElementById('ticketsWrap').innerHTML = `<div class="empty">${escapeHtml(t(FB + 'loadError', { error: e.message }))}</div>`;
  }
}

function formatWhen(iso) {
  return iso ? new Intl.DateTimeFormat(I18N.intlLocale, { day: 'numeric', month: 'short', year: 'numeric' }).format(new Date(iso)) : '';
}

function responsesHtml(tk) {
  if (!tk.responses || !tk.responses.length) return '';
  return `<ul class="responses" aria-label="${escapeHtml(t(FB + 'responsesLabel'))}">${tk.responses.map((r) => `
    <li><div class="responses-head"><strong><bdi>${escapeHtml(r.author)}</bdi></strong><span>${escapeHtml(formatWhen(r.created_at))}</span></div>
      <p class="responses-body" dir="auto">${escapeHtml(r.body)}</p></li>`).join('')}</ul>`;
}

// An archived ticket (done or declined) is read-only: no vote button, but its
// status and responses stay visible so the reasoning can be looked up.
function ticketHtml(tk, archived) {
  const kind = KINDS.includes(tk.kind) ? tk.kind : 'feedback';
  const status = STATUSES.includes(tk.status) ? tk.status : 'open';
  const rel = (tk.kind === 'error' && tk.related_name)
    ? `<div class="ticket-rel">${escapeHtml(t(FB + 'relatedTo', { name: I18N.iso(tk.related_name) }))}</div>` : '';
  const where = tk.tenant_name ? I18N.iso(tk.tenant_name) : t(FB + 'generalAlliance');
  const n = (tk.responses || []).length;
  const count = I18N.number(tk.upvote_count);
  const vote = archived
    ? `<span class="vote vote--static" aria-label="${escapeHtml(t(FB + 'upvotesCount', { count: tk.upvote_count }))}"><i>&#9650;</i><b>${count}</b></span>`
    : `<button type="button" class="vote${tk.voted_by_me ? ' is-voted' : ''}" data-ticket-id="${tk.id}" aria-pressed="${tk.voted_by_me ? 'true' : 'false'}" aria-label="${escapeHtml(t(FB + 'upvoteTitle', { title: tk.title }))}"><i>&#9650;</i><b>${count}</b></button>`;
  return `<article class="ticket">
    ${vote}
    <div class="ticket-main">
      <div class="ticket-title"><b dir="auto">${escapeHtml(tk.title)}</b><span class="tag tag--${kind}">${escapeHtml(labelFor('kind', kind))}</span></div>
      ${rel}
      <p class="ticket-desc" dir="auto">${escapeHtml(tk.description)}</p>
      <div class="ticket-meta"><span class="status status--${status}">${escapeHtml(labelFor('status', status))}</span><span>${escapeHtml(t(FB + 'metaLine', { alliance: where, when: formatRelativeTime(tk.created_at) }))}</span>${n ? `<span class="resp-count">${escapeHtml(t(FB + 'teamResponses', { count: n }))}</span>` : ''}</div>
      ${responsesHtml(tk)}
    </div>
  </article>`;
}

// Filter and sort run in the browser over the list the API already returns.
let KIND_FILTER = 'all';
let SORT = 'votes';
function visible(list) {
  const out = KIND_FILTER === 'all' ? list.slice() : list.filter((tk) => tk.kind === KIND_FILTER);
  out.sort((a, b) => (SORT === 'newest'
    ? new Date(b.created_at) - new Date(a.created_at)
    : (b.upvote_count - a.upvote_count) || (new Date(b.created_at) - new Date(a.created_at))));
  return out;
}

function renderTickets() {
  const wrap = document.getElementById('ticketsWrap');
  const active = visible(TICKETS.active), archived = visible(TICKETS.archived);
  const empty = KIND_FILTER !== 'all' ? 'emptyFiltered' : 'emptyActive';
  const activeHtml = `<section class="board" aria-labelledby="boardTitle">
      <div class="board-head"><h2 id="boardTitle">${escapeHtml(t(FB + 'activeTitle'))}</h2><span>${escapeHtml(t(FB + 'itemCount', { count: active.length }))}</span></div>
      ${active.length ? active.map((tk) => ticketHtml(tk, false)).join('') : `<div class="empty">${escapeHtml(t(FB + empty))}</div>`}
    </section>`;
  const archiveHtml = archived.length ? `
    <details class="archive"${ARCHIVE_OPEN ? ' open' : ''}>
      <summary>${escapeHtml(t(FB + 'archivedTitle'))}<span>${escapeHtml(t(FB + 'archivedCount', { count: archived.length }))}</span></summary>
      <p class="archive-note">${escapeHtml(t(FB + 'archivedNote'))}</p>
      ${archived.map((tk) => ticketHtml(tk, true)).join('')}
    </details>` : '';
  wrap.innerHTML = activeHtml + archiveHtml;
}

// A re-render (a vote, a filter) must not collapse an archive the visitor opened.
let ARCHIVE_OPEN = false;
document.getElementById('ticketsWrap').addEventListener('toggle', (e) => {
  if (e.target.classList && e.target.classList.contains('archive')) ARCHIVE_OPEN = e.target.open;
}, true);

document.getElementById('kindChips').addEventListener('click', (e) => {
  const b = e.target.closest('[data-kind]');
  if (!b) return;
  KIND_FILTER = b.dataset.kind;
  document.querySelectorAll('#kindChips [data-kind]').forEach((c) => c.setAttribute('aria-pressed', String(c === b)));
  renderTickets();
});
document.getElementById('sortSelect').addEventListener('change', (e) => { SORT = e.target.value; renderTickets(); });

document.getElementById('ticketsWrap').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-ticket-id]');
  if (btn) toggleVote(Number(btn.dataset.ticketId));
});

async function toggleVote(id) {
  try {
    const result = await api('POST', `/api/tickets/${id}/vote`);
    const tk = TICKETS.active.find(x => x.id === id);
    if (tk) { tk.upvote_count = result.upvote_count; tk.voted_by_me = result.voted_by_me; }
    renderTickets();
  } catch (e) {
    alert(e.message);
  }
}

// ── Modals ──
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  const openModal = document.querySelector('.modal.open');
  if (!openModal) return;
  const close = MODAL_CLOSERS[openModal.dataset.closeFn];
  if (close) close();
});

// The functions live inside this IIFE, so Escape and the dismiss buttons look them up here.
const MODAL_CLOSERS = {};
let RETURN_FOCUS = null;
function showModal(id) {
  RETURN_FOCUS = document.activeElement;
  const modal = document.getElementById(id);
  modal.classList.add('open');
  const first = modal.querySelector('select, input, textarea');
  if (first) first.focus();
}
function hideModal(id) {
  document.getElementById(id).classList.remove('open');
  if (RETURN_FOCUS && typeof RETURN_FOCUS.focus === 'function') RETURN_FOCUS.focus();
}

function openFeedbackModal() {
  document.getElementById('fbCategory').value = 'Bug';
  document.getElementById('fbTitle').value = '';
  document.getElementById('fbDescription').value = '';
  document.getElementById('fbContact').value = '';
  showModal('feedbackModal');
}
function closeFeedbackModal() { hideModal('feedbackModal'); }

async function submitFeedback() {
  const title = document.getElementById('fbTitle').value.trim();
  const description = document.getElementById('fbDescription').value.trim();
  if (!title || !description) { alert(t(FB + 'titleRequired')); return; }
  try {
    await api('POST', '/api/tickets', {
      kind: 'feedback',
      error_type: document.getElementById('fbCategory').value,
      title, description,
      submitter_contact: document.getElementById('fbContact').value.trim() || null,
    });
    closeFeedbackModal();
    await loadTickets();
  } catch (e) { alert(e.message); }
}

let ALLIANCES_LOADED = false;
async function ensureAlliancesLoaded() {
  if (ALLIANCES_LOADED) return;
  try {
    const alliances = await api('GET', '/api/alliances');
    const sel = document.getElementById('reqAlliance');
    alliances.forEach(a => {
      const opt = document.createElement('option');
      opt.value = a.slug; opt.textContent = a.name;
      sel.appendChild(opt);
    });
    ALLIANCES_LOADED = true;
  } catch (e) { /* Kingdom-wide/not sure option still works without this */ }
}

async function openRequestModal() {
  await ensureAlliancesLoaded();
  document.getElementById('reqType').value = 'event_request';
  document.getElementById('reqAlliance').value = '';
  document.getElementById('reqName').value = '';
  document.getElementById('reqTiming').value = '';
  document.getElementById('reqDetails').value = '';
  document.getElementById('reqContact').value = '';
  showModal('requestModal');
}
function closeRequestModal() { hideModal('requestModal'); }
MODAL_CLOSERS.closeFeedbackModal = closeFeedbackModal;
MODAL_CLOSERS.closeRequestModal = closeRequestModal;

async function submitRequest() {
  const name = document.getElementById('reqName').value.trim();
  const details = document.getElementById('reqDetails').value.trim();
  if (!name || !details) { alert(t(FB + 'requestRequired')); return; }
  const timing = document.getElementById('reqTiming').value.trim();
  const kind = document.getElementById('reqType').value;
  const description = `Proposed name: ${name}\nProposed timing: ${timing || '(not specified)'}\n\n${details}`;
  try {
    await api('POST', '/api/tickets', {
      kind,
      title: name,
      description,
      tenant_slug: document.getElementById('reqAlliance').value || null,
      submitter_contact: document.getElementById('reqContact').value.trim() || null,
    });
    closeRequestModal();
    await loadTickets();
  } catch (e) { alert(e.message); }
}

// Wire the page's own static controls — replaces their onclick attributes
// (Phase 3 audit remediation).
document.getElementById('btnOpenFeedbackModal').addEventListener('click', openFeedbackModal);
document.getElementById('btnOpenRequestModal').addEventListener('click', openRequestModal);
document.getElementById('btnSubmitFeedback').addEventListener('click', submitFeedback);
document.getElementById('btnSubmitRequest').addEventListener('click', submitRequest);
document.querySelectorAll('[data-modal-dismiss]').forEach((btn) => {
  btn.addEventListener('click', () => {
    const backdrop = btn.closest('.modal');
    const close = backdrop && MODAL_CLOSERS[backdrop.dataset.closeFn];
    if (close) close();
  });
});

I18N.apply(document);
I18N.bindLanguageSelect(document.getElementById('langSelect'), I18N.locales);
loadTickets();

})();
