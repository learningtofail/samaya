// Setup tab, "Platform" sections (superadmin only): Kingdoms and their
// coordinators, Discord servers (each in one Kingdom), alliances (tenants) with
// their primary and secondary servers, and users.
// Depends on common.js.

async function loadPlatform() {
  await Promise.all([loadPlatformKingdoms(), loadPlatformKingdomCoordinators(), loadPlatformDiscordServers(),
    loadPlatformTenants(), loadPlatformUsers()]);
}

function platformCell(label, html) {
  return `<td class="pf-v6-c-table__td" data-label="${escapeHtml(label)}">${html}</td>`;
}

function actionButton(label, action, data, variant) {
  const attrs = Object.keys(data).map((k) => `data-${k}="${escapeHtml(String(data[k]))}"`).join(' ');
  return `<button type="button" class="pf-v6-c-button pf-m-${variant || 'secondary'} pf-m-small" data-action="${action}" ${attrs}>${escapeHtml(label)}</button>`;
}

// ── Discord servers ──────────────────────────────────────────
let DISCORD_SERVERS = [];

async function loadPlatformDiscordServers() {
  try {
    DISCORD_SERVERS = await api('GET', '/api/discord-servers', null, true);
    const kingdomNames = await ensureKingdomNamesLoaded();
    byId('discordServersBody').innerHTML = DISCORD_SERVERS.length
      ? DISCORD_SERVERS.map((s) => `<tr class="pf-v6-c-table__tr">
          ${platformCell('Name', escapeHtml(s.name))}
          ${platformCell('Kingdom', escapeHtml(kingdomNames[s.kingdom_id] || String(s.kingdom_id)))}
          ${platformCell('Guild ID', `<span class="samaya-muted">${escapeHtml(s.guild_id)}</span>`)}
          ${platformCell('Bot', s.has_own_bot_token ? 'Own bot' : 'Platform bot')}
          ${platformCell('Actions', actionButton('Edit', 'edit', { id: s.id }))}
        </tr>`).join('')
      : emptyRow(5, 'No Discord servers yet.');
  } catch (e) { toast(e.message, true); }
}

async function openServerForm(s) {
  const kingdoms = await api('GET', '/api/kingdoms', null, true);
  if (!kingdoms.length) { toast('Create a kingdom first', true); return; }
  byId('serverModalTitleText').textContent = s ? 'Edit Discord server' : 'New Discord server';
  byId('srvId').value = s ? s.id : '';
  byId('srvName').value = s ? s.name : '';
  byId('srvGuild').value = s ? s.guild_id : '';
  byId('srvKingdom').innerHTML = optionsHtml(kingdoms.map((k) => ({ value: k.id, label: k.name })), s ? s.kingdom_id : kingdoms[0].id);
  byId('srvToken').value = '';
  byId('srvClearToken').checked = false;
  byId('srvClearGroup').classList.toggle('hidden', !(s && s.has_own_bot_token));
  byId('srvTokenHelp').textContent = s && s.has_own_bot_token
    ? 'This server has its own bot token. Leave blank to keep it, or paste a new one.'
    : 'Leave blank to use the shared platform bot, or paste a token to give this server its own. The token is never shown again.';
  openModalById('serverModal');
}

function closeServerModal() {
  closeModalById('serverModal');
}

