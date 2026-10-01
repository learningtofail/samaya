// Setup tab (#v-setup, spec §66.7 and §67): your public display name, each
// alliance's destinations (the Discord channels and roles its reminders and
// messages go to), Kingdom audience groups, and, for a superadmin, the
// platform sections that access.js and platform.js render. Depends on
// common.js and pickers.js.

let SETUP_DESTINATIONS = [];   // every destination of the alliances shown
let SETUP_GROUPS = [];
let SETUP_KINGDOM_DESTINATIONS = [];
const DST_PICKERS = { channel: null, role: null };

function loadSetup() {
  const sa = isSuperadmin();
  byId('setupDisplayName').value = ME && ME.display_name ? ME.display_name : '';
  byId('setupSuperadmin').classList.toggle('hidden', !sa);
  renderSetupAlliances();
  loadSetupGroups();
  if (sa) {
    loadAccess();
    loadPlatform();
  }
}

// ── Alliance destinations ────────────────────────────────────

function serversLineHtml(t) {
  const secondary = (t.secondary_servers || []).map((s) => escapeHtml(s.name));
  return `<div><span class="samaya-muted">Primary:</span> ${escapeHtml(t.server_name)}</div>
    ${secondary.length ? `<div><span class="samaya-muted">Secondary:</span> ${secondary.join(', ')}</div>` : ''}`;
}

function destinationFlagsHtml(d) {
  const flags = [];
  if (d.post_by_default) flags.push(pfLabel('Default', 'pf-m-green'));
  if (d.leadership_only) flags.push(pfLabel('Leadership only', 'pf-m-orange'));
  return flags.length ? `<div class="label-stack">${flags.join(' ')}</div>` : '<span class="samaya-muted">Opt in per event</span>';
}

function buildDestinationRow(d, t, can) {
  const primary = d.server_id === t.server_id;
  const actions = can
    ? `<div class="row-actions">
        <button type="button" class="pf-v6-c-button pf-m-secondary pf-m-small" data-action="edit-destination" data-id="${d.id}">Edit</button>
        <button type="button" class="pf-v6-c-button pf-m-danger pf-m-small" data-action="delete-destination" data-id="${d.id}">Delete</button>
      </div>`
    : '<span class="samaya-muted">Read only</span>';
  return `<tr class="pf-v6-c-table__tr">
    <td class="pf-v6-c-table__td" data-label="Label"><strong>${escapeHtml(d.label)}</strong></td>
    <td class="pf-v6-c-table__td" data-label="Server">${serverBadgeHtml(d.server_name, primary)}</td>
    <td class="pf-v6-c-table__td" data-label="Channel"><span class="dest-channel" data-server="${d.server_id}" data-slug="${escapeHtml(d.alliance_slug)}" data-channel="${escapeHtml(d.channel_id)}">${escapeHtml(d.channel_id)}</span></td>
    <td class="pf-v6-c-table__td" data-label="Role">${d.role_id ? `<span class="dest-role" data-server="${d.server_id}" data-slug="${escapeHtml(d.alliance_slug)}" data-role="${escapeHtml(d.role_id)}">${escapeHtml(d.role_id)}</span>` : '<span class="samaya-muted">None</span>'}</td>
    <td class="pf-v6-c-table__td" data-label="Posts">${destinationFlagsHtml(d)}</td>
    <td class="pf-v6-c-table__td" data-label="Actions">${actions}</td>
  </tr>`;
}

