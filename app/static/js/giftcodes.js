// Gift codes tab (#v-giftcodes): start a redemption run and watch it (spec §79).
// State lives in GIFT; the server advances runs, this page only starts, polls and
// cancels. Depends on common.js.

const GIFT = { enabled: false, runs: [], timer: null };
const GIFT_POLL_ACTIVE_MS = 3000;
const GIFT_POLL_IDLE_MS = 30000;

const GIFT_STOP_TEXT = {
  code_expired: 'The code has expired.',
  code_invalid: 'The game does not recognise this code.',
  claim_limit: 'The code has reached its claim limit.',
  api_changed: "The game's answers stopped making sense, which usually means it changed its API. Nothing more was sent.",
  unreachable: 'The game server could not be reached.',
  blocked: 'The game server refused these requests. Nothing more was sent.',
  cancelled: 'Cancelled.',
};

const GIFT_STATUS_LABEL = {
  queued: ['Queued', 'pf-m-gray'], running: ['Running', 'pf-m-blue'], done: ['Done', 'pf-m-green'],
  stopped: ['Stopped', 'pf-m-orange'], cancelled: ['Cancelled', 'pf-m-gray'],
};

function giftIsActive(run) { return run.status === 'queued' || run.status === 'running'; }
function giftViewOpen() { return byId('v-giftcodes').classList.contains('active'); }

async function loadGiftcodes() {
  try {
    GIFT.enabled = (await api('GET', '/api/giftcode-config', null, true)).enabled;
  } catch (e) { toast(e.message, true); }
  byId('giftcodesOff').classList.toggle('hidden', GIFT.enabled);
  byId('giftStartPanel').classList.toggle('hidden', !GIFT.enabled || !writableTenants().length);
  renderGiftAlliances();
  await refreshGiftRuns();
}

function renderGiftAlliances() {
  const box = byId('giftAlliances');
  if (box.dataset.ready) return;
  box.dataset.ready = '1';
  box.innerHTML = writableTenants().map((t) =>
    `<label class="check"><input type="checkbox" name="giftAlliance" value="${t.id}" checked> ${escapeHtml(t.name)}</label>`).join('');
}

async function refreshGiftRuns() {
  clearTimeout(GIFT.timer);
  try {
    GIFT.runs = await api('GET', '/api/giftcode-runs?limit=20', null, false, COMBINED_SLUG);
    renderGiftRuns();
  } catch (e) { toast(e.message, true); }
  if (!giftViewOpen()) return;
  GIFT.timer = setTimeout(refreshGiftRuns, GIFT.runs.some(giftIsActive) ? GIFT_POLL_ACTIVE_MS : GIFT_POLL_IDLE_MS);
}

function giftCountsText(c) {
  const bits = [`${c.redeemed} redeemed`, `${c.already} already had it`];
  if (c.wrong_kingdom) bits.push(`${c.wrong_kingdom} wrong kingdom`);
  if (c.requirement) bits.push(`${c.requirement} below the code's requirement`);
  if (c.other) bits.push(`${c.other} other`);
  if (c.skipped) bits.push(`${c.skipped} not sent`);
  return bits.join(', ');
}

function renderGiftRuns() {
  const active = GIFT.runs.find(giftIsActive);
  byId('giftActive').classList.toggle('hidden', !active);
  if (active) {
    const c = active.counts;
    const done = c.total - c.waiting;
    byId('giftActiveBody').innerHTML = `
      <p class="run-card__title">Redeeming <code>${escapeHtml(active.code)}</code></p>
      <label class="sr-only" for="giftProgress">Players processed</label>
      <progress class="run-card__bar" id="giftProgress" max="${c.total}" value="${done}">${done} of ${c.total}</progress>
      <p class="run-card__counts" role="status">${done} of ${c.total} done. ${escapeHtml(giftCountsText(c))}.</p>
      <div class="row-actions"><button type="button" class="pf-v6-c-button pf-m-danger" data-action="cancel" data-id="${active.id}">Cancel run</button></div>`;
  }
  byId('giftRunsBody').innerHTML = GIFT.runs.length ? GIFT.runs.map(giftRunRowHtml).join('') : emptyRow(5, 'No runs yet.');
}