// bot_token is write-only (never returned by the API), so it cannot be
// pre-filled. An empty string clears it back to the platform bot.
async function saveServerModal() {
  const id = byId('srvId').value;
  const name = byId('srvName').value.trim();
  const guildId = byId('srvGuild').value.trim();
  const kingdomId = parseInt(byId('srvKingdom').value, 10);
  const token = byId('srvToken').value.trim();
  if (!name || !guildId) { toast('Name and guild ID are required', true); return; }
  try {
    if (id) {
      const s = DISCORD_SERVERS.find((x) => x.id === parseInt(id, 10));
      const payload = {};
      if (s.name !== name) payload.name = name;
      if (s.guild_id !== guildId) payload.guild_id = guildId;
      if (s.kingdom_id !== kingdomId) payload.kingdom_id = kingdomId;
      if (token) payload.bot_token = token;
      else if (byId('srvClearToken').checked) payload.bot_token = '';
      if (!Object.keys(payload).length) { toast('No changes made'); closeServerModal(); return; }
      await api('PATCH', `/api/discord-servers/${id}`, payload, true);
      toast('Discord server updated');
      clearDiscordListCache();
    } else {
      await api('POST', '/api/discord-servers', { name, guild_id: guildId, kingdom_id: kingdomId, bot_token: token || null }, true);
      toast('Discord server created. It can now be assigned to an alliance.');
    }
    closeServerModal();
    loadPlatformDiscordServers();
  } catch (e) { toast(e.message, true); }
}

bindActions(byId('discordServersBody'), {
  edit(btn) {
    const s = DISCORD_SERVERS.find((x) => x.id === parseInt(btn.dataset.id, 10));
    if (s) openServerForm(s);
  },
});

// ── Kingdoms and coordinators ────────────────────────────────
let KINGDOMS = [];

async function loadPlatformKingdoms() {
  try {
    KINGDOMS = await api('GET', '/api/kingdoms', null, true);
    byId('kingdomsBody').innerHTML = KINGDOMS.length
      ? KINGDOMS.map((k) => `<tr class="pf-v6-c-table__tr">
          ${platformCell('Name', escapeHtml(k.name))}
          ${platformCell('Slug', escapeHtml(k.slug))}
          ${platformCell('Actions', `<div class="row-actions">${actionButton('Edit', 'edit', { id: k.id })} ${actionButton('Invite coordinator', 'invite', { id: k.id })}</div>`)}
        </tr>`).join('')
      : emptyRow(3, 'No kingdoms yet.');
  } catch (e) { toast(e.message, true); }
}

async function createKingdom() {
  const name = prompt('Kingdom name (for example "Kingdom 138"):');
  if (!name) return;
  const slug = prompt('URL slug (lowercase, no spaces, for example "k138"):', name.toLowerCase().replace(/[^a-z0-9]+/g, ''));
  if (!slug) return;
  try {
    await api('POST', '/api/kingdoms', { name, slug }, true);
    toast('Kingdom created');
    KINGDOM_NAMES_CACHE = null;
    loadPlatformKingdoms();
  } catch (e) { toast(e.message, true); }
}

// Blank branding titles clear back to the built-in defaults.
async function editKingdom(k) {
  const name = prompt('Kingdom name:', k.name);
  if (name === null) return;
  const slug = prompt('URL slug:', k.slug);
  if (slug === null) return;
  const publicTitle = prompt('Public events page title (blank for the default "Kingshot Event Schedule"):', k.public_site_title || '');
  if (publicTitle === null) return;
  const adminTitle = prompt('Admin console title (blank for the default "Samaya"):', k.admin_console_title || '');
  if (adminTitle === null) return;
  const payload = {};
  if (name !== k.name) payload.name = name;
  if (slug !== k.slug) payload.slug = slug;
  if (publicTitle !== (k.public_site_title || '')) payload.public_site_title = publicTitle;
  if (adminTitle !== (k.admin_console_title || '')) payload.admin_console_title = adminTitle;
  if (!Object.keys(payload).length) { toast('No changes made'); return; }
  try {
    await api('PATCH', `/api/kingdoms/${k.id}`, payload, true);
    toast('Kingdom updated');
    KINGDOM_NAMES_CACHE = null;
    loadPlatformKingdoms();
  } catch (e) { toast(e.message, true); }
}

