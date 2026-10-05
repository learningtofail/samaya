// Players tab (#v-players): one alliance's list of player IDs (spec §80.3). The
// list is leaders-only and never public. State lives in PLAYERS; render*()
// functions draw from it. Depends on common.js.

const PLAYERS = { slug: '', rows: [], missing: 0, gameNumber: null, writable: false, editing: null };

function playersSlug() { return singleSlugFor('players'); }

async function loadPlayers() {
  renderAllianceFilterSelect('playersAlliance', 'players', loadPlayers, false);
  PLAYERS.slug = playersSlug();
  const tenant = tenantBySlug(PLAYERS.slug);
  PLAYERS.writable = !!tenant && canWriteTenant(tenant);
  byId('playersAddPanel').classList.toggle('hidden', !PLAYERS.writable);
  byId('btnExportPlayers').classList.toggle('hidden', !PLAYERS.writable);
  byId('playersAddResult').textContent = '';
  try {
    const data = await api('GET', '/api/players', null, false, PLAYERS.slug);
    PLAYERS.rows = data.players;
    PLAYERS.missing = data.missing_kingdom;
    PLAYERS.gameNumber = data.game_numbers[PLAYERS.slug] || null;
    renderPlayers();
  } catch (e) {
    toast(e.message, true);
    byId('playersBody').innerHTML = emptyRow(5, 'Could not load players: ' + e.message);
  }
}

function playerRowHtml(p) {
  const kid = p.kid
    ? escapeHtml(String(p.kid))
    : (p.effective_kid ? escapeHtml(String(p.effective_kid)) + ' <span class="samaya-muted">(Kingdom)</span>' : pfLabel('Missing', 'pf-m-red'));
  const actions = PLAYERS.writable
    ? `<div class="row-actions">
        <button type="button" class="pf-v6-c-button pf-m-secondary pf-m-small" data-action="edit" data-id="${p.id}">Edit</button>
        <button type="button" class="pf-v6-c-button pf-m-link pf-m-danger pf-m-small" data-action="remove" data-id="${p.id}">Remove</button>
      </div>`
    : '<span class="samaya-muted">Read only</span>';
  return `<tr class="pf-v6-c-table__tr">
    <td class="pf-v6-c-table__td" data-label="Player ID"><code>${escapeHtml(p.fid)}</code></td>
    <td class="pf-v6-c-table__td" data-label="Kingdom">${kid}</td>
    <td class="pf-v6-c-table__td" data-label="Name">${escapeHtml(p.name || '')}</td>
    <td class="pf-v6-c-table__td" data-label="Note">${escapeHtml(p.note || '')}</td>
    <td class="pf-v6-c-table__td" data-label="Actions">${actions}</td>
  </tr>`;
}

function renderPlayers() {
  const n = PLAYERS.rows.length;
  const parts = [`${n} player${n === 1 ? '' : 's'}`];
  if (PLAYERS.missing) parts.push(`${PLAYERS.missing} without a kingdom number`);
  if (!PLAYERS.gameNumber) parts.push('the Kingdom has no game number yet (Setup, Kingdoms)');
  byId('playersCount').textContent = parts.join(', ');
  byId('playersBody').innerHTML = n ? PLAYERS.rows.map(playerRowHtml).join('') : emptyRow(5, 'No players yet.');
}

function renderAddResult(out) {
  const box = byId('playersAddResult');
  box.textContent = '';
  const summary = document.createElement('p');
  summary.textContent = `Added ${out.added}, updated ${out.updated}, already listed ${out.duplicates}.`;
  box.appendChild(summary);
  if (out.in_other_alliance.length) {
    const p = document.createElement('p');
    p.textContent = `${out.in_other_alliance.length} already on another alliance's list and left alone: ${out.in_other_alliance.slice(0, 10).join(', ')}${out.in_other_alliance.length > 10 ? ', and more' : ''}.`;
    box.appendChild(p);
  }
  if (out.rejected.length) {
    const list = document.createElement('ul');
    out.rejected.slice(0, 10).forEach((r) => {
      const li = document.createElement('li');
      li.textContent = (r.line ? `Line ${r.line}: ` : '') + r.reason;
      list.appendChild(li);
    });
    box.appendChild(list);
  }
}

async function addPlayers() {
  const text = byId('playersText').value;
  if (!text.trim()) { toast('Paste at least one player ID', true); return; }
  try {
    const out = await api('POST', '/api/players/bulk', { text }, false, PLAYERS.slug);
    byId('playersText').value = '';
    await loadPlayers();
    renderAddResult(out);
  } catch (e) { toast(e.message, true); }
}

async function exportPlayers() {
  try {
    const res = await fetch('/admin/api/players/export', { headers: { 'X-Tenant-Slug': PLAYERS.slug }, credentials: 'same-origin' });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement('a');
    a.href = url;
    a.download = `players-${PLAYERS.slug}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) { toast(e.message, true); }
}

function openPlayerModal(p) {
  PLAYERS.editing = p;
  byId('plId').value = p.id;
  byId('plKid').value = p.kid || '';
  byId('plName').value = p.name || '';
  byId('plNote').value = p.note || '';
  byId('plKidHelp').textContent = PLAYERS.gameNumber
    ? `Leave blank to use the Kingdom's number, ${PLAYERS.gameNumber}.`
    : 'The Kingdom has no game number set, so this player needs their own.';
  byId('plAlliance').innerHTML = optionsHtml(writableTenants().map((t) => ({ value: t.slug, label: t.name })), PLAYERS.slug);
  openModalById('playerModal');
}

function closePlayerModal() { closeModalById('playerModal'); }

async function savePlayer() {
  const p = PLAYERS.editing;
  if (!p) return;
  const kidText = byId('plKid').value.trim();
  const kid = kidText === '' ? null : parseInt(kidText, 10);
  if (kid !== null && !(kid >= 1 && kid <= 999999)) { toast('The kingdom number must be between 1 and 999999', true); return; }
  const payload = {};
  if (kid !== (p.kid || null)) payload.kid = kid;
  if (byId('plName').value.trim() !== (p.name || '')) payload.name = byId('plName').value.trim();
  if (byId('plNote').value.trim() !== (p.note || '')) payload.note = byId('plNote').value.trim();
  if (byId('plAlliance').value !== PLAYERS.slug) payload.tenant_slug = byId('plAlliance').value;
  if (!Object.keys(payload).length) { closePlayerModal(); return; }
  try {
    await api('PATCH', `/api/players/${p.id}`, payload, false, PLAYERS.slug);
    closePlayerModal();
    toast('Player updated');
    loadPlayers();
  } catch (e) { toast(e.message, true); }
}

async function removePlayer(p) {
  if (!confirm(`Remove player ${p.fid} from this alliance? Their gift code results are removed too.`)) return;
  try {
    await api('DELETE', `/api/players/${p.id}`, null, false, PLAYERS.slug);
    toast('Player removed');
    loadPlayers();
  } catch (e) { toast(e.message, true); }
}

byId('btnAddPlayers').addEventListener('click', addPlayers);
byId('btnExportPlayers').addEventListener('click', exportPlayers);
byId('btnSavePlayer').addEventListener('click', savePlayer);
bindActions(byId('playersBody'), {
  edit(btn) { const p = PLAYERS.rows.find((r) => r.id === parseInt(btn.dataset.id, 10)); if (p) openPlayerModal(p); },
  remove(btn) { const p = PLAYERS.rows.find((r) => r.id === parseInt(btn.dataset.id, 10)); if (p) removePlayer(p); },
});

VIEW_LOADERS.players = loadPlayers;
