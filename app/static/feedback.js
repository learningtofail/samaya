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

const KIND_META = {
  feedback:              { label: 'Feedback',      color: 'pf-m-blue' },
  event_request:         { label: 'Event Request',        color: 'pf-m-green' },
  announcement_request:  { label: 'Announcement Request', color: 'pf-m-purple' },
  error:                 { label: 'Issue',          color: 'pf-m-red' },
};
const STATUS_META = {
  open:        { label: 'Open',        color: 'pf-m-gray' },
  planned:     { label: 'Planned',     color: 'pf-m-cyan' },
  in_progress: { label: 'In Progress', color: 'pf-m-orange' },
  done:        { label: 'Done',        color: 'pf-m-green' },
  declined:    { label: 'Declined',    color: 'pf-m-red' },
};

function pfLabel(text, color) {
  return `<span class="pf-v6-c-label ${color} pf-m-filled"><span class="pf-v6-c-label__content"><span class="pf-v6-c-label__text">${text}</span></span></span>`;
}

function formatRelativeTime(iso) {
  const diffMs = Date.now() - new Date(iso).getTime();
  const mins = Math.round(diffMs / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.round(hrs / 24);
  return `${days}d ago`;
}

let TICKETS = [];

async function loadTickets() {
  try {
    TICKETS = await api('GET', '/api/tickets');
    renderTickets();
  } catch (e) {
    document.getElementById('ticketsWrap').innerHTML = `<div class="empty-state">Couldn't load the board: ${escapeHtml(e.message)}</div>`;
  }
}

// Each row's upvote button carries data-ticket-id instead of an inline
// onclick="toggleVote(${t.id})" — the ticketsWrap container below handles
// the click via delegation (elementList didn't exist yet, so a per-row
// addEventListener() at render time isn't an option here; see the module
// doc comment).
function renderTickets() {
  const wrap = document.getElementById('ticketsWrap');
  if (!TICKETS.length) {
    wrap.innerHTML = '<div class="empty-state">Nothing here yet — be the first to submit feedback or a request.</div>';
    return;
  }
  wrap.innerHTML = TICKETS.map(t => {
    const kindMeta = KIND_META[t.kind] || { label: t.kind, color: 'pf-m-gray' };
    const statusMeta = STATUS_META[t.status] || { label: t.status, color: 'pf-m-gray' };
    const relatedLine = (t.kind === 'error' && t.related_name)
      ? `<div class="pf-v6-u-font-size-sm samaya-subtle-text">Re: ${escapeHtml(t.related_name)}</div>` : '';
    const allianceLine = t.tenant_name ? escapeHtml(t.tenant_name) : 'Kingdom-wide / general';
    return `
    <div class="pf-v6-c-card pf-v6-u-mb-sm">
      <div class="pf-v6-c-card__body">
        <div class="ticket-row">
          <button class="vote-btn${t.voted_by_me ? ' voted' : ''}" data-ticket-id="${t.id}" aria-label="Upvote ${escapeHtml(t.title)}">
            <span>▲</span><span class="vote-count">${t.upvote_count}</span>
          </button>
          <div class="ticket-body">
            <div class="pf-v6-l-flex pf-m-align-items-center pf-m-space-items-sm">
              ${pfLabel(kindMeta.label, kindMeta.color)}
              <strong>${escapeHtml(t.title)}</strong>
            </div>
            ${relatedLine}
            <p class="pf-v6-u-font-size-sm pf-v6-u-mt-xs samaya-ticket-description">${escapeHtml(t.description)}</p>
            <div class="ticket-meta">
              ${pfLabel(statusMeta.label, statusMeta.color)}
              <span class="pf-v6-u-font-size-sm samaya-subtle-text">${allianceLine} · reported ${formatRelativeTime(t.created_at)}</span>
            </div>
          </div>
        </div>
      </div>
    </div>`;
  }).join('');
}

document.getElementById('ticketsWrap').addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-ticket-id]');
  if (btn) toggleVote(Number(btn.dataset.ticketId));
});

async function toggleVote(id) {
  try {
    const result = await api('POST', `/api/tickets/${id}/vote`);
    const t = TICKETS.find(x => x.id === id);
    if (t) { t.upvote_count = result.upvote_count; t.voted_by_me = result.voted_by_me; }
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
  if (!title || !description) { alert('Title and description are required.'); return; }
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
  if (!name || !details) { alert('Proposed name and details are required.'); return; }
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

loadTickets();

})();