bindActions(byId('kingdomsBody'), {
  edit(btn) {
    const k = KINGDOMS.find((x) => x.id === parseInt(btn.dataset.id, 10));
    if (k) editKingdom(k);
  },
  async invite(btn) {
    try {
      const inv = await api('POST', '/api/kingdom-invites', { kingdom_id: parseInt(btn.dataset.id, 10) }, true);
      copyInviteLink(inviteLinkFor(inv.token));
    } catch (e) { toast(e.message, true); }
  },
});

// GET /api/kingdom-coordinators is scoped to one kingdom, so this fans out.
async function loadPlatformKingdomCoordinators() {
  try {
    const kingdoms = await api('GET', '/api/kingdoms', null, true);
    const perKingdom = await Promise.all(kingdoms.map(async (k) => {
      const coords = await api('GET', `/api/kingdom-coordinators?kingdom_id=${k.id}`, null, true);
      return coords.map((c) => ({ ...c, kingdom_name: k.name }));
    }));
    const rows = perKingdom.flat();
    byId('kingdomCoordinatorsBody').innerHTML = rows.length
      ? rows.map((c) => `<tr class="pf-v6-c-table__tr">
          ${platformCell('Person', escapeHtml(c.discord_username))}
          ${platformCell('Kingdom', escapeHtml(c.kingdom_name))}
          ${platformCell('Actions', actionButton('Remove', 'remove', { id: c.id, name: c.discord_username, kingdom: c.kingdom_name }, 'danger'))}
        </tr>`).join('')
      : emptyRow(3, 'No kingdom coordinators yet.');
  } catch (e) { toast(e.message, true); }
}

bindActions(byId('kingdomCoordinatorsBody'), {
  async remove(btn) {
    if (!confirm(`Remove ${btn.dataset.name}'s kingdom-coordinator access to ${btn.dataset.kingdom}? Their membership of individual alliances is not affected.`)) return;
    try {
      await api('DELETE', `/api/kingdom-coordinators/${btn.dataset.id}`, null, true);
      toast('Kingdom coordinator access removed');
      loadPlatformKingdomCoordinators();
    } catch (e) { toast(e.message, true); }
  },
});

// ── Users ────────────────────────────────────────────────────
let PLATFORM_USERS = [];

async function loadPlatformUsers() {
  try {
    PLATFORM_USERS = await api('GET', '/api/users', null, true);
    byId('usersBody').innerHTML = PLATFORM_USERS.length ? PLATFORM_USERS.map(buildUserRow).join('') : emptyRow(5, 'No users yet.');
  } catch (e) { toast(e.message, true); }
}

function buildUserRow(u) {
  const isSelf = ME && ME.id === u.id;
  const lastLogin = u.last_login_at ? new Date(u.last_login_at).toUTCString().slice(0, 16) : 'Never';
  const toggle = isSelf && u.is_superadmin
    ? '<span class="samaya-muted" title="You cannot remove your own superadmin access">(you)</span>'
    : actionButton(u.is_superadmin ? 'Revoke superadmin' : 'Make superadmin', 'toggle', { id: u.id }, 'danger');
  return `<tr class="pf-v6-c-table__tr">
    ${platformCell('Discord name', escapeHtml(u.discord_username))}
    ${platformCell('Display name', u.display_name ? escapeHtml(u.display_name) : '<span class="samaya-muted">Not set (shows as Team)</span>')}
    ${platformCell('Superadmin', u.is_superadmin ? pfLabel('Superadmin', 'pf-m-red') : '')}
    ${platformCell('Last login', `<span class="samaya-muted">${escapeHtml(lastLogin)}</span>`)}
    ${platformCell('Actions', `<div class="row-actions">${actionButton('Edit display name', 'name', { id: u.id })} ${toggle}</div>`)}
  </tr>`;
}

