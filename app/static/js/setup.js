// Setup tab (#v-setup, spec §66.7 and §68): your public display name, the
// Kingdom's Audiences (named lists of Discord server and channel destinations,
// with the alliances that use each one) and, for a superadmin, the platform
// sections that access.js and platform.js render. Depends on common.js and
// pickers.js.

let SETUP_AUDIENCES = [];      // every Audience of the Kingdoms the user can see
let KINGDOM_SERVERS = {};      // Kingdom id -> [{id, name}]
let AUD_ROWS = new Map();      // row number -> { server, channel, role } of the open Audience form
let AUD_ROW_SEQ = 0;

function loadSetup() {
  const sa = isSuperadmin();
  byId('setupDisplayName').value = ME && ME.display_name ? ME.display_name : '';
  byId('setupSuperadmin').classList.toggle('hidden', !sa);
  loadSetupAudiences();
  if (sa) {
    loadAccess();
    loadPlatform();
  }
}

// The Audience endpoints act on the Kingdom of the selected alliance, so a
// Kingdom is reached through any alliance of it the user can see.
function slugForKingdom(kingdomId) {
  const t = TENANTS.find((x) => x.kingdom_id === kingdomId);
  return t ? t.slug : undefined;
}

function manageableKingdomIds() {
  return Array.from(new Set(TENANTS.map((t) => t.kingdom_id))).filter(isKingdomCoordinator);
}

async function loadSetupAudiences() {
  if (!TENANTS.length) {
    byId('audiencesBody').innerHTML = emptyRow(4, 'No audiences yet.');
    return;
  }
  try {
    SETUP_AUDIENCES = await api('GET', '/api/audiences', null, false, '*');
  } catch (e) {
    toast(e.message, true);
    SETUP_AUDIENCES = [];
  }
  renderAudiencesPanel();
}

// ── Kingdom audiences ────────────────────────────────────────

function audienceFlagsHtml(a) {
  return a.leadership_only ? pfLabel('Leadership only', 'pf-m-orange') : '';
}

function audienceAlliancesHtml(a) {
  if (!a.links.length) return '<span class="samaya-muted">No alliance uses it</span>';
  return a.links.map((l) => `<div>${escapeHtml(l.alliance)} ${l.post_by_default ? pfLabel('Default', 'pf-m-green') : '<span class="samaya-muted">optional</span>'}</div>`).join('');
}

function renderAudiencesPanel() {
  const kingdoms = manageableKingdomIds();
  byId('btnCreateAudience').classList.toggle('hidden', !kingdoms.length);
  byId('audiencesBody').innerHTML = SETUP_AUDIENCES.length
    ? SETUP_AUDIENCES.map((a) => {
      const slug = slugForKingdom(a.kingdom_id);
      const actions = isKingdomCoordinator(a.kingdom_id)
        ? `<div class="row-actions">
            <button type="button" class="pf-v6-c-button pf-m-secondary pf-m-small" data-action="edit" data-id="${a.id}">Edit</button>
            <button type="button" class="pf-v6-c-button pf-m-danger pf-m-small" data-action="delete" data-id="${a.id}">Delete</button>
          </div>`
        : '<span class="samaya-muted">Read only</span>';
      return `<tr class="pf-v6-c-table__tr">
        <td class="pf-v6-c-table__td" data-label="Audience"><strong>${escapeHtml(a.label)}</strong><div class="label-stack">${audienceFlagsHtml(a)}</div></td>
        <td class="pf-v6-c-table__td" data-label="Destinations">${a.destinations.map((d) => `<div>${destinationHtml(d, slug)}</div>`).join('')}</td>
        <td class="pf-v6-c-table__td" data-label="Used by">${audienceAlliancesHtml(a)}</td>
        <td class="pf-v6-c-table__td" data-label="Actions">${actions}</td>
      </tr>`;
    }).join('')
    : emptyRow(4, 'No audiences yet. A Kingdom coordinator or superadmin creates them.');
  resolveDestinationNames(byId('audiencesBody'));
}

async function kingdomServers(kingdomId) {
  if (!KINGDOM_SERVERS[kingdomId]) {
    KINGDOM_SERVERS[kingdomId] = await api('GET', '/api/kingdom-servers', null, false, slugForKingdom(kingdomId));
  }
  return KINGDOM_SERVERS[kingdomId];
}

function audienceFormKingdom() {
  const existing = SETUP_AUDIENCES.find((x) => String(x.id) === byId('audId').value);
  return existing ? existing.kingdom_id : parseInt(byId('audKingdom').value, 10);
}

