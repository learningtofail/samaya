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
const KIND_COLORS = { feedback: 'pf-m-blue', event_request: 'pf-m-green', announcement_request: 'pf-m-purple', error: 'pf-m-red' };
const STATUS_COLORS = { open: 'pf-m-gray', planned: 'pf-m-cyan', in_progress: 'pf-m-orange', done: 'pf-m-green', declined: 'pf-m-red', dismissed: 'pf-m-gray' };
function metaFor(group, colors, key) {
  const k = `${FB}${group}.${key}`;
  return { label: I18N.has(k) ? t(k) : key, color: colors[key] || 'pf-m-gray' };
}

function pfLabel(text, color) {
  return `<span class="pf-v6-c-label ${color} pf-m-filled"><span class="pf-v6-c-label__content"><span class="pf-v6-c-label__text">${escapeHtml(text)}</span></span></span>`;
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
    document.getElementById('ticketsWrap').innerHTML = `<div class="empty-state">${escapeHtml(t(FB + 'loadError', { error: e.message }))}</div>`;
  }
}

function formatWhen(iso) {
  return iso ? new Intl.DateTimeFormat(I18N.intlLocale, { day: 'numeric', month: 'short', year: 'numeric' }).format(new Date(iso)) : '';
}

function responsesHtml(tk) {
  if (!tk.responses || !tk.responses.length) return '';
  return `<ul class="ticket-responses" aria-label="${escapeHtml(t(FB + 'responsesLabel'))}">${tk.responses.map(r => `
    <li class="ticket-responses__item">
      <div class="ticket-responses__head"><strong>${escapeHtml(r.author)}</strong>
        <span class="samaya-subtle-text">${escapeHtml(formatWhen(r.created_at))}</span></div>
      <p class="ticket-responses__body">${escapeHtml(r.body)}</p>
    </li>`).join('')}</ul>`;
}

// Each row's upvote button carries data-ticket-id instead of an inline
// onclick — the ticketsWrap container handles the click via delegation.
// An archived ticket (done or declined) is read-only: no vote button, but
// its status and responses stay visible so the reasoning can be looked up.
function ticketHtml(tk, archived) {
  const kindMeta = metaFor('kind', KIND_COLORS, tk.kind);
  const statusMeta = metaFor('status', STATUS_COLORS, tk.status);
  const relatedLine = (tk.kind === 'error' && tk.related_name)
    ? `<div class="pf-v6-u-font-size-sm samaya-subtle-text">${escapeHtml(t(FB + 'relatedTo', { name: tk.related_name }))}</div>` : '';
  const vote = archived
    ? `<span class="vote-btn vote-btn--static" aria-label="${escapeHtml(t(FB + 'upvotesCount', { count: tk.upvote_count }))}"><span>▲</span><span class="vote-count">${I18N.number(tk.upvote_count)}</span></span>`
    : `<button class="vote-btn${tk.voted_by_me ? ' voted' : ''}" data-ticket-id="${tk.id}" aria-label="${escapeHtml(t(FB + 'upvoteTitle', { title: tk.title }))}"><span>▲</span><span class="vote-count">${I18N.number(tk.upvote_count)}</span></button>`;
  return `
    <div class="pf-v6-c-card pf-v6-u-mb-sm">
      <div class="pf-v6-c-card__body">
        <div class="ticket-row">
          ${vote}
          <div class="ticket-body">
            <div class="pf-v6-l-flex pf-m-align-items-center pf-m-space-items-sm">
              ${pfLabel(kindMeta.label, kindMeta.color)}
              <strong>${escapeHtml(tk.title)}</strong>
            </div>
            ${relatedLine}
            <p class="pf-v6-u-font-size-sm pf-v6-u-mt-xs samaya-ticket-description">${escapeHtml(tk.description)}</p>
            <div class="ticket-meta">
              ${pfLabel(statusMeta.label, statusMeta.color)}
              <span class="pf-v6-u-font-size-sm samaya-subtle-text">${escapeHtml(t(FB + 'metaLine', { alliance: tk.tenant_name || t(FB + 'generalAlliance'), when: formatRelativeTime(tk.created_at) }))}</span>
            </div>
            ${responsesHtml(tk)}
          </div>
        </div>
      </div>
    </div>`;
}

function renderTickets() {
  const wrap = document.getElementById('ticketsWrap');
  const { active, archived } = TICKETS;
  const activeHtml = active.length
    ? active.map(tk => ticketHtml(tk, false)).join('')
    : `<div class="empty-state">${escapeHtml(t(FB + 'emptyActive'))}</div>`;
  const archiveHtml = archived.length ? `
    <details class="ticket-archive">
      <summary>${escapeHtml(t(FB + 'archivedTitle', { count: archived.length }))}</summary>
      <p class="samaya-subtle-text ticket-archive__note">${escapeHtml(t(FB + 'archivedNote'))}</p>
      ${archived.map(tk => ticketHtml(tk, true)).join('')}
    </details>` : '';
  wrap.innerHTML = activeHtml + archiveHtml;
}

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
  const openModal = document.querySelector('.pf-v6-c-backdrop.open');
  if (!openModal) return;
  const fnName = openModal.dataset.closeFn;
  if (fnName && typeof window[fnName] === 'function') window[fnName]();
});

function openFeedbackModal() {
  document.getElementById('fbCategory').value = 'Bug';
  document.getElementById('fbTitle').value = '';
  document.getElementById('fbDescription').value = '';
  document.getElementById('fbContact').value = '';
  document.getElementById('feedbackModal').classList.add('open');
}
function closeFeedbackModal() { document.getElementById('feedbackModal').classList.remove('open'); }

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
  document.getElementById('requestModal').classList.add('open');
}
function closeRequestModal() { document.getElementById('requestModal').classList.remove('open'); }

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
    const backdrop = btn.closest('.pf-v6-c-backdrop');
    const fnName = backdrop && backdrop.dataset.closeFn;
    if (fnName === 'closeFeedbackModal') closeFeedbackModal();
    if (fnName === 'closeRequestModal') closeRequestModal();
  });
});

I18N.apply(document);
loadTickets();

})();
