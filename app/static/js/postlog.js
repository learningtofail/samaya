// Post Log view (#v-postlog): PostLog table and CSV export. Depends on
// common.js.

async function exportPostLogCsv() {
  try {
    const res = await fetch('/admin/api/post-log/export.csv', {
      headers: { 'X-Tenant-Slug': getTabFilter('postlog') },
      credentials: 'same-origin',
    });
    if (res.status === 401) { window.location.href = '/auth/login'; return; }
    if (!res.ok) throw new Error('Export failed: ' + res.statusText);
    const blob = await res.blob();
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href = url;
    a.download = 'PostLog_Export.csv';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    toast(e.message, true);
  }
}

// Cached so filterPostLogTable() (spec §29) can re-render from a text
// filter without a round trip.
let POSTLOG_CACHE = [];

function filterPostLogTable() {
  renderPostLogTable(POSTLOG_CACHE);
}

async function loadPostLog() {
  renderAllianceFilterSelect('postlogFilter', 'postlog', loadPostLog);
  try {
    const logs = await api('GET', '/api/post-log?limit=100', null, false, getTabFilter('postlog'));
    POSTLOG_CACHE = logs;
    renderPostLogTable(logs);
  } catch(e) { toast(e.message, true); }
}

function renderPostLogTable(allLogs) {
    const filterText = (document.getElementById('postLogFilterInput')?.value || '').trim().toLowerCase();
    const logs = filterText
      ? allLogs.filter(l =>
          l.event_name.toLowerCase().includes(filterText) ||
          (l.posted_by || '').toLowerCase().includes(filterText) ||
          l.status.toLowerCase().includes(filterText))
      : allLogs;
    const tbody = document.getElementById('logBody');
    if (!logs.length) {
      tbody.innerHTML = '<tr class="pf-v6-c-table__tr"><td class="pf-v6-c-table__td" colspan="7" style="color:var(--muted);padding:20px">No posts yet.</td></tr>';
      return;
    }
    tbody.innerHTML = logs.map(l => {
      const dimStyle = l.status === 'cancelled' ? 'opacity:.5;' : '';
      const leadStyle = l.leadership_only ? 'background:var(--bg3);' : '';
      // "All" filter mixes tenants in one list — a left border plus
      // inline tenant name gives the same at-a-glance attribution the
      // Events/Schedule tables already use.
      const showTenantTag = getTabFilter('postlog') === COMBINED_SLUG;
      const allyColor = TENANT_COLORS[l.tenant_id] || '#475569';
      const borderStyle = showTenantTag ? `border-left:3px solid ${allyColor};` : '';
      const tenantTag = showTenantTag ? ` <span style="color:var(--muted);font-size:var(--fs-sm)">(${escapeHtml(tenantName(l.tenant_id))})</span>` : '';
      return `<tr class="pf-v6-c-table__tr" style="${dimStyle}${leadStyle}${borderStyle}">
        <td class="pf-v6-c-table__td">${escapeHtml(l.event_name)}${l.leadership_only ? ' 👑' : ' 🛡️'}${tenantTag}</td>
        <td class="pf-v6-c-table__td">${l.occurrence_date}</td>
        <td class="pf-v6-c-table__td" style="color:var(--muted);font-size:var(--fs-sm)">${l.timing}</td>
        <td class="pf-v6-c-table__td" style="font-size:var(--fs-sm)">${l.posted_at_utc ? fmtDateTime(l.posted_at_utc) : '—'}</td>
        <td class="pf-v6-c-table__td" style="font-size:var(--fs-sm);color:var(--muted)">${escapeHtml(l.posted_by)}</td>
        <td class="pf-v6-c-table__td">${occurrenceStatusBadge(l.status)}</td>
        <td class="pf-v6-c-table__td" style="font-size:var(--fs-xs);color:var(--muted);font-family:monospace">${l.discord_event_id ? escapeHtml(l.discord_event_id.slice(0,20))+'…' : '—'}</td>
      </tr>`;
    }).join('');
}


// Wire this view's filter/export controls — replaces their oninput/onclick
// attributes (Phase 3 audit remediation).
document.getElementById('postLogFilterInput')?.addEventListener('input', () => filterPostLogTable());
document.getElementById('btnExportPostLogCsv')?.addEventListener('click', () => exportPostLogCsv());
