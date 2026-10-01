// Setup tab, "Access" section (superadmin only): invite links and the roster
// of people who have claimed access, per alliance. Depends on common.js.
//
// Invites and members are inherently per-alliance (UserTenant grants), so this
// section has its own alliance select that is always one real alliance.

function accessTenantSlug() {
  const saved = getTabFilter('access');
  if (saved !== COMBINED_SLUG && TENANTS.some((t) => t.slug === saved)) return saved;
  return TENANTS[0] ? TENANTS[0].slug : '';
}

const ACCESS_ROLES = ['owner', 'coordinator', 'viewer'];

async function loadAccess() {
  renderAllianceFilterSelect('accessAllianceSelect', 'access', loadAccess, false);
  await Promise.all([loadAccessInvites(), loadAccessMembers()]);
}

async function loadAccessInvites() {
  try {
    const invites = await api('GET', '/api/invites', null, false, accessTenantSlug());
    byId('invitesBody').innerHTML = invites.length ? invites.map(buildInviteRow).join('') : emptyRow(4, 'No invites yet.');
  } catch (e) { toast(e.message, true); }
}

async function loadAccessMembers() {
  try {
    const members = await api('GET', '/api/members', null, false, accessTenantSlug());
    byId('membersBody').innerHTML = members.length ? members.map(buildMemberRow).join('') : emptyRow(3, 'No one has claimed access yet.');
  } catch (e) { toast(e.message, true); }
}

function buildMemberRow(m) {
  return `<tr class="pf-v6-c-table__tr">
    <td class="pf-v6-c-table__td" data-label="Person">${escapeHtml(m.discord_username)}</td>
    <td class="pf-v6-c-table__td" data-label="Role">${escapeHtml(m.role)}</td>
    <td class="pf-v6-c-table__td" data-label="Actions"><div class="row-actions">
      <button type="button" class="pf-v6-c-button pf-m-secondary pf-m-small" data-action="role" data-id="${m.id}" data-role="${escapeHtml(m.role)}" title="Change role: owner, coordinator, or read-only viewer">Change role</button>
      <button type="button" class="pf-v6-c-button pf-m-danger pf-m-small" data-action="remove" data-id="${m.id}" data-name="${escapeHtml(m.discord_username)}">Remove</button>
    </div></td>
  </tr>`;
}

function buildInviteRow(inv) {
  let status;
  let action = '';
  if (inv.revoked_at) {
    status = pfLabel('Revoked', 'pf-m-gray');
  } else if (inv.used_at) {
    status = pfLabel('Claimed', 'pf-m-green');
  } else {
    status = `<button type="button" class="pf-v6-c-button pf-m-secondary pf-m-small" data-action="copy" data-token="${escapeHtml(inv.token)}">Copy link</button>`;
    action = `<button type="button" class="pf-v6-c-button pf-m-danger pf-m-small" data-action="revoke" data-id="${inv.id}">Revoke</button>`;
  }
  const expires = new Date(inv.expires_at).toUTCString().slice(0, 16);
  return `<tr class="pf-v6-c-table__tr">
    <td class="pf-v6-c-table__td" data-label="Role">${escapeHtml(inv.role)}</td>
    <td class="pf-v6-c-table__td" data-label="Status">${status}</td>
    <td class="pf-v6-c-table__td samaya-muted" data-label="Expires">${escapeHtml(expires)}</td>
    <td class="pf-v6-c-table__td" data-label="Actions">${action}</td>
  </tr>`;
}

function copyInviteLink(link) {
  if (navigator.clipboard) {
    navigator.clipboard.writeText(link).then(
      () => toast('Invite link copied. Share it by Discord DM or in an officer channel.'),
      () => toast(link),
    );
  } else {
    toast(link);
  }
}

function inviteLinkFor(token) {
  return window.location.origin + '/invite/' + token;
}

function askRole(message, current) {
  const role = prompt(message + ' (owner, coordinator or viewer)', current);
  if (!role) return null;
  if (!ACCESS_ROLES.includes(role)) { toast('Role must be owner, coordinator or viewer', true); return null; }
  return role;
}

bindActions(byId('membersBody'), {
  async role(btn) {
    const next = askRole('New role for this person?', btn.dataset.role);
    if (!next || next === btn.dataset.role) return;
    if (!confirm(`Change this person's role from ${btn.dataset.role} to ${next}?`)) return;
    try {
      await api('PATCH', '/api/members/' + btn.dataset.id, { role: next }, false, accessTenantSlug());
      toast('Role updated');
      loadAccessMembers();
    } catch (e) { toast(e.message, true); }
  },
  async remove(btn) {
    if (!confirm(`Remove ${btn.dataset.name}'s access to this alliance? They will need a new invite to get back in.`)) return;
    try {
      await api('DELETE', '/api/members/' + btn.dataset.id, null, false, accessTenantSlug());
      toast('Access removed');
      loadAccessMembers();
    } catch (e) { toast(e.message, true); }
  },
});

bindActions(byId('invitesBody'), {
  copy(btn) { copyInviteLink(inviteLinkFor(btn.dataset.token)); },
  async revoke(btn) {
    if (!confirm('Revoke this invite? Anyone holding the link will no longer be able to use it.')) return;
    try {
      await api('DELETE', '/api/invites/' + btn.dataset.id, null, false, accessTenantSlug());
      toast('Invite revoked');
      loadAccessInvites();
    } catch (e) { toast(e.message, true); }
  },
});

byId('btnCreateInvite').addEventListener('click', async () => {
  const role = askRole('Role for this invite?', 'coordinator');
  if (!role) return;
  try {
    const inv = await api('POST', '/api/invites', { role }, false, accessTenantSlug());
    await loadAccessInvites();
    copyInviteLink(inviteLinkFor(inv.token));
  } catch (e) { toast(e.message, true); }
});