bindActions(byId('usersBody'), {
  async name(btn) {
    const u = PLATFORM_USERS.find((x) => x.id === parseInt(btn.dataset.id, 10));
    if (!u) return;
    const next = prompt(`Display name for ${u.discord_username}, shown on public feedback responses (blank clears it):`, u.display_name || '');
    if (next === null || next.trim() === (u.display_name || '')) return;
    try {
      await api('PATCH', `/api/users/${u.id}`, { display_name: next.trim() }, true);
      if (ME && ME.id === u.id) ME.display_name = next.trim() || null;
      toast('Display name updated');
      loadPlatformUsers();
    } catch (e) { toast(e.message, true); }
  },
  async toggle(btn) {
    const u = PLATFORM_USERS.find((x) => x.id === parseInt(btn.dataset.id, 10));
    if (!u) return;
    const verb = u.is_superadmin ? 'Revoke' : 'Grant';
    if (!confirm(`${verb} superadmin access ${u.is_superadmin ? 'from' : 'to'} ${u.discord_username}? Superadmin can see and edit every alliance and kingdom here.`)) return;
    try {
      await api('PATCH', `/api/users/${u.id}`, { is_superadmin: !u.is_superadmin }, true);
      toast('Superadmin ' + (u.is_superadmin ? 'revoked' : 'granted'));
      loadPlatformUsers();
    } catch (e) { toast(e.message, true); }
  },
});

// ── Alliances (tenants) ──────────────────────────────────────
const TENANT_ICON_MAX_BYTES = 8 * 1024 * 1024;
let PLATFORM_TENANTS = [];

async function loadPlatformTenants() {
  try {
    PLATFORM_TENANTS = await api('GET', '/api/tenants', null, true);
    const names = await ensureKingdomNamesLoaded();
    byId('tenantsBody').innerHTML = PLATFORM_TENANTS.length
      ? PLATFORM_TENANTS.map((t) => `<tr class="pf-v6-c-table__tr">
          ${platformCell('Alliance', `${t.icon_image_data ? `<img class="tenant-icon" src="${escapeHtml(t.icon_image_data)}" alt="">` : `<span class="type-chip__dot" data-color="${escapeHtml(t.color)}"></span>`} ${escapeHtml(t.name)}`)}
          ${platformCell('Slug', escapeHtml(t.slug))}
          ${platformCell('Kingdom', escapeHtml(names[t.kingdom_id] || String(t.kingdom_id)))}
          ${platformCell('Discord server(s)', `${serversLineHtml(t)}<div class="samaya-muted">${escapeHtml(t.guild_id)}</div>`)}
          ${platformCell('Actions', actionButton('Edit', 'edit', { id: t.id }))}
        </tr>`).join('')
      : emptyRow(5, 'No alliances yet.');
    applyTypeColors(byId('tenantsBody'));
  } catch (e) { toast(e.message, true); }
}

async function openTenantForm(t) {
  const kingdoms = await api('GET', '/api/kingdoms', null, true);
  if (!t && !kingdoms.length) { toast('Create a kingdom first', true); return; }
  if (!DISCORD_SERVERS.length) { toast('Create a Discord server first', true); return; }
  byId('tenantModalTitleText').textContent = t ? 'Edit alliance' : 'New alliance';
  byId('tnId').value = t ? t.id : '';
  byId('tnName').value = t ? t.name : '';
  byId('tnSlug').value = t ? t.slug : '';
  byId('tnKingdom').innerHTML = optionsHtml(kingdoms.map((k) => ({ value: k.id, label: k.name })), t ? t.kingdom_id : '');
  byId('tnKingdom').disabled = !!t;
  renderTenantServerChoices(kingdoms, t);
  setTenantIconPreview(t ? (t.icon_image_data || '') : '');
  byId('tnIconFile').value = '';
  openModalById('tenantModal');
}