async function renderSetupAlliances() {
  const host = byId('setupAlliances');
  if (!TENANTS.length) { host.innerHTML = '<p class="samaya-empty">You have no alliances yet.</p>'; return; }
  try {
    SETUP_DESTINATIONS = await api('GET', '/api/destinations', null, false, '*');
  } catch (e) {
    toast(e.message, true);
    SETUP_DESTINATIONS = [];
  }
  host.innerHTML = TENANTS.map((t) => {
    const can = isOwnerOfTenant(t);
    const rows = SETUP_DESTINATIONS.filter((d) => d.tenant_id === t.id);
    return `<section class="alliance" aria-labelledby="alliance${t.id}Name">
      <h4 class="alliance__name" id="alliance${t.id}Name">${escapeHtml(t.name)}</h4>
      ${serversLineHtml(t)}
      <div class="table-wrap">
        <table class="pf-v6-c-table pf-m-grid-md responsive-table">
          <caption class="sr-only">Destinations for ${escapeHtml(t.name)}</caption>
          <thead><tr class="pf-v6-c-table__tr"><th class="pf-v6-c-table__th" scope="col">Label</th><th class="pf-v6-c-table__th" scope="col">Server</th><th class="pf-v6-c-table__th" scope="col">Channel</th><th class="pf-v6-c-table__th" scope="col">Role</th><th class="pf-v6-c-table__th" scope="col">Posts</th><th class="pf-v6-c-table__th" scope="col">Actions</th></tr></thead>
          <tbody>${rows.length ? rows.map((d) => buildDestinationRow(d, t, can)).join('') : emptyRow(6, 'No destinations yet. This alliance cannot receive reminders.')}</tbody>
        </table>
      </div>
      ${can
    ? `<button type="button" class="pf-v6-c-button pf-m-primary pf-m-small" data-action="add-destination" data-id="${t.id}">Add a destination for ${escapeHtml(t.name)}</button>`
    : `<p class="samaya-muted">Only an owner of ${escapeHtml(t.name)} or a superadmin can change these.</p>`}
    </section>`;
  }).join('');
  resolveDestinationNames(host);
}

// Channel and role IDs are replaced by names once Discord answers. A failure
// leaves the ID, which is still correct.
function resolveDestinationNames(host) {
  host.querySelectorAll('.dest-channel').forEach((el) => {
    loadDiscordList('channel', el.dataset.slug, serverArg(el.dataset.slug, el.dataset.server)).then(({ items }) => {
      const hit = items.find((c) => String(c.id) === el.dataset.channel);
      if (hit) el.textContent = '#' + hit.name;
    });
  });
  host.querySelectorAll('.dest-role').forEach((el) => {
    loadDiscordList('role', el.dataset.slug, serverArg(el.dataset.slug, el.dataset.server)).then(({ items }) => {
      const hit = items.find((r) => String(r.id) === el.dataset.role);
      if (hit) el.textContent = '@' + hit.name;
    });
  });
}

// The primary server is the default, so only a secondary needs the parameter.
function serverArg(slug, serverId) {
  const t = tenantBySlug(slug);
  return t && String(t.server_id) === String(serverId) ? undefined : serverId;
}

function mountDestinationPickers(t, serverId, channel, role) {
  const arg = serverArg(t.slug, serverId);
  DST_PICKERS.channel = mountDiscordPicker(byId('dstChannelHost'), {
    kind: 'channel', slug: t.slug, serverId: arg, id: 'dstChannel', label: 'Channel', value: channel, emptyLabel: 'Choose a channel',
  });
  DST_PICKERS.role = mountDiscordPicker(byId('dstRoleHost'), {
    kind: 'role', slug: t.slug, serverId: arg, id: 'dstRole', label: 'Role (optional)', value: role, emptyLabel: 'No role',
    helper: 'Mentioned on events that have "mention the role" turned on.',
  });
}

function openDestinationForm(t, d) {
  byId('destinationModalTitleText').textContent = d ? 'Edit destination' : 'New destination';
  byId('dstId').value = d ? d.id : '';
  byId('dstTenantId').value = t.id;
  byId('dstLabel').value = d ? d.label : '';
  const servers = allowedServers(t);
  const serverId = d ? d.server_id : t.server_id;
  byId('dstServer').innerHTML = optionsHtml(
    servers.map((s) => ({ value: s.id, label: `${s.name} (${s.primary ? 'primary' : 'secondary'})` })), serverId);
  byId('dstDefault').checked = d ? d.post_by_default : true;
  byId('dstLeadership').checked = d ? d.leadership_only : false;
  mountDestinationPickers(t, serverId, d ? d.channel_id : '', d ? d.role_id : '');
  openModalById('destinationModal');
}

function closeDestinationModal() {
  closeModalById('destinationModal');
}

byId('dstServer').addEventListener('change', () => {
  const t = tenantById(parseInt(byId('dstTenantId').value, 10));
  if (t) mountDestinationPickers(t, byId('dstServer').value, '', '');
});

