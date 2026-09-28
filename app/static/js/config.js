// Config view (#v-config): lists the current tenant's own Discord
// channels/roles, for reference when filling in an event's notification
// settings elsewhere. Read-only — setting a tenant's own bot token/guild
// is a superadmin action done via the Platform tab (platform.js), not
// here; this view used to have that form, calling an endpoint
// (PUT /api/config/discord) that no longer exists after the Phase 1
// multi-tenant migration moved that responsibility to tenants.py. Fixed
// as part of removing the theme switcher this file also used to render.
// Depends on common.js.

async function loadDiscordConfig() {
  await Promise.all([loadDiscordChannels(), loadDiscordRoles()]);
}

// The guild's own Discord display name — distinct from the tenant/alliance
// name shown everywhere else in the UI, and worth surfacing here since
// more than one tenant can point at the same guild_id (e.g. MOD/NSR).
// Fetched once per load and reused for every channel row, rather than a
// per-row call, since it's the same value for all of them (Config is
// single-tenant — see common.js's SINGLE_TENANT_ONLY_VIEWS).
async function loadDiscordChannels() {
  const tbody = document.getElementById('discordChannelsBody');
  try {
    const [channels, guild] = await Promise.all([
      api('GET', '/api/discord/channels'),
      api('GET', '/api/discord/guild').catch(() => null),
    ]);
    const guildName = guild ? escapeHtml(guild.name) : '<span style="color:var(--muted)">—</span>';
    tbody.innerHTML = channels.length
      ? channels.map(c => `<tr><td>${escapeHtml(c.name)}</td><td>${guildName}</td><td style="color:var(--muted);font-size:var(--fs-sm)">${escapeHtml(c.id)}</td></tr>`).join('')
      : '<tr><td colspan="3" style="color:var(--muted);padding:20px">No channels found.</td></tr>';
  } catch(e) {
    tbody.innerHTML = `<tr><td colspan="3" style="color:var(--muted);padding:20px">${escapeHtml(e.message)}</td></tr>`;
  }
}

async function loadDiscordRoles() {
  const tbody = document.getElementById('discordRolesBody');
  try {
    const roles = await api('GET', '/api/discord/roles');
    tbody.innerHTML = roles.length
      ? roles.map(r => `<tr><td>${escapeHtml(r.name)}</td><td style="color:var(--muted);font-size:var(--fs-sm)">${escapeHtml(r.id)}</td></tr>`).join('')
      : '<tr><td colspan="2" style="color:var(--muted);padding:20px">No roles found.</td></tr>';
  } catch(e) {
    tbody.innerHTML = `<tr><td colspan="2" style="color:var(--muted);padding:20px">${escapeHtml(e.message)}</td></tr>`;
  }
}
