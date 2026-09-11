async function exportPostLogCsv() {
  try {
    const res = await fetch('/admin/api/post-log/export.csv', {
      headers: { 'X-Admin-Key': getAdminKey() },
    });
    if (res.status === 401) { clearAdminKey(); throw new Error('Admin key rejected — reload and re-enter it.'); }
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
      tbody.innerHTML = '<tr><td colspan="7" style="color:var(--muted);padding:20px">No posts yet.</td></tr>';
      return;
    }
    tbody.innerHTML = logs.map(l => {
      const dimStyle = l.status === 'cancelled' ? 'opacity:.5;' : '';
      const leadStyle = l.leadership_only ? 'background:var(--bg3);' : '';
      return `<tr style="${dimStyle}${leadStyle}">
        <td>${escapeHtml(l.event_name)}${l.leadership_only ? ' 👑' : ' 🛡️'}</td>
        <td>${l.occurrence_date}</td>
        <td style="color:var(--muted);font-size:var(--fs-sm)">${l.timing}</td>
        <td style="font-size:var(--fs-sm)">${l.posted_at_utc ? fmtDateTime(l.posted_at_utc) : '—'}</td>
        <td style="font-size:var(--fs-sm);color:var(--muted)">${escapeHtml(l.posted_by)}</td>
        <td><span class="badge badge-${escapeHtml(l.status)}">${escapeHtml(l.status)}</span></td>
        <td style="font-size:var(--fs-xs);color:var(--muted);font-family:monospace">${l.discord_event_id ? escapeHtml(l.discord_event_id.slice(0,20))+'…' : '—'}</td>
      </tr>`;
    }).join('');
  } catch(e) { toast(e.message, true); }
}

// ── Config ──────────────────────────────────────────────────

