// Config view (#v-config): read-only Discord metadata (channels/roles),
// for reference when filling in an event's notification settings
// elsewhere. Setting a server's own bot token/guild is a superadmin
// action done via the Platform tab (platform.js) against DiscordServer,
// not here.
//
// Spec §38.5: rebuilt as an accordion grouped by Discord server (not by
// alliance) — GET /api/discord/config-overview already does that
// grouping server-side, since more than one alliance can share one
// Discord server (Tenant.server_id has no unique constraint) and a
// per-alliance list would just show the same channel/role list twice
// under two names. Depends on common.js.

async function loadDiscordConfig() {
  renderAllianceFilterSelect('configFilter', 'config', loadDiscordConfig);
  const wrap = document.getElementById('discordConfigAccordion');
  if (!wrap) return;
  wrap.innerHTML = '<p style="color:var(--muted)">Loading…</p>';
  try {
    const data = await api('GET', '/api/discord/config-overview', null, false, getTabFilter('config'));
    renderDiscordConfigAccordion(data.servers);
  } catch(e) {
    wrap.innerHTML = `<p style="color:#991b1b">${escapeHtml(e.message)}</p>`;
  }
}

function renderDiscordConfigAccordion(servers) {
  const wrap = document.getElementById('discordConfigAccordion');
  if (!servers.length) {
    wrap.innerHTML = '<p style="color:var(--muted);padding:20px">No Discord servers configured yet.</p>';
    return;
  }

  wrap.innerHTML = servers.map((s, idx) => {
    const alliancesLabel = s.tenants
      .map(t => `<span style="color:${t.color || 'var(--muted)'};font-weight:500">${escapeHtml(t.name)}</span>`)
      .join(', ');

    if (s.error) {
      return `<details class="pf-v6-u-mb-md" ${idx === 0 ? 'open' : ''}>
        <summary style="cursor:pointer;font-weight:600;padding:10px 0">${escapeHtml(s.server_name)} <span style="font-weight:400;color:var(--muted);font-size:var(--fs-sm)">(${alliancesLabel})</span></summary>
        <div class="pf-v6-c-alert pf-m-warning pf-m-inline pf-v6-u-mb-md"><div class="pf-v6-c-alert__icon">⚠</div><p class="pf-v6-c-alert__title">${escapeHtml(s.error)}</p></div>
      </details>`;
    }

    const guildName = s.guild_name ? escapeHtml(s.guild_name) : '<span style="color:var(--muted)">—</span>';
    const channelRows = s.channels.length
      ? s.channels.map(c => `<tr><td>${escapeHtml(c.name)}</td><td style="color:var(--muted);font-size:var(--fs-sm)">${escapeHtml(c.id)}</td></tr>`).join('')
      : '<tr><td colspan="2" style="color:var(--muted);padding:12px">No channels found.</td></tr>';
    const roleRows = s.roles.length
      ? s.roles.map(r => `<tr><td>${escapeHtml(r.name)}</td><td style="color:var(--muted);font-size:var(--fs-sm)">${escapeHtml(r.id)}</td></tr>`).join('')
      : '<tr><td colspan="2" style="color:var(--muted);padding:12px">No roles found.</td></tr>';

    return `<details class="pf-v6-u-mb-md" ${idx === 0 ? 'open' : ''}>
      <summary style="cursor:pointer;font-weight:600;padding:10px 0">${escapeHtml(s.server_name)} — ${guildName} <span style="font-weight:400;color:var(--muted);font-size:var(--fs-sm)">(${alliancesLabel})</span></summary>
      <div class="pf-v6-l-gallery pf-m-gutter" style="--pf-v6-l-gallery--GridTemplateColumns--min: 260px">
        <div>
          <h3 class="pf-v6-c-title pf-m-sm pf-v6-u-mb-xs">Channels</h3>
          <table class="pf-v6-c-table pf-m-grid-md"><thead><tr><th>Name</th><th>ID</th></tr></thead><tbody>${channelRows}</tbody></table>
        </div>
        <div>
          <h3 class="pf-v6-c-title pf-m-sm pf-v6-u-mb-xs">Roles</h3>
          <table class="pf-v6-c-table pf-m-grid-md"><thead><tr><th>Name</th><th>ID</th></tr></thead><tbody>${roleRows}</tbody></table>
        </div>
      </div>
    </details>`;
  }).join('');
}
