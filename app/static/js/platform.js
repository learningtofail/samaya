// Platform view (#v-platform, superadmin-only — see applyRoleVisibility
// in common.js): Kingdom and Tenant creation, the "onboard a new
// alliance" surface. Depends on common.js.

async function loadPlatform() {
  await Promise.all([loadPlatformKingdoms(), loadPlatformKingdomCoordinators(), loadPlatformDiscordServers(), loadPlatformTenants(), loadPlatformUsers()]);
}

// Discord Servers (spec §25) — the shared guild/bot-credential entity
// Tenants now reference via server_id, instead of each carrying its own
// copy of guild_id/bot_token/public_key.
let DISCORD_SERVERS = [];

async function loadPlatformDiscordServers() {
  try {
    DISCORD_SERVERS = await api('GET', '/api/discord-servers', null, /*skipTenantHeader=*/true);
    document.getElementById('discordServersBody').innerHTML = DISCORD_SERVERS.length
      ? DISCORD_SERVERS.map(s =>
          '<tr class="pf-v6-c-table__tr">'
          + '<td class="pf-v6-c-table__td">' + escapeHtml(s.name) + '</td>'
          + '<td class="pf-v6-c-table__td" style="font-size:var(--fs-sm);color:var(--muted)">' + escapeHtml(s.guild_id) + '</td>'
          + '<td class="pf-v6-c-table__td">' + (s.has_own_bot_token ? 'Own bot' : 'Platform bot') + '</td>'
          + '<td class="pf-v6-c-table__td" style="font-size:var(--fs-sm);color:var(--muted)">' + (s.tenant_names.length ? escapeHtml(s.tenant_names.join(', ')) : '—') + '</td>'
          + '<td class="pf-v6-c-table__td">'
          + '<button class="pf-v6-c-button pf-m-secondary pf-m-small" onclick="editDiscordServer(' + escapeHtml(JSON.stringify(s)) + ')" '
          + 'title="Edit this server\'s name, guild ID, or bot credentials.">Edit</button>'
          + '</td>'
          + '</tr>'
        ).join('')
      : '<tr class="pf-v6-c-table__tr"><td class="pf-v6-c-table__td" colspan="5" style="color:var(--muted);padding:20px">No Discord servers yet.</td></tr>';
  } catch(e) { toast(e.message, true); }
}

async function createDiscordServer() {
  const name = prompt('Discord server name (e.g. "HTD"):');
  if (!name) return;
  const guildId = prompt('Discord guild (server) ID:');
  if (!guildId) return;
  const botToken = prompt('Bot token for this server (leave blank to use the shared platform bot):', '');

  try {
    await api('POST', '/api/discord-servers', {name, guild_id: guildId, bot_token: botToken || null}, /*skipTenantHeader=*/true);
    toast('Discord server created — it can now be assigned to a tenant below');
    loadPlatformDiscordServers();
  } catch(e) { toast(e.message, true); }
}

// Same prompt-based editing convention as editKingdom()/editTenant() —
// Cancel on any one field leaves it unchanged rather than aborting the
// whole edit. bot_token can't be pre-filled (write-only, never returned
// by the API), same reasoning as editTenant() below.
async function editDiscordServer(s) {
  const name = prompt('Server name:', s.name);
  const guildId = (name !== null) ? prompt('Discord guild (server) ID:', s.guild_id) : null;
  const tokenNote = s.has_own_bot_token
    ? 'This server has its own bot token set. Leave blank to keep it unchanged, or type CLEAR to remove it and fall back to the shared platform bot.'
    : 'This server currently uses the shared platform bot. Leave blank to keep it that way, or paste a bot token to give it its own.';
  const botTokenInput = (guildId !== null) ? prompt(tokenNote, '') : null;

  const payload = {};
  if (name !== null && name !== s.name)         payload.name = name;
  if (guildId !== null && guildId !== s.guild_id) payload.guild_id = guildId;
  if (botTokenInput !== null && botTokenInput !== '') {
    payload.bot_token = (botTokenInput.trim().toUpperCase() === 'CLEAR') ? '' : botTokenInput.trim();
  }

  if (!Object.keys(payload).length) {
    toast('No changes made');
    return;
  }

  try {
    await api('PATCH', `/api/discord-servers/${s.id}`, payload, /*skipTenantHeader=*/true);
    toast('Discord server updated');
    loadPlatformDiscordServers();
  } catch(e) { toast(e.message, true); }
}