function giftRunRowHtml(r) {
  const [label, color] = GIFT_STATUS_LABEL[r.status] || [r.status, 'pf-m-gray'];
  const why = r.stop_reason && GIFT_STOP_TEXT[r.stop_reason] ? `<div class="samaya-muted">${escapeHtml(GIFT_STOP_TEXT[r.stop_reason])}</div>` : '';
  return `<tr class="pf-v6-c-table__tr">
    <td class="pf-v6-c-table__td" data-label="Code"><code>${escapeHtml(r.code)}</code></td>
    <td class="pf-v6-c-table__td samaya-muted" data-label="Started" data-sort="${escapeHtml(r.started_at || r.created_at)}">${escapeHtml(fmtDateTime(r.started_at || r.created_at))}</td>
    <td class="pf-v6-c-table__td" data-label="Status">${pfLabel(label, color)}${why}</td>
    <td class="pf-v6-c-table__td" data-label="Result">${escapeHtml(giftCountsText(r.counts))}</td>
    <td class="pf-v6-c-table__td" data-label="Actions"><button type="button" class="pf-v6-c-button pf-m-secondary pf-m-small" data-action="details" data-id="${r.id}">Details</button></td>
  </tr>`;
}

async function startGiftRun() {
  const code = byId('giftCode').value.trim();
  const tenantIds = [...byId('giftAlliances').querySelectorAll('input:checked')].map((i) => parseInt(i.value, 10));
  if (!code) { toast('Enter a gift code', true); return; }
  if (!tenantIds.length) { toast('Pick at least one alliance', true); return; }
  try {
    await api('POST', '/api/giftcode-runs', { code, tenant_ids: tenantIds }, true);
    byId('giftCode').value = '';
    toast('Run started. It begins within a minute.');
    refreshGiftRuns();
  } catch (e) { toast(e.message, true); }
}

async function cancelGiftRun(id) {
  if (!confirm('Cancel this run? Players not yet sent the code will be skipped.')) return;
  try {
    await api('POST', `/api/giftcode-runs/${id}/cancel`, null, true);
    toast('Run cancelled');
    refreshGiftRuns();
  } catch (e) { toast(e.message, true); }
}

function giftProblemRows(list) {
  return list.map((p) => `<tr class="pf-v6-c-table__tr">
    <td class="pf-v6-c-table__td" data-label="Player ID"><code>${escapeHtml(p.fid)}</code>${p.name ? ' ' + escapeHtml(p.name) : ''}</td>
    <td class="pf-v6-c-table__td" data-label="Alliance">${escapeHtml(p.alliance)}</td>
    <td class="pf-v6-c-table__td" data-label="Kingdom tried">${escapeHtml(String(p.kid))}</td>
    <td class="pf-v6-c-table__td" data-label="Answer">${escapeHtml(p.message)}</td>
  </tr>`).join('');
}

function giftProblemTable(title, list) {
  if (!list.length) return '';
  return `<h3 class="view__subtitle">${escapeHtml(title)}</h3>
    <div class="table-wrap"><table class="pf-v6-c-table pf-m-grid-md responsive-table">
      <thead><tr class="pf-v6-c-table__tr"><th class="pf-v6-c-table__th" scope="col">Player</th><th class="pf-v6-c-table__th" scope="col">Alliance</th><th class="pf-v6-c-table__th" scope="col">Kingdom tried</th><th class="pf-v6-c-table__th" scope="col">Answer</th></tr></thead>
      <tbody>${giftProblemRows(list)}</tbody></table></div>`;
}

async function openRunDetails(id) {
  try {
    const run = await api('GET', `/api/giftcode-runs/${id}`, null, false, COMBINED_SLUG);
    byId('runModalTitle').textContent = `Run for ${run.code}`;
    const why = run.stop_reason && GIFT_STOP_TEXT[run.stop_reason] ? `<p>${escapeHtml(GIFT_STOP_TEXT[run.stop_reason])}</p>` : '';
    byId('runModalBody').innerHTML = `${why}<p>${escapeHtml(giftCountsText(run.counts))}.</p>`
      + giftProblemTable('Wrong kingdom: fix the number on the Players tab and run the code again', run.wrong_kingdom)
      + giftProblemTable('Other problems', run.problems);
    openModalById('runModal');
  } catch (e) { toast(e.message, true); }
}

function closeRunModal() { closeModalById('runModal'); }

byId('btnStartRun').addEventListener('click', startGiftRun);
bindActions(byId('giftActiveBody'), { cancel(btn) { cancelGiftRun(parseInt(btn.dataset.id, 10)); } });
bindActions(byId('giftRunsBody'), { details(btn) { openRunDetails(parseInt(btn.dataset.id, 10)); } });

VIEW_LOADERS.giftcodes = loadGiftcodes;