async function saveDestinationModal() {
  const t = tenantById(parseInt(byId('dstTenantId').value, 10));
  if (!t) return;
  const id = byId('dstId').value;
  const channel = DST_PICKERS.channel.value();
  const role = DST_PICKERS.role.value();
  const label = byId('dstLabel').value.trim();
  if (!label) { toast('Give the destination a label', true); return; }
  if (!channel) { toast('Choose a channel', true); return; }
  if (!/^\d+$/.test(channel) || (role && !/^\d+$/.test(role))) { toast('Channel and role IDs are digits only.', true); return; }
  const payload = {
    label, server_id: parseInt(byId('dstServer').value, 10), channel_id: channel, role_id: role,
    post_by_default: byId('dstDefault').checked, leadership_only: byId('dstLeadership').checked,
  };
  const btn = byId('btnSaveDestinationModal');
  btn.disabled = true;
  try {
    if (id) await api('PATCH', `/api/destinations/${id}`, payload, false, t.slug);
    else await api('POST', '/api/destinations', payload, false, t.slug);
    toast(`Destination saved for ${t.name}.`);
    closeDestinationModal();
    renderSetupAlliances();
    loadSetupGroups();
  } catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
}
byId('btnSaveDestinationModal').addEventListener('click', saveDestinationModal);

bindActions(byId('setupAlliances'), {
  'add-destination'(btn) {
    const t = tenantById(parseInt(btn.dataset.id, 10));
    if (t) openDestinationForm(t, null);
  },
  'edit-destination'(btn) {
    const d = SETUP_DESTINATIONS.find((x) => x.id === parseInt(btn.dataset.id, 10));
    const t = d && tenantById(d.tenant_id);
    if (t) openDestinationForm(t, d);
  },
  async 'delete-destination'(btn) {
    const d = SETUP_DESTINATIONS.find((x) => x.id === parseInt(btn.dataset.id, 10));
    const t = d && tenantById(d.tenant_id);
    if (!t || !confirm(`Delete the destination "${d.label}"? Pending reminders to it are cancelled.`)) return;
    try {
      await api('DELETE', `/api/destinations/${d.id}`, null, false, t.slug);
      toast('Destination deleted.');
      renderSetupAlliances();
      loadSetupGroups();
    } catch (e) { toast(e.message, true); }
  },
});

// ── Audience groups ──────────────────────────────────────────

// The group endpoints act on the Kingdom of the selected alliance.
function slugForKingdom(kingdomId) {
  const t = TENANTS.find((x) => x.kingdom_id === kingdomId);
  return t ? t.slug : undefined;
}

function groupKingdomIds() {
  return Array.from(new Set(TENANTS.map((t) => t.kingdom_id))).filter(isKingdomCoordinator);
}

async function loadSetupGroups() {
  const panel = byId('setupGroupsPanel');
  const kingdoms = groupKingdomIds();
  panel.classList.toggle('hidden', !kingdoms.length);
  if (!kingdoms.length) return;
  try {
    const [perKingdom, destinations] = await Promise.all([
      Promise.all(kingdoms.map((k) => api('GET', '/api/audience-groups', null, false, slugForKingdom(k)))),
      api('GET', '/api/destinations?kingdom=true', null, false, '*'),
    ]);
    SETUP_GROUPS = perKingdom.flat();
    SETUP_KINGDOM_DESTINATIONS = destinations.filter((d) => kingdoms.includes((tenantById(d.tenant_id) || {}).kingdom_id));
  } catch (e) { toast(e.message, true); return; }
  byId('groupsBody').innerHTML = SETUP_GROUPS.length
    ? SETUP_GROUPS.map((g) => `<tr class="pf-v6-c-table__tr">
        <td class="pf-v6-c-table__td" data-label="Group"><strong>${escapeHtml(g.name)}</strong>${g.description ? `<div class="samaya-muted">${escapeHtml(g.description)}</div>` : ''}</td>
        <td class="pf-v6-c-table__td" data-label="Destinations">${g.destinations.length
    ? g.destinations.map((d) => `<div>${escapeHtml(d.alliance)}: ${escapeHtml(d.label)}${d.leadership_only ? ' (leadership only)' : ''}</div>`).join('')
    : '<span class="samaya-muted">Empty</span>'}</td>
        <td class="pf-v6-c-table__td" data-label="Actions"><div class="row-actions">
          <button type="button" class="pf-v6-c-button pf-m-secondary pf-m-small" data-action="edit" data-id="${g.id}">Edit</button>
          <button type="button" class="pf-v6-c-button pf-m-danger pf-m-small" data-action="delete" data-id="${g.id}">Delete</button>
        </div></td>
      </tr>`).join('')
    : emptyRow(3, 'No audience groups yet.');
}

