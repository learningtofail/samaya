// Audit Log view (#v-audit, owner-only — see applyRoleVisibility in
// common.js): a read-only feed over /api/audit-log (spec §31). Depends on
// common.js.

// require_tenant_owner-gated per-alliance (audit_log.py) — no combined
// form, same reasoning as Access's invites/members. Reuses whichever
// alliance Access's own selector is set to, since both live in the same
// superadmin-only console area (spec §38.6) and audit/access naturally
// refer to the same alliance at a time.
function auditTenantSlug() {
  return typeof accessTenantSlug === 'function' ? accessTenantSlug() : (TENANTS[0] ? TENANTS[0].slug : '');
}

async function loadAuditLog() {
  const label = document.getElementById('auditAllianceLabel');
  if (label) {
    const t = TENANTS.find(t => t.slug === auditTenantSlug());
    label.textContent = t ? `Alliance: ${t.name}` : '';
  }
  try {
    const entries = await api('GET', '/api/audit-log?limit=100', null, false, auditTenantSlug());
    const tbody = document.getElementById('auditLogBody');
    tbody.innerHTML = entries.length
      ? entries.map(buildAuditRow).join('')
      : '<tr class="pf-v6-c-table__tr"><td class="pf-v6-c-table__td" colspan="5" style="color:var(--muted);padding:20px">No audit entries yet.</td></tr>';
  } catch(e) { toast(e.message, true); }
}

function _auditActionLabel(action) {
  if (action === 'create') return pfLabel('Create', 'pf-m-green');
  if (action === 'delete') return pfLabel('Delete', 'pf-m-red');
  return pfLabel('Update', 'pf-m-blue');
}

function _auditDetails(entry) {
  // Keep this compact — a full JSON diff belongs in a real diff viewer,
  // not a table cell. This just surfaces which fields changed, so an
  // owner can spot-check without needing the raw payload.
  const before = entry.before || {};
  const after = entry.after || {};
  const keys = new Set([...Object.keys(before), ...Object.keys(after)]);
  if (!keys.size) return '';
  const parts = [];
  keys.forEach(k => {
    const b = before[k], a = after[k];
    if (JSON.stringify(b) === JSON.stringify(a)) return;
    if (entry.action === 'create') parts.push(k + ': ' + JSON.stringify(a));
    else if (entry.action === 'delete') parts.push(k + ': ' + JSON.stringify(b));
    else parts.push(k + ': ' + JSON.stringify(b) + ' → ' + JSON.stringify(a));
  });
  return escapeHtml(parts.join(', '));
}

function buildAuditRow(entry) {
  const when = entry.timestamp ? new Date(entry.timestamp).toUTCString().slice(0, 22) : '';
  return '<tr class="pf-v6-c-table__tr">'
    + '<td class="pf-v6-c-table__td" style="color:var(--muted);font-size:var(--fs-sm)">' + when + '</td>'
    + '<td class="pf-v6-c-table__td">' + escapeHtml(entry.user || 'system') + '</td>'
    + '<td class="pf-v6-c-table__td">' + _auditActionLabel(entry.action) + '</td>'
    + '<td class="pf-v6-c-table__td">' + escapeHtml(entry.table_name) + (entry.row_id ? ' #' + entry.row_id : '') + '</td>'
    + '<td class="pf-v6-c-table__td" style="font-size:var(--fs-sm)">' + _auditDetails(entry) + '</td>'
    + '</tr>';
}
