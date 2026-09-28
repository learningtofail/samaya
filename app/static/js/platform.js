// Platform view (#v-platform, superadmin-only — see applyRoleVisibility
// in common.js): Kingdom and Tenant creation, the "onboard a new
// alliance" surface. Depends on common.js.

async function loadPlatform() {
  await Promise.all([loadPlatformKingdoms(), loadPlatformTenants()]);
}

async function loadPlatformKingdoms() {
  try {
    const kingdoms = await api('GET', '/api/kingdoms', null, /*skipTenantHeader=*/true);
    document.getElementById('kingdomsBody').innerHTML = kingdoms.length
      ? kingdoms.map(k =>
          '<tr class="pf-v6-c-table__tr"><td class="pf-v6-c-table__td">' + escapeHtml(k.name) + '</td><td class="pf-v6-c-table__td">' + escapeHtml(k.slug) + '</td>'
          + '<td class="pf-v6-c-table__td"><button class="pf-v6-c-button pf-m-secondary pf-m-small" onclick="createKingdomInvite(' + k.id + ')" '
          + 'title="Invite someone as kingdom coordinator for this kingdom">+ Kingdom Coordinator Invite</button></td></tr>'
        ).join('')
      : '<tr class="pf-v6-c-table__tr"><td class="pf-v6-c-table__td" colspan="3" style="color:var(--muted);padding:20px">No kingdoms yet.</td></tr>';
  } catch(e) { toast(e.message, true); }
}

async function loadPlatformTenants() {
  try {
    const tenants = await api('GET', '/api/tenants', null, /*skipTenantHeader=*/true);
    document.getElementById('tenantsBody').innerHTML = tenants.length
      ? tenants.map(t =>
          '<tr class="pf-v6-c-table__tr">'
          + '<td class="pf-v6-c-table__td"><span class="cat-dot" style="background:' + escapeHtml(t.color) + '"></span>' + escapeHtml(t.name) + '</td>'
          + '<td class="pf-v6-c-table__td">' + escapeHtml(t.slug) + '</td>'
          + '<td class="pf-v6-c-table__td">' + t.kingdom_id + '</td>'
          + '<td class="pf-v6-c-table__td" style="font-size:var(--fs-sm);color:var(--muted)">' + escapeHtml(t.guild_id) + '</td>'
          + '<td class="pf-v6-c-table__td">' + (t.has_own_bot_token ? 'Own bot' : 'Platform bot') + '</td>'
          + '<td class="pf-v6-c-table__td">'
          + '<button class="pf-v6-c-button pf-m-secondary pf-m-small" onclick="editTenant(' + escapeHtml(JSON.stringify(t)) + ')" '
          + 'title="Edit this alliance\'s name, slug, guild, or bot credentials.">Edit</button>'
          + '</td>'
          + '</tr>'
        ).join('')
      : '<tr class="pf-v6-c-table__tr"><td class="pf-v6-c-table__td" colspan="6" style="color:var(--muted);padding:20px">No tenants yet.</td></tr>';
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

async function createTenant() {
  const kingdoms = await api('GET', '/api/kingdoms', null, /*skipTenantHeader=*/true);
  if (!kingdoms.length) {
    toast('Create a Kingdom first', true);
    return;
  }
  const kingdomList = kingdoms.map(k => k.id + '=' + k.name).join(', ');
  const kingdomIdStr = prompt('Kingdom ID for this alliance (' + kingdomList + '):', String(kingdoms[0].id));
  if (!kingdomIdStr) return;
  const name = prompt('Alliance name (e.g. "MOD"):');
  if (!name) return;
  const slug = prompt('URL slug (lowercase — e.g. "mod"):', name.toLowerCase().replace(/[^a-z0-9]+/g, ''));
  if (!slug) return;
  const guildId = prompt('Discord guild (server) ID for this alliance:');
  if (!guildId) return;
  const botToken = prompt('Bot token for this alliance (leave blank to use the shared platform bot):', '');

  try {
    await api('POST', '/api/tenants', {
      kingdom_id: parseInt(kingdomIdStr, 10), name, slug, guild_id: guildId,
      bot_token: botToken || null,
    }, /*skipTenantHeader=*/true);
    toast('Tenant created — ' + name + ' can now be invited (see the Access tab once you switch into it)');
    loadPlatformTenants();
    loadTenants();  // refresh the picker too
  } catch(e) { toast(e.message, true); }
}

// Edits an existing Tenant via PATCH /api/tenants/{id} (the backend has
// supported this since Phase 4 — this just wires up the UI). Each field
// prompts pre-filled with its current value; pressing Cancel on any one
// of them leaves that field unchanged rather than aborting the whole
// edit, since re-typing every other field just to fix one is tedious.
// bot_token can't be pre-filled (write-only, never returned by the API,
// same reasoning as createTenant never showing an existing token) — its
// prompt explains the two ways to leave it alone vs. actually change it.
async function editTenant(t) {
  const name = prompt('Alliance name:', t.name);
  const slug = (name !== null) ? prompt('URL slug:', t.slug) : null;
  const guildId = (slug !== null) ? prompt('Discord guild (server) ID:', t.guild_id) : null;
  const tokenNote = t.has_own_bot_token
    ? 'This alliance has its own bot token set. Leave blank to keep it unchanged, or type CLEAR to remove it and fall back to the shared platform bot.'
    : 'This alliance currently uses the shared platform bot. Leave blank to keep it that way, or paste a bot token to give it its own.';
  const botTokenInput = (guildId !== null) ? prompt(tokenNote, '') : null;

  const payload = {};
  if (name !== null && name !== t.name)         payload.name = name;
  if (slug !== null && slug !== t.slug)         payload.slug = slug;
  if (guildId !== null && guildId !== t.guild_id) payload.guild_id = guildId;
  if (botTokenInput !== null && botTokenInput !== '') {
    payload.bot_token = (botTokenInput.trim().toUpperCase() === 'CLEAR') ? '' : botTokenInput.trim();
  }

  if (!Object.keys(payload).length) {
    toast('No changes made');
    return;
  }

  try {
    await api('PATCH', `/api/tenants/${t.id}`, payload, /*skipTenantHeader=*/true);
    toast('Alliance updated');
    loadPlatformTenants();
    loadTenants();  // refresh the picker too, in case name/slug changed
  } catch(e) { toast(e.message, true); }
}

async function createKingdomInvite(kingdomId) {
  try {
    const inv = await api('POST', '/api/kingdom-invites', {kingdom_id: kingdomId}, /*skipTenantHeader=*/true);
    copyInviteLink(window.location.origin + '/invite/' + inv.token);
  } catch(e) { toast(e.message, true); }
}
