// Setup tab (#v-setup, spec §66.7): your public display name, each alliance's
// Notifications destination (the channel and role reminders and messages go
// to unless an event overrides them), and, for a superadmin, the platform
// sections that access.js and platform.js render. Depends on common.js and
// pickers.js.

const SETUP_PICKERS = {};   // tenant id -> { channel, role }

function loadSetup() {
  const sa = isSuperadmin();
  byId('setupDisplayName').value = ME && ME.display_name ? ME.display_name : '';
  byId('setupSuperadmin').classList.toggle('hidden', !sa);
  renderSetupAlliances();
  if (sa) {
    loadAccess();
    loadPlatform();
  }
}

function renderSetupAlliances() {
  const host = byId('setupAlliances');
  if (!TENANTS.length) { host.innerHTML = '<p class="samaya-empty">You have no alliances yet.</p>'; return; }
  host.innerHTML = TENANTS.map((t) => {
    const can = isSuperadmin() || isOwnerOfTenant(t);
    return `<section class="alliance" aria-labelledby="alliance${t.id}Name">
      <h4 class="alliance__name" id="alliance${t.id}Name">${escapeHtml(t.name)} <span class="samaya-muted">${escapeHtml(t.server_name)}</span></h4>
      ${can
    ? `<div class="alliance__pickers">
            <div id="ntfChannelHost${t.id}"></div>
            <div id="ntfRoleHost${t.id}"></div>
          </div>
          <button type="button" class="pf-v6-c-button pf-m-primary pf-m-small" data-action="save" data-id="${t.id}">Save notifications for ${escapeHtml(t.name)}</button>`
    : `<p class="samaya-muted">Channel ${t.notification_channel_id ? escapeHtml(t.notification_channel_id) : 'not set'}, role ${t.notification_role_id ? escapeHtml(t.notification_role_id) : 'not set'}. Only an owner of ${escapeHtml(t.name)} or a superadmin can change this.</p>`}
    </section>`;
  }).join('');
  TENANTS.forEach((t) => {
    if (!(isSuperadmin() || isOwnerOfTenant(t))) return;
    SETUP_PICKERS[t.id] = {
      channel: mountDiscordPicker(byId('ntfChannelHost' + t.id), {
        kind: 'channel', slug: t.slug, id: 'ntfChannel' + t.id, label: 'Notifications channel',
        value: t.notification_channel_id, emptyLabel: 'No channel set',
        helper: 'Reminders and messages post here unless an event overrides the channel.',
      }),
      role: mountDiscordPicker(byId('ntfRoleHost' + t.id), {
        kind: 'role', slug: t.slug, id: 'ntfRole' + t.id, label: 'Notifications role',
        value: t.notification_role_id, emptyLabel: 'No role',
        helper: 'Mentioned on events that have "mention the role" turned on.',
      }),
    };
  });
}

bindActions(byId('setupAlliances'), {
  async save(btn) {
    const t = tenantById(parseInt(btn.dataset.id, 10));
    const pick = t && SETUP_PICKERS[t.id];
    if (!pick) return;
    const channel = pick.channel.value();
    const role = pick.role.value();
    if ((channel && !/^\d+$/.test(channel)) || (role && !/^\d+$/.test(role))) {
      toast('Channel and role IDs are digits only.', true);
      return;
    }
    btn.disabled = true;
    try {
      const updated = await api('PUT', '/api/notification-destination', {
        notification_channel_id: channel, notification_role_id: role,
      }, false, t.slug);
      Object.assign(t, updated);
      toast(`Notifications for ${t.name} saved.`);
    } catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
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