function addAudienceDestinationRow(servers, kingdomId, d) {
  const n = ++AUD_ROW_SEQ;
  const slug = slugForKingdom(kingdomId);
  const serverId = d ? d.server_id : servers[0].id;
  const li = document.createElement('li');
  li.className = 'aud-dest';
  li.dataset.row = String(n);
  li.innerHTML = `
    <div class="aud-dest__head">
      <label class="pf-v6-c-form__label" for="audDst${n}Server"><span class="pf-v6-c-form__label-text">Discord server</span></label>
      <button type="button" class="pf-v6-c-button pf-m-link pf-m-danger pf-m-small" data-action="remove-destination" data-row="${n}">Remove</button>
    </div>
    <select class="pf-v6-c-form-control" id="audDst${n}Server">${optionsHtml(servers.map((s) => ({ value: s.id, label: s.name })), serverId)}</select>
    <div id="audDst${n}Channel"></div>
    <div id="audDst${n}Role"></div>`;
  byId('audDestinations').appendChild(li);
  const row = { server: li.querySelector('select'), channel: null, role: null };
  const mount = (channel, role) => {
    const sid = row.server.value;
    row.channel = mountDiscordPicker(byId(`audDst${n}Channel`), {
      kind: 'channel', slug, serverId: sid, id: `audDst${n}ChannelSel`, label: 'Channel', value: channel, emptyLabel: 'Choose a channel',
    });
    row.role = mountDiscordPicker(byId(`audDst${n}Role`), {
      kind: 'role', slug, serverId: sid, id: `audDst${n}RoleSel`, label: 'Role (optional)', value: role, emptyLabel: 'No role',
      helper: 'Mentioned on events that have "mention the role" turned on.',
    });
  };
  mount(d ? d.channel_id : '', d ? d.role_id : '');
  row.server.addEventListener('change', () => mount('', ''));
  AUD_ROWS.set(n, row);
}

function renderAudienceLinks(kingdomId, links) {
  const alliances = TENANTS.filter((t) => t.kingdom_id === kingdomId);
  const byTenant = new Map((links || []).map((l) => [l.tenant_id, l]));
  byId('audLinks').innerHTML = alliances.length
    ? alliances.map((t) => {
      const link = byTenant.get(t.id);
      return `<li class="aud-link">
        <label class="check"><input type="checkbox" data-link="${t.id}"${link ? ' checked' : ''}> ${escapeHtml(t.name)}</label>
        <label class="check"><input type="checkbox" data-default="${t.id}"${link && link.post_by_default ? ' checked' : ''}${link ? '' : ' disabled'}> Post by default</label>
      </li>`;
    }).join('')
    : '<li class="samaya-muted">No alliances in this Kingdom.</li>';
}

byId('audLinks').addEventListener('change', (e) => {
  const box = e.target.closest('input[data-link]');
  if (!box) return;
  const def = byId('audLinks').querySelector(`input[data-default="${box.dataset.link}"]`);
  def.disabled = !box.checked;
  if (box.checked) def.checked = true; else def.checked = false;
});

async function openAudienceForm(a) {
  const kingdoms = manageableKingdomIds();
  if (!a && !kingdoms.length) return;
  const kingdomId = a ? a.kingdom_id : kingdoms[0];
  byId('audienceModalTitleText').textContent = a ? 'Edit audience' : 'New audience';
  byId('audId').value = a ? a.id : '';
  byId('audLabel').value = a ? a.label : '';
  byId('audLeadership').checked = a ? a.leadership_only : false;
  byId('audKingdomGroup').classList.toggle('hidden', !!a || kingdoms.length < 2);
  KINGDOM_NAMES_CACHE = KINGDOM_NAMES_CACHE || {};
  byId('audKingdom').innerHTML = optionsHtml(kingdoms.map((k) => ({ value: k, label: KINGDOM_NAMES_CACHE[k] || `Kingdom ${k}` })), kingdomId);
  byId('audDestinations').innerHTML = '';
  AUD_ROWS = new Map();
  let servers;
  try { servers = await kingdomServers(kingdomId); } catch (e) { toast(e.message, true); return; }
  if (!servers.length) { toast('This Kingdom has no Discord servers yet. Ask a superadmin to add one.', true); return; }
  (a && a.destinations.length ? a.destinations : [null]).forEach((d) => addAudienceDestinationRow(servers, kingdomId, d));
  renderAudienceLinks(kingdomId, a ? a.links : []);
  openModalById('audienceModal');
}

