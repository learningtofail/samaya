// Post Log view (#v-postlog): PostLog table and CSV export. Depends on
// common.js.

async function exportPostLogCsv() {
  try {
    const res = await fetch('/admin/api/post-log/export.csv', {
      headers: { 'X-Tenant-Slug': getCurrentTenantSlug() },
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

async function loadPostLog() {
  try {
    const logs = await api('GET', '/api/post-log?limit=100');
    const tbody = document.getElementById('logBody');
    if (!logs.length) {
      tbody.innerHTML = '<tr class="pf-v6-c-table__tr"><td class="pf-v6-c-table__td" colspan="7" style="color:var(--muted);padding:20px">No posts yet.</td></tr>';
      return;
    }
    const statusColor = { posted: 'pf-m-green', active: 'pf-m-blue', completed: 'pf-m-grey', cancelled: 'pf-m-red', pending: 'pf-m-grey', error: 'pf-m-red' };
    tbody.innerHTML = logs.map(l => {
      const dimStyle = l.status === 'cancelled' ? 'opacity:.5;' : '';
      const leadStyle = l.leadership_only ? 'background:var(--bg3);' : '';
      return `<tr class="pf-v6-c-table__tr" style="${dimStyle}${leadStyle}">
        <td class="pf-v6-c-table__td">${escapeHtml(l.event_name)}${l.leadership_only ? ' 👑' : ' 🛡️'}</td>
        <td class="pf-v6-c-table__td">${l.occurrence_date}</td>
        <td class="pf-v6-c-table__td" style="color:var(--muted);font-size:var(--fs-sm)">${l.timing}</td>
        <td class="pf-v6-c-table__td" style="font-size:var(--fs-sm)">${l.posted_at_utc ? fmtDateTime(l.posted_at_utc) : '—'}</td>
        <td class="pf-v6-c-table__td" style="font-size:var(--fs-sm);color:var(--muted)">${escapeHtml(l.posted_by)}</td>
        <td class="pf-v6-c-table__td">${pfLabel(escapeHtml(l.status), statusColor[l.status] || 'pf-m-grey')}</td>
        <td class="pf-v6-c-table__td" style="font-size:var(--fs-xs);color:var(--muted);font-family:monospace">${l.discord_event_id ? escapeHtml(l.discord_event_id.slice(0,20))+'…' : '—'}</td>
      </tr>`;
    }).join('');
  } catch(e) { toast(e.message, true); }
}