async function loadPlatformKingdoms() {
  try {
    const kingdoms = await api('GET', '/api/kingdoms', null, /*skipTenantHeader=*/true);
    document.getElementById('kingdomsBody').innerHTML = kingdoms.length
      ? kingdoms.map(k =>
          '<tr class="pf-v6-c-table__tr"><td class="pf-v6-c-table__td">' + escapeHtml(k.name) + '</td><td class="pf-v6-c-table__td">' + escapeHtml(k.slug) + '</td>'
          + '<td class="pf-v6-c-table__td">'
          + '<button class="pf-v6-c-button pf-m-secondary pf-m-small" onclick="editKingdom(' + escapeHtml(JSON.stringify(k)) + ')" title="Edit this kingdom\'s name or slug">Edit</button> '
          + '<button class="pf-v6-c-button pf-m-secondary pf-m-small" onclick="createKingdomInvite(' + k.id + ')" '
          + 'title="Invite someone as kingdom coordinator for this kingdom">+ Kingdom Coordinator Invite</button></td></tr>'
        ).join('')
      : '<tr class="pf-v6-c-table__tr"><td class="pf-v6-c-table__td" colspan="3" style="color:var(--muted);padding:20px">No kingdoms yet.</td></tr>';
  } catch(e) { toast(e.message, true); }
}

// Same prompt-based editing convention platform.js uses elsewhere — Cancel
// on any one field leaves it unchanged rather than aborting the whole edit.
// Spec §38.7: also prompts for the two branding title overrides
// (public_site_title/admin_console_title) — leaving either blank clears it
// back to the built-in default, matching KingdomPatch's own "empty string
// or falsy -> None" semantics in routers/admin/tenants.py.
async function editKingdom(k) {
  const name = prompt('Kingdom name:', k.name);
  if (name === null) return;
  const slug = prompt('URL slug:', k.slug);
  if (slug === null) return;
  const publicTitle = prompt(
    'Public events page title (leave blank to use the default "Kingshot Event Schedule"):',
    k.public_site_title || ''
  );
  if (publicTitle === null) return;
  const adminTitle = prompt(
    'Admin console title (leave blank to use the default "Samaya"):',
    k.admin_console_title || ''
  );
  if (adminTitle === null) return;

  const payload = {};
  if (name !== k.name) payload.name = name;
  if (slug !== k.slug) payload.slug = slug;
  if (publicTitle !== (k.public_site_title || '')) payload.public_site_title = publicTitle;
  if (adminTitle !== (k.admin_console_title || '')) payload.admin_console_title = adminTitle;
  if (!Object.keys(payload).length) {
    toast('No changes made');
    return;
  }

  try {
    await api('PATCH', `/api/kingdoms/${k.id}`, payload, /*skipTenantHeader=*/true);
    toast('Kingdom updated');
    loadPlatformKingdoms();
  } catch(e) { toast(e.message, true); }
}