function closeAudienceModal() {
  closeModalById('audienceModal');
}

byId('audKingdom').addEventListener('change', async () => {
  const kingdomId = parseInt(byId('audKingdom').value, 10);
  byId('audDestinations').innerHTML = '';
  AUD_ROWS = new Map();
  try {
    const servers = await kingdomServers(kingdomId);
    if (servers.length) addAudienceDestinationRow(servers, kingdomId, null);
  } catch (e) { toast(e.message, true); }
  renderAudienceLinks(kingdomId, []);
});

byId('btnAddAudienceDestination').addEventListener('click', async () => {
  const kingdomId = audienceFormKingdom();
  try { addAudienceDestinationRow(await kingdomServers(kingdomId), kingdomId, null); } catch (e) { toast(e.message, true); }
});

byId('audDestinations').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-action="remove-destination"]');
  if (!btn) return;
  if (AUD_ROWS.size < 2) { toast('An audience needs at least one destination.', true); return; }
  AUD_ROWS.delete(parseInt(btn.dataset.row, 10));
  btn.closest('li').remove();
});

function collectAudienceDestinations() {
  const out = [];
  for (const row of AUD_ROWS.values()) {
    const channel = row.channel.value();
    const role = row.role.value();
    if (!channel) { toast('Choose a channel for every destination.', true); return null; }
    if (!/^\d+$/.test(channel) || (role && !/^\d+$/.test(role))) { toast('Channel and role IDs are digits only.', true); return null; }
    out.push({ server_id: parseInt(row.server.value, 10), channel_id: channel, role_id: role });
  }
  return out;
}

async function saveAudienceModal() {
  const id = byId('audId').value;
  const kingdomId = audienceFormKingdom();
  const label = byId('audLabel').value.trim();
  if (!label) { toast('Give the audience a label', true); return; }
  const destinations = collectAudienceDestinations();
  if (!destinations) return;
  const links = Array.from(byId('audLinks').querySelectorAll('input[data-link]:checked')).map((box) => ({
    tenant_id: parseInt(box.dataset.link, 10),
    post_by_default: byId('audLinks').querySelector(`input[data-default="${box.dataset.link}"]`).checked,
  }));
  const payload = { label, leadership_only: byId('audLeadership').checked, destinations, links };
  const btn = byId('btnSaveAudienceModal');
  btn.disabled = true;
  try {
    if (id) await api('PATCH', `/api/audiences/${id}`, payload, false, slugForKingdom(kingdomId));
    else await api('POST', '/api/audiences', payload, false, slugForKingdom(kingdomId));
    toast('Audience saved.');
    closeAudienceModal();
    loadSetupAudiences();
  } catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
}

byId('btnCreateAudience').addEventListener('click', () => openAudienceForm(null));
byId('btnSaveAudienceModal').addEventListener('click', saveAudienceModal);
bindActions(byId('audiencesBody'), {
  edit(btn) {
    const a = SETUP_AUDIENCES.find((x) => x.id === parseInt(btn.dataset.id, 10));
    if (a) openAudienceForm(a);
  },
  async delete(btn) {
    const a = SETUP_AUDIENCES.find((x) => x.id === parseInt(btn.dataset.id, 10));
    if (!a || !confirm(`Delete the audience "${a.label}"? Pending reminders to it are cancelled.`)) return;
    try {
      await api('DELETE', `/api/audiences/${a.id}`, null, false, slugForKingdom(a.kingdom_id));
      toast('Audience deleted.');
      loadSetupAudiences();
    } catch (e) { toast(e.message, true); }
  },
});

// ── Alliances' primary and secondary servers (also used by platform.js) ──

function serversLineHtml(t) {
  const secondary = (t.secondary_servers || []).map((s) => escapeHtml(s.name));
  return `<div><span class="samaya-muted">Primary server:</span> ${escapeHtml(t.server_name)}</div>
    ${secondary.length ? `<div><span class="samaya-muted">Also present on:</span> ${secondary.join(', ')}</div>` : ''}`;
}

byId('btnSaveDisplayName').addEventListener('click', async () => {
  const value = byId('setupDisplayName').value.trim();
  try {
    const res = await api('PATCH', '/api/me', { display_name: value }, true);
    ME.display_name = res.display_name;
    byId('setupDisplayName').value = res.display_name || '';
    toast(res.display_name ? `Public responses will show as ${res.display_name}.` : 'Display name cleared. Responses show as Team.');
  } catch (e) { toast(e.message, true); }
});

VIEW_LOADERS.setup = loadSetup;
