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
          ${platformCell('Name', `${k.color ? `<span class="type-chip__dot" data-color="${escapeHtml(k.color)}"></span> ` : ''}${escapeHtml(k.name)}`)}
          ${platformCell('Slug', escapeHtml(k.slug))}
          ${platformCell('Actions', `<div class="row-actions">${actionButton('Edit', 'edit', { id: k.id })} ${actionButton('Invite coordinator', 'invite', { id: k.id })}</div>`)}
        </tr>`).join('')
      : emptyRow(3, 'No kingdoms yet.');
    applyTypeColors(byId('kingdomsBody'));
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
const KINGDOM_DEFAULT_COLOR = '#C9A227';

function editKingdom(k) {
  byId('kgId').value = k.id;
  byId('kgName').value = k.name;
  byId('kgSlug').value = k.slug;
  byId('kgGameNumber').value = k.game_number || '';
  byId('kgPublicTitle').value = k.public_site_title || '';
  byId('kgAdminTitle').value = k.admin_console_title || '';
  byId('kgColor').value = k.color || KINGDOM_DEFAULT_COLOR;
  byId('kgColorDefault').checked = !k.color;
  byId('kgColor').disabled = !k.color;
  renderKingdomLocales(k);
  openModalById('kingdomModal');
}

// Spec §72.2: which shipped languages the public pages offer, and the default.
function renderKingdomLocales(k) {
  const enabled = new Set(k.enabled_locales || ['en']);
  byId('kgLocales').innerHTML = (k.available_locales || []).map((l) =>
    `<label class="locale-picker__item"><input type="checkbox" data-locale="${escapeHtml(l.tag)}"${enabled.has(l.tag) ? ' checked' : ''}>
      <span lang="${escapeHtml(l.tag)}">${escapeHtml(l.name)}</span>
      <span class="locale-picker__tag">${escapeHtml(l.tag)}${l.dir === 'rtl' ? ' · right to left' : ''}</span>
      ${l.reviewed ? '' : '<span class="locale-picker__badge">Draft</span>'}</label>`).join('');
  fillDefaultLocale(k.default_locale || 'en');
}
function checkedLocales() {
  return [...byId('kgLocales').querySelectorAll('input[data-locale]:checked')].map((i) => i.dataset.locale);
}
function fillDefaultLocale(selected) {
  const names = {};
  byId('kgLocales').querySelectorAll('input[data-locale]').forEach((i) => { names[i.dataset.locale] = i.parentElement.querySelector('span').textContent; });
  const tags = checkedLocales();
  byId('kgDefaultLocale').innerHTML = tags.map((tag) => `<option value="${escapeHtml(tag)}"${tag === selected ? ' selected' : ''}>${escapeHtml(names[tag])}</option>`).join('');
}
byId('kgLocales').addEventListener('change', () => fillDefaultLocale(byId('kgDefaultLocale').value));

function closeKingdomModal() {
  closeModalById('kingdomModal');
}

byId('kgColorDefault').addEventListener('change', (e) => { byId('kgColor').disabled = e.target.checked; });

async function saveKingdomModal() {
  const k = KINGDOMS.find((x) => x.id === parseInt(byId('kgId').value, 10));
  if (!k) return;
  const name = byId('kgName').value.trim();
  const slug = byId('kgSlug').value.trim();
  if (!name || !slug) { toast('Name and slug are required', true); return; }
  const payload = {};
  if (name !== k.name) payload.name = name;
  if (slug !== k.slug) payload.slug = slug;
  const gameNumber = byId('kgGameNumber').value.trim() === '' ? null : parseInt(byId('kgGameNumber').value, 10);
  if (gameNumber !== null && !(gameNumber >= 1)) { toast('The game kingdom number must be a positive whole number', true); return; }
  if (gameNumber !== (k.game_number || null)) payload.game_number = gameNumber;
  const publicTitle = byId('kgPublicTitle').value.trim();
  const adminTitle = byId('kgAdminTitle').value.trim();
  if (publicTitle !== (k.public_site_title || '')) payload.public_site_title = publicTitle;
  if (adminTitle !== (k.admin_console_title || '')) payload.admin_console_title = adminTitle;
  if (byId('kgColorDefault').checked) {
    if (k.color) payload.color = '';
  } else if (byId('kgColor').value.toUpperCase() !== (k.color || '')) {
    payload.color = byId('kgColor').value;
  }
  const locales = checkedLocales(), defaultLocale = byId('kgDefaultLocale').value;
  if (!locales.length) { toast('Enable at least one language', true); return; }
  if (locales.join() !== (k.enabled_locales || ['en']).join()) payload.enabled_locales = locales;
  if (defaultLocale !== (k.default_locale || 'en')) payload.default_locale = defaultLocale;
  if (!Object.keys(payload).length) { toast('No changes made'); closeKingdomModal(); return; }
  try {
    const saved = await api('PATCH', `/api/kingdoms/${k.id}`, payload, true);
    toast(saved.color_note || 'Kingdom updated');
    closeKingdomModal();
    KINGDOM_NAMES_CACHE = null;
    loadPlatformKingdoms();
  } catch (e) { toast(e.message, true); }
}
byId('btnSaveKingdomModal').addEventListener('click', saveKingdomModal);

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
  // A legacy non-hex color cannot be shown in a color input; it is left alone
  // unless the person picks a new one (the field is only sent once touched).
  byId('tnColor').value = t && /^#[0-9a-f]{6}$/i.test(t.color || '') ? t.color : '#475569';
  byId('tnColor').dataset.touched = '';
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
      const saved = await api('PATCH', `/api/tenants/${id}`, {
        name, slug, server_id: serverId, secondary_server_ids: selectedSecondaryServerIds(), icon_image_data: iconData || '',
        ...(byId('tnColor').dataset.touched ? { color: byId('tnColor').value } : {}),
      }, true);
      toast((saved && saved.color_note) || 'Alliance updated');
    } else {
      await api('POST', '/api/tenants', {
        kingdom_id: parseInt(byId('tnKingdom').value, 10), name, slug, server_id: serverId, secondary_server_ids: selectedSecondaryServerIds(), icon_image_data: iconData || null,
        color: byId('tnColor').value,
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
byId('tnColor').addEventListener('input', (e) => { e.target.dataset.touched = '1'; });
byId('btnSaveTenantModal').addEventListener('click', saveTenantModal);