function fillGroupDestinations(scope, chosen) {
  const choices = SETUP_KINGDOM_DESTINATIONS.filter((d) => (tenantById(d.tenant_id) || {}).kingdom_id === scope);
  byId('grpDestinations').innerHTML = choices.length
    ? choices.map((d) => `<label class="check"><input type="checkbox" value="${d.id}"${chosen.has(d.id) ? ' checked' : ''}> ${escapeHtml(d.alliance)}: ${escapeHtml(d.label)}${d.leadership_only ? ' (leadership only)' : ''}</label>`).join('')
    : '<p class="samaya-muted">No destinations exist yet. Alliance owners add them above.</p>';
}

byId('grpKingdom').addEventListener('change', () => fillGroupDestinations(parseInt(byId('grpKingdom').value, 10), new Set()));

function openGroupForm(g) {
  byId('groupModalTitleText').textContent = g ? 'Edit audience group' : 'New audience group';
  byId('grpId').value = g ? g.id : '';
  byId('grpName').value = g ? g.name : '';
  byId('grpDescription').value = g ? g.description : '';
  const chosen = new Set(g ? g.destination_ids : []);
  const kingdoms = groupKingdomIds();
  byId('grpKingdomGroup').classList.toggle('hidden', !!g || kingdoms.length < 2);
  KINGDOM_NAMES_CACHE = KINGDOM_NAMES_CACHE || {};
  byId('grpKingdom').innerHTML = optionsHtml(kingdoms.map((k) => ({ value: k, label: KINGDOM_NAMES_CACHE[k] || `Kingdom ${k}` })), kingdoms[0]);
  fillGroupDestinations(g ? g.kingdom_id : kingdoms[0], chosen);
  openModalById('groupModal');
}

function closeGroupModal() {
  closeModalById('groupModal');
}

async function saveGroupModal() {
  const id = byId('grpId').value;
  const group = SETUP_GROUPS.find((x) => String(x.id) === id);
  const kingdomId = group ? group.kingdom_id : parseInt(byId('grpKingdom').value, 10);
  const name = byId('grpName').value.trim();
  if (!name) { toast('Give the group a name', true); return; }
  const ids = Array.from(byId('grpDestinations').querySelectorAll('input:checked')).map((i) => parseInt(i.value, 10));
  const payload = { name, description: byId('grpDescription').value.trim(), destination_ids: ids };
  try {
    if (id) await api('PATCH', `/api/audience-groups/${id}`, payload, false, slugForKingdom(group.kingdom_id));
    else await api('POST', '/api/audience-groups', payload, false, slugForKingdom(kingdomId));
    toast('Audience group saved.');
    closeGroupModal();
    loadSetupGroups();
  } catch (e) { toast(e.message, true); }
}

byId('btnCreateGroup').addEventListener('click', () => openGroupForm(null));
byId('btnSaveGroupModal').addEventListener('click', saveGroupModal);
bindActions(byId('groupsBody'), {
  edit(btn) {
    const g = SETUP_GROUPS.find((x) => x.id === parseInt(btn.dataset.id, 10));
    if (g) openGroupForm(g);
  },
  async delete(btn) {
    const g = SETUP_GROUPS.find((x) => x.id === parseInt(btn.dataset.id, 10));
    if (!g || !confirm(`Delete the group "${g.name}"?`)) return;
    try {
      await api('DELETE', `/api/audience-groups/${g.id}`, null, false, slugForKingdom(g.kingdom_id));
      toast('Audience group deleted.');
      loadSetupGroups();
    } catch (e) { toast(e.message, true); }
  },
});

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
