// Dashboard view (#v-dashboard). Depends on common.js (api, toast, CAT_COLORS)
// and services/discord_api indirectly via GET /api/status.

async function loadDashboard() {
  try {
    const s = await api('GET', '/api/status');
    document.getElementById('st-service').innerHTML =
      `<span class="badge badge-ok">ok</span>`;
    document.getElementById('st-discord').innerHTML = s.discord_connected
      ? `<span class="badge badge-ok">${escapeHtml(s.discord_bot)}</span>`
      : `<span class="badge badge-err">disconnected</span>`;
    const regen = s.scheduler.regenerate_occurrences;
    document.getElementById('st-regen').textContent =
      regen.last_run ? new Date(regen.last_run).toUTCString().slice(0,25) : 'Never';
    document.getElementById('st-result').innerHTML = regen.last_result
      ? `<span class="badge badge-${regen.last_result === 'success' ? 'ok' : 'err'}">${regen.last_result}</span>`
      : '—';
  } catch(e) { toast(e.message, true); }

  // Today's events
  try {
    const occs = await api('GET', '/api/occurrences');
    const today = new Date().toISOString().slice(0,10);
    const todayOccs = occs.filter(o => o.occurrence_date === today);
    const el = document.getElementById('todayEvents');
    if (!todayOccs.length) {
      el.innerHTML = '<p style="color:var(--muted)">No events scheduled for today.</p>';
      return;
    }
    el.innerHTML = todayOccs.map(o => `
      <div class="card" style="margin-bottom:8px;padding:12px;display:flex;align-items:center;gap:12px">
        <span class="cat-dot" style="background:${CAT_COLORS[o.category]||CAT_COLORS.Other}"></span>
        <strong>${escapeHtml(o.event_name)}</strong>${o.leadership_only ? ' <span title="Leadership only">👑</span>' : ' <span title="Alliance">🛡️</span>'}
        <span style="color:var(--muted)">${fmtTime(o.start_datetime_utc)}</span>
        <span style="color:var(--muted)">${escapeHtml(o.discord_channel)}</span>
        <span class="badge badge-${escapeHtml(o.post_status)}">${escapeHtml(o.post_status)}</span>
      </div>
    `).join('');
  } catch(e) {}
}

async function triggerRegen() {
  try {
    await api('POST', '/api/scheduler/regenerate');
    toast('Regeneration complete');
    loadDashboard();
  } catch(e) { toast(e.message, true); }
}
