// Audit log tab (#v-audit, superadmin only): a read-only feed over
// /api/audit-log, one alliance at a time. Depends on common.js.

async function loadAuditLog() {
  renderAllianceFilterSelect('auditAlliance', 'audit', loadAuditLog, false);
  const slug = singleSlugFor('audit');
  try {
    const entries = await api('GET', '/api/audit-log?limit=100', null, false, slug);
    byId('auditLogBody').innerHTML = entries.length ? entries.map(buildAuditRow).join('') : emptyRow(5, 'No audit entries yet.');
  } catch (e) {
    toast(e.message, true);
    byId('auditLogBody').innerHTML = emptyRow(5, 'Could not load the audit log: ' + e.message);
  }
}

function auditActionLabel(action) {
  if (action === 'create') return pfLabel('Create', 'pf-m-green');
  if (action === 'delete') return pfLabel('Delete', 'pf-m-red');
  return pfLabel('Update', 'pf-m-blue');
}

// Compact on purpose: which fields changed, not a full diff.
function auditDetails(entry) {
  const before = entry.before || {};
  const after = entry.after || {};
  const keys = new Set([...Object.keys(before), ...Object.keys(after)]);
  const parts = [];
  keys.forEach((k) => {
    const b = before[k];
    const a = after[k];
    if (JSON.stringify(b) === JSON.stringify(a)) return;
    if (entry.action === 'create') parts.push(k + ': ' + JSON.stringify(a));
    else if (entry.action === 'delete') parts.push(k + ': ' + JSON.stringify(b));
    else parts.push(k + ': ' + JSON.stringify(b) + ' to ' + JSON.stringify(a));
  });
  const text = parts.join(', ');
  return escapeHtml(text.length > 400 ? text.slice(0, 400) + '...' : text);
}

function buildAuditRow(entry) {
  const when = entry.timestamp ? new Date(entry.timestamp).toUTCString().slice(0, 22) : '';
  return `<tr class="pf-v6-c-table__tr">
    <td class="pf-v6-c-table__td samaya-muted" data-label="When (UTC)">${escapeHtml(when)}</td>
    <td class="pf-v6-c-table__td" data-label="Who">${escapeHtml(entry.user || 'system')}</td>
    <td class="pf-v6-c-table__td" data-label="Action">${auditActionLabel(entry.action)}</td>
    <td class="pf-v6-c-table__td" data-label="What">${escapeHtml(entry.table_name)}${entry.row_id ? ' #' + entry.row_id : ''}</td>
    <td class="pf-v6-c-table__td" data-label="Details">${auditDetails(entry)}</td>
  </tr>`;
}

VIEW_LOADERS.audit = loadAuditLog;
