// Dashboard view (#v-dashboard). Depends on common.js (api, toast) and
// services/discord_api indirectly via GET /api/status.

function pfLabel(text, color) {
  return `<span class="pf-v6-c-label ${color} pf-m-filled"><span class="pf-v6-c-label__content"><span class="pf-v6-c-label__text">${text}</span></span></span>`;
}

async function loadDashboard() {
  try {
    const s = await api('GET', '/api/status');
    document.getElementById('st-service').innerHTML = pfLabel('ok', 'pf-m-green');
    document.getElementById('st-discord').innerHTML = s.discord_connected
      ? pfLabel(escapeHtml(s.discord_bot), 'pf-m-green')
      : pfLabel('disconnected', 'pf-m-red');
    const regen = s.scheduler.regenerate_occurrences;
    document.getElementById('st-regen').textContent =
      regen.last_run ? new Date(regen.last_run).toUTCString().slice(0,25) : 'Never';
    document.getElementById('st-result').innerHTML = regen.last_result
      ? pfLabel(escapeHtml(regen.last_result), regen.last_result === 'success' ? 'pf-m-green' : 'pf-m-red')
      : '—';
  } catch(e) { toast(e.message, true); }

  // Today's events, plus an Upcoming (next 48h) window (spec §29) — "Today"
  // goes blank by evening even when something's happening early tomorrow,
  // so a coordinator checking in the night before had nothing to look at.
  try {
    const occs = await api('GET', '/api/occurrences');
    const today = new Date().toISOString().slice(0,10);
    const now = new Date();
    const todayOccs = occs.filter(o => o.occurrence_date === today);
    const upcomingOccs = occs
      .filter(o => {
        const start = new Date(o.start_datetime_utc);
        const hoursAhead = (start - now) / 3600000;
        return o.occurrence_date !== today && hoursAhead > 0 && hoursAhead <= 48;
      })
      .sort((a, b) => new Date(a.start_datetime_utc) - new Date(b.start_datetime_utc));

    const statusColor = { posted: 'pf-m-green', active: 'pf-m-blue', completed: 'pf-m-grey', cancelled: 'pf-m-red', pending: 'pf-m-grey' };
    function occurrenceCard(o) {
      return `
      <div class="pf-v6-c-card pf-v6-u-mb-sm">
        <div class="pf-v6-c-card__body" style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
          ${o.scope === 'kingdom-wide' ? '<span title="Kingdom-wide">🌐</span> ' : ''}
          <strong>${escapeHtml(o.event_name)}</strong>${o.leadership_only ? ' <span title="Leadership only">👑</span>' : ' <span title="Alliance">🛡️</span>'}
          <span style="color:var(--muted)">${fmtTime(o.start_datetime_utc)}</span>
          <span style="color:var(--muted);font-size:0.85em">${formatRelativeTime(new Date(o.start_datetime_utc))}</span>
          <span style="color:var(--muted)">${escapeHtml(o.discord_channel)}</span>
          ${pfLabel(escapeHtml(o.post_status), statusColor[o.post_status] || 'pf-m-grey')}
        </div>
      </div>`;
    }

    const todayEl = document.getElementById('todayEvents');
    todayEl.innerHTML = todayOccs.length
      ? todayOccs.map(occurrenceCard).join('')
      : '<p style="color:var(--muted)">No events scheduled for today.</p>';

    const upcomingEl = document.getElementById('upcomingEvents');
    if (upcomingEl) {
      upcomingEl.innerHTML = upcomingOccs.length
        ? upcomingOccs.map(occurrenceCard).join('')
        : '<p style="color:var(--muted)">Nothing else in the next 48 hours.</p>';
    }
  } catch(e) {}

  // Delivery Health (spec §31) — trailing-7-day rollup of both delivery
  // channels this app posts through, so a coordinator can spot a pattern
  // of failures without digging through Announcements or Post Log.
  try {
    const h = await api('GET', '/api/delivery-health');
    const el = document.getElementById('deliveryHealth');
    if (el) {
      function healthRow(label, posted, error) {
        const total = posted + error;
        const rate = total ? Math.round((posted / total) * 100) : null;
        return `
        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:6px">
          <strong style="min-width:120px">${label}</strong>
          ${pfLabel(posted + ' posted', 'pf-m-green')}
          ${error ? pfLabel(error + ' failed', 'pf-m-red') : ''}
          ${rate !== null ? `<span style="color:var(--muted);font-size:0.85em">${rate}% success</span>` : '<span style="color:var(--muted);font-size:0.85em">No activity in the last 7 days</span>'}
        </div>`;
      }
      el.innerHTML = healthRow('Announcements', h.announcements.posted, h.announcements.error)
        + healthRow('Event Posts', h.event_posts.posted, h.event_posts.error);
    }
  } catch(e) {}
}

async function triggerRegen() {
  try {
    await api('POST', '/api/scheduler/regenerate');
    toast('Regeneration complete');
    loadDashboard();
  } catch(e) { toast(e.message, true); }
}
