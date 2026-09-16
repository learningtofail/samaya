// Access view (#v-access, owner-only — see applyRoleVisibility in
// common.js): invite creation, the pending-invites list, and revocation
// for the current tenant. Depends on common.js.

async function loadAccess() {
  try {
    const invites = await api('GET', '/api/invites');
    const tbody = document.getElementById('invitesBody');
    if (!invites.length) {
      tbody.innerHTML = '<tr class="pf-v6-c-table__tr"><td class="pf-v6-c-table__td" colspan="4" style="color:var(--muted);padding:20px">No invites yet.</td></tr>';
      return;
    }
    tbody.innerHTML = invites.map(buildInviteRow).join('');
  } catch(e) { toast(e.message, true); }
}

function buildInviteRow(inv) {
  let status, actionCell;
  if (inv.revoked_at) {
    status = pfLabel('Revoked', 'pf-m-grey');
    actionCell = '';
  } else if (inv.used_at) {
    status = pfLabel('Claimed', 'pf-m-green');
    actionCell = '';
  } else {
    const link = window.location.origin + '/invite/' + inv.token;
    status = '<button class="pf-v6-c-button pf-m-secondary pf-m-small" onclick="copyInviteLink(\'' + link + '\')">📋 Copy link</button>';
    actionCell = '<button class="pf-v6-c-button pf-m-danger pf-m-small" onclick="revokeInvite(' + inv.id + ')">Revoke</button>';
  }
  const expires = new Date(inv.expires_at).toUTCString().slice(0, 16);
  return '<tr class="pf-v6-c-table__tr">'
    + '<td class="pf-v6-c-table__td">' + escapeHtml(inv.role) + '</td>'
    + '<td class="pf-v6-c-table__td">' + status + '</td>'
    + '<td class="pf-v6-c-table__td" style="color:var(--muted);font-size:var(--fs-sm)">' + expires + '</td>'
    + '<td class="pf-v6-c-table__td">' + actionCell + '</td>'
    + '</tr>';
}

async function createInvite() {
  const role = prompt('Role for this invite — "owner" or "coordinator"?', 'coordinator');
  if (!role) return;
  if (role !== 'owner' && role !== 'coordinator') {
    toast('Role must be "owner" or "coordinator"', true);
    return;
  }
  try {
    const inv = await api('POST', '/api/invites', {role});
    await loadAccess();
    copyInviteLink(window.location.origin + '/invite/' + inv.token);
  } catch(e) { toast(e.message, true); }
}

async function revokeInvite(id) {
  if (!confirm('Revoke this invite? Anyone who has the link will no longer be able to use it.')) return;
  try {
    await api('DELETE', '/api/invites/' + id);
    toast('Invite revoked');
    loadAccess();
  } catch(e) { toast(e.message, true); }
}

function copyInviteLink(link) {
  if (navigator.clipboard) {
    navigator.clipboard.writeText(link).then(
      () => toast('Invite link copied — share it via Discord DM or an officer channel'),
      () => toast(link)  // clipboard write blocked (e.g. non-HTTPS context) — show the link itself instead
    );
  } else {
    toast(link);
  }
}