// Servers must belong to the alliance's Kingdom, so both lists follow the
// Kingdom select. The primary is excluded from the secondary choices.
function renderTenantServerChoices(kingdoms, t, keepPrimary) {
  const kingdomId = parseInt(byId('tnKingdom').value, 10) || (t && t.kingdom_id);
  const pool = DISCORD_SERVERS.filter((s) => s.kingdom_id === kingdomId);
  const primary = keepPrimary || (t && pool.some((s) => s.id === t.server_id) ? t.server_id : (pool[0] || {}).id);
  byId('tnServer').innerHTML = optionsHtml(pool.map((s) => ({ value: s.id, label: `${s.name} (${s.guild_id})` })), primary);
  const chosen = new Set(t ? (t.secondary_servers || []).map((s) => s.id) : []);
  const others = pool.filter((s) => String(s.id) !== String(byId('tnServer').value));
  byId('tnSecondary').innerHTML = others.length
    ? others.map((s) => `<label class="check"><input type="checkbox" value="${s.id}"${chosen.has(s.id) ? ' checked' : ''}> ${escapeHtml(s.name)} (${escapeHtml(s.guild_id)})</label>`).join('')
    : '<p class="samaya-muted">No other servers in this Kingdom.</p>';
}

function selectedSecondaryServerIds() {
  return Array.from(byId('tnSecondary').querySelectorAll('input:checked')).map((i) => parseInt(i.value, 10));
}

byId('tnKingdom').addEventListener('change', () => renderTenantServerChoices(null, null));
byId('tnServer').addEventListener('change', () => {
  const keep = selectedSecondaryServerIds();
  const t = { secondary_servers: keep.map((id) => ({ id })) };
  renderTenantServerChoices(null, t, byId('tnServer').value);
});

function closeTenantModal() {
  closeModalById('tenantModal');
}

function setTenantIconPreview(dataUri) {
  byId('tnIconData').value = dataUri || '';
  const img = byId('tnIconPreview');
  img.classList.toggle('hidden', !dataUri);
  img.src = dataUri || '';
  if (!dataUri) img.removeAttribute('src');
  byId('tnIconRemove').classList.toggle('hidden', !dataUri);
}

async function saveTenantModal() {
  const id = byId('tnId').value;
  const name = byId('tnName').value.trim();
  const slug = byId('tnSlug').value.trim();
  const serverId = parseInt(byId('tnServer').value, 10);
  const iconData = byId('tnIconData').value;
  if (!name || !slug) { toast('Name and slug are required', true); return; }
  try {
    if (id) {
      await api('PATCH', `/api/tenants/${id}`, {
        name, slug, server_id: serverId, secondary_server_ids: selectedSecondaryServerIds(), icon_image_data: iconData || '',
      }, true);
      toast('Alliance updated');
    } else {
      await api('POST', '/api/tenants', {
        kingdom_id: parseInt(byId('tnKingdom').value, 10), name, slug, server_id: serverId, secondary_server_ids: selectedSecondaryServerIds(), icon_image_data: iconData || null,
      }, true);
      toast(`${name} created. Invite its leaders from the Access section.`);
    }
    closeTenantModal();
    await loadTenants();
    if (typeof loadSetup === 'function') loadSetup();
  } catch (e) { toast(e.message, true); }
}

bindActions(byId('tenantsBody'), {
  edit(btn) {
    const t = PLATFORM_TENANTS.find((x) => x.id === parseInt(btn.dataset.id, 10));
    if (t) openTenantForm(t);
  },
});
byId('btnCreateKingdom').addEventListener('click', createKingdom);
byId('btnCreateDiscordServer').addEventListener('click', () => openServerForm(null));
byId('btnSaveServerModal').addEventListener('click', saveServerModal);
byId('btnCreateTenant').addEventListener('click', () => openTenantForm(null));
byId('tnIconFile').addEventListener('change', async (e) => {
  const file = e.target.files && e.target.files[0];
  const data = await readImageFile(file, TENANT_ICON_MAX_BYTES);
  if (data) setTenantIconPreview(data);
  else e.target.value = '';
});
byId('tnIconRemove').addEventListener('click', () => { setTenantIconPreview(''); byId('tnIconFile').value = ''; });
byId('btnSaveTenantModal').addEventListener('click', saveTenantModal);