// The standing counterpart to the "+ Kingdom Coordinator Invite" button
// above — GET /api/kingdom-coordinators is scoped to one kingdom at a
// time (routers/admin/invites.py), so this fetches the kingdom list
// first and fans out one request per kingdom to build a flat table.
async function loadPlatformKingdomCoordinators() {
  try {
    const kingdoms = await api('GET', '/api/kingdoms', null, /*skipTenantHeader=*/true);
    const perKingdom = await Promise.all(kingdoms.map(async k => {
      const coords = await api('GET', `/api/kingdom-coordinators?kingdom_id=${k.id}`, null, /*skipTenantHeader=*/true);
      return coords.map(c => ({ ...c, kingdom_name: k.name }));
    }));
    const rows = perKingdom.flat();
    document.getElementById('kingdomCoordinatorsBody').innerHTML = rows.length
      ? rows.map(c =>
          '<tr class="pf-v6-c-table__tr">'
          + '<td class="pf-v6-c-table__td">' + escapeHtml(c.discord_username) + '</td>'
          + '<td class="pf-v6-c-table__td">' + escapeHtml(c.kingdom_name) + '</td>'
          + '<td class="pf-v6-c-table__td">'
          + '<button class="pf-v6-c-button pf-m-danger pf-m-small" onclick="removeKingdomCoordinator(' + c.id + ',\'' + escapeHtml(c.discord_username).replace(/'/g, "\\'") + '\',\'' + escapeHtml(c.kingdom_name).replace(/'/g, "\\'") + '\')">Remove</button>'
          + '</td>'
          + '</tr>'
        ).join('')
      : '<tr class="pf-v6-c-table__tr"><td class="pf-v6-c-table__td" colspan="3" style="color:var(--muted);padding:20px">No kingdom coordinators yet.</td></tr>';
  } catch(e) { toast(e.message, true); }
}

async function removeKingdomCoordinator(grantId, username, kingdomName) {
  if (!confirm('Remove ' + username + '\'s kingdom-coordinator access to ' + kingdomName + '? '
    + 'This does not affect their membership on any individual alliance.')) return;
  try {
    await api('DELETE', `/api/kingdom-coordinators/${grantId}`, null, /*skipTenantHeader=*/true);
    toast('Kingdom coordinator access removed');
    loadPlatformKingdomCoordinators();
  } catch(e) { toast(e.message, true); }
}

async function loadPlatformUsers() {
  try {
    const users = await api('GET', '/api/users', null, /*skipTenantHeader=*/true);
    document.getElementById('usersBody').innerHTML = users.length
      ? users.map(buildUserRow).join('')
      : '<tr class="pf-v6-c-table__tr"><td class="pf-v6-c-table__td" colspan="4" style="color:var(--muted);padding:20px">No users yet.</td></tr>';
  } catch(e) { toast(e.message, true); }
}

function buildUserRow(u) {
  const isSelf = ME && ME.id === u.id;
  const lastLogin = u.last_login_at ? new Date(u.last_login_at).toUTCString().slice(0, 16) : '—';
  const toggleLabel = u.is_superadmin ? 'Revoke superadmin' : 'Make superadmin';
  const toggleBtn = isSelf && u.is_superadmin
    ? '<span style="color:var(--muted);font-size:var(--fs-sm)" title="You can\'t remove your own superadmin access">(you)</span>'
    : '<button class="pf-v6-c-button pf-m-danger pf-m-small" onclick="toggleSuperadmin(' + u.id + ',' + (u.is_superadmin ? 'true' : 'false') + ',\'' + escapeHtml(u.discord_username).replace(/'/g, "\\'") + '\')">' + toggleLabel + '</button>';
  return '<tr class="pf-v6-c-table__tr">'
    + '<td class="pf-v6-c-table__td">' + escapeHtml(u.discord_username) + '</td>'
    + '<td class="pf-v6-c-table__td">' + (u.is_superadmin ? pfLabel('Superadmin', 'pf-m-red') : '') + '</td>'
    + '<td class="pf-v6-c-table__td" style="color:var(--muted);font-size:var(--fs-sm)">' + lastLogin + '</td>'
    + '<td class="pf-v6-c-table__td">' + toggleBtn + '</td>'
    + '</tr>';
}

// Superadmin grants full access to every tenant and kingdom platform-wide
// — confirm explicitly before flipping it either direction. Revoking your
// own flag is blocked server-side (see routers/admin/users.py) since
// there'd be no UI path back in.
async function toggleSuperadmin(userId, currentlySuperadmin, username) {
  const verb = currentlySuperadmin ? 'Revoke' : 'Grant';
  if (!confirm(verb + ' superadmin access ' + (currentlySuperadmin ? 'from' : 'to') + ' ' + username + '? '
    + 'Superadmin can see and edit every tenant and kingdom in this deployment.')) return;
  try {
    await api('PATCH', `/api/users/${userId}`, {is_superadmin: !currentlySuperadmin}, /*skipTenantHeader=*/true);
    toast('Superadmin ' + (currentlySuperadmin ? 'revoked' : 'granted'));
    loadPlatformUsers();
  } catch(e) { toast(e.message, true); }
}

async function loadPlatformTenants() {
  try {
    const tenants = await api('GET', '/api/tenants', null, /*skipTenantHeader=*/true);
    document.getElementById('tenantsBody').innerHTML = tenants.length
      ? tenants.map(t =>
          '<tr class="pf-v6-c-table__tr">'
          + '<td class="pf-v6-c-table__td">' + (t.icon_image_data
              ? '<img src="' + escapeHtml(t.icon_image_data) + '" style="width:20px;height:20px;border-radius:4px;object-fit:cover;vertical-align:middle;margin-right:6px">'
              : '<span class="cat-dot" style="background:' + escapeHtml(t.color) + '"></span>') + escapeHtml(t.name) + '</td>'
          + '<td class="pf-v6-c-table__td">' + escapeHtml(t.slug) + '</td>'
          + '<td class="pf-v6-c-table__td">' + t.kingdom_id + '</td>'
          + '<td class="pf-v6-c-table__td">' + escapeHtml(t.server_name) + '<div style="font-size:var(--fs-sm);color:var(--muted)">' + escapeHtml(t.guild_id) + '</div></td>'
          + '<td class="pf-v6-c-table__td">'
          + '<button class="pf-v6-c-button pf-m-secondary pf-m-small" onclick="editTenant(' + escapeHtml(JSON.stringify(t)) + ')" '
          + 'title="Edit this alliance\'s name, slug, or which Discord server it belongs to.">Edit</button>'
          + '</td>'
          + '</tr>'
        ).join('')
      : '<tr class="pf-v6-c-table__tr"><td class="pf-v6-c-table__td" colspan="5" style="color:var(--muted);padding:20px">No tenants yet.</td></tr>';
  } catch(e) { toast(e.message, true); }
}

async function createKingdom() {
  const name = prompt('Kingdom name (e.g. "Kingdom 138"):');
  if (!name) return;
  const slug = prompt('URL slug (lowercase, no spaces — e.g. "k138"):', name.toLowerCase().replace(/[^a-z0-9]+/g, ''));
  if (!slug) return;
  try {
    await api('POST', '/api/kingdoms', {name, slug}, /*skipTenantHeader=*/true);
    toast('Kingdom created');
    loadPlatformKingdoms();
  } catch(e) { toast(e.message, true); }
}

// Spec §38: Tenant create/edit moved from a prompt() chain to a real modal
// (#tenantModal) — a file picker for the alliance's 1:1 icon can't be done
// through prompt(). Bot credentials still aren't edited here at all (spec
// §25) — that's the Discord Server's own concern (editDiscordServer above);
// this modal only picks which server the alliance belongs to.
const TENANT_ICON_MAX_BYTES = 8 * 1024 * 1024;

async function createTenant() {
  const kingdoms = await api('GET', '/api/kingdoms', null, /*skipTenantHeader=*/true);
  if (!kingdoms.length) {
    toast('Create a Kingdom first', true);
    return;
  }
  if (!DISCORD_SERVERS.length) {
    toast('Create a Discord Server first (above)', true);
    return;
  }
  openTenantModal(null, kingdoms);
}

async function editTenant(t) {
  const kingdoms = await api('GET', '/api/kingdoms', null, /*skipTenantHeader=*/true);
  openTenantModal(t, kingdoms);
}

function openTenantModal(t, kingdoms) {
  document.getElementById('tenantModalTitle').querySelector('.pf-v6-c-modal-box__title-text').textContent = t ? 'Edit Tenant' : 'New Tenant';
  document.getElementById('tnId').value = t ? t.id : '';
  document.getElementById('tnName').value = t ? t.name : '';
  document.getElementById('tnSlug').value = t ? t.slug : '';
  document.getElementById('tnKingdom').innerHTML = kingdoms.map(k =>
    `<option value="${k.id}" ${t && t.kingdom_id === k.id ? 'selected' : ''}>${escapeHtml(k.name)}</option>`).join('');
  document.getElementById('tnServer').innerHTML = DISCORD_SERVERS.map(s =>
    `<option value="${s.id}" ${t && t.server_id === s.id ? 'selected' : ''}>${escapeHtml(s.name)} (${escapeHtml(s.guild_id)})</option>`).join('');
  setTenantIconPreview(t ? (t.icon_image_data || '') : '');
  document.getElementById('tnIconFile').value = '';
  document.getElementById('tenantModal').classList.add('open');
  focusModal(document.getElementById('tenantModal'));
}

function closeTenantModal() {
  document.getElementById('tenantModal').classList.remove('open');
  unfocusModal();
}

function setTenantIconPreview(dataUri) {
  document.getElementById('tnIconData').value = dataUri || '';
  const img = document.getElementById('tnIconPreview');
  const removeBtn = document.getElementById('tnIconRemove');
  if (dataUri) {
    img.src = dataUri;
    img.classList.remove('hidden');
    removeBtn.classList.remove('hidden');
  } else {
    img.classList.add('hidden');
    img.src = '';
    removeBtn.classList.add('hidden');
  }
}

function handleTenantIconFile(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  if (file.size > TENANT_ICON_MAX_BYTES) {
    toast('Icon image is too large (max 8MB) — pick a smaller file', true);
    input.value = '';
    return;
  }
  const reader = new FileReader();
  reader.onload = () => setTenantIconPreview(reader.result);
  reader.onerror = () => toast('Could not read that image file', true);
  reader.readAsDataURL(file);
}

function removeTenantIcon() {
  setTenantIconPreview('');
  document.getElementById('tnIconFile').value = '';
}

async function saveTenantModal() {
  const id = document.getElementById('tnId').value;
  const name = document.getElementById('tnName').value.trim();
  const slug = document.getElementById('tnSlug').value.trim();
  const kingdomId = parseInt(document.getElementById('tnKingdom').value, 10);
  const serverId = parseInt(document.getElementById('tnServer').value, 10);
  const iconData = document.getElementById('tnIconData').value;
  if (!name || !slug) { toast('Name and slug are required', true); return; }

  try {
    if (id) {
      // icon_image_data: '' explicitly clears it (same PATCH null-vs-empty
      // convention as EventDefinition.cover_image_data) — omitting the key
      // entirely isn't an option here since the modal always has a value
      // (possibly '') for it once opened.
      await api('PATCH', `/api/tenants/${id}`, {
        name, slug, server_id: serverId, icon_image_data: iconData || '',
      }, /*skipTenantHeader=*/true);
      toast('Alliance updated');
    } else {
      await api('POST', '/api/tenants', {
        kingdom_id: kingdomId, name, slug, server_id: serverId, icon_image_data: iconData || null,
      }, /*skipTenantHeader=*/true);
      toast('Tenant created — ' + name + ' can now be invited from the Access tab');
    }
    closeTenantModal();
    loadPlatformTenants();
    loadTenants();  // refresh TENANTS/TENANT_ICONS everywhere
  } catch(e) { toast(e.message, true); }
}

async function createKingdomInvite(kingdomId) {
  try {
    const inv = await api('POST', '/api/kingdom-invites', {kingdom_id: kingdomId}, /*skipTenantHeader=*/true);
    copyInviteLink(window.location.origin + '/invite/' + inv.token);
  } catch(e) { toast(e.message, true); }
}

// Wire this view's create buttons and the Tenant modal's icon controls —
// replaces their onclick/onchange attributes (Phase 3 audit remediation).
document.getElementById('btnCreateKingdom')?.addEventListener('click', () => createKingdom());
document.getElementById('btnCreateDiscordServer')?.addEventListener('click', () => createDiscordServer());
document.getElementById('btnCreateTenant')?.addEventListener('click', () => createTenant());
document.getElementById('tnIconFile')?.addEventListener('change', function () { handleTenantIconFile(this); });
document.getElementById('tnIconRemove')?.addEventListener('click', () => removeTenantIcon());
document.getElementById('btnSaveTenantModal')?.addEventListener('click', () => saveTenantModal());
