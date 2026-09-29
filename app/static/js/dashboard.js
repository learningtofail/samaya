// Dashboard view (#v-dashboard). Depends on common.js (api, toast).
// Spec §38.2: no more single "current tenant" — this view has its own
// "Alliance: [All ▾]" filter (dashboardFilter), defaulting to every
// accessible alliance. GET /api/status and GET /api/delivery-health now
// return one row per alliance ("alliances": [...]) instead of one
// tenant's numbers, so narrowing to "All" still works the same way as
// narrowing to one real slug — just more rows.

function pfLabel(text, color) {
  return `<span class="pf-v6-c-label ${color} pf-m-filled"><span class="pf-v6-c-label__content"><span class="pf-v6-c-label__text">${text}</span></span></span>`;
}

// ── Canonical status vocabulary ──────────────────────────────────
// One word and one color per underlying meaning, used everywhere a
// status pill appears — admin (here, schedule.js, announcements.js) and
// public (events.html, which has no shared JS with this file so carries
// its own identical copy — see CLAUDE.md §22). Replaces printing the raw
// post_status/Announcement.status DB string directly, which is what made
// admin pills read differently from the public page's friendly labels
// for the exact same state. Live Now/Completed are event-only — they
// come from Discord's own Scheduled Event lifecycle (routers/webhooks.py),
// which an announcement has no equivalent of.
const STATUS_META = {
  scheduled: { text: 'Scheduled', color: 'pf-m-gray'  },
  announced: { text: 'Announced', color: 'pf-m-green' },
  live:      { text: 'Live Now',  color: 'pf-m-blue'  },
  completed: { text: 'Completed', color: 'pf-m-gray'  },
  failed:    { text: 'Failed',    color: 'pf-m-red'   },
  cancelled: { text: 'Cancelled', color: 'pf-m-red'   },
};

function occurrenceStatusBadge(status) {
  const key = {
    pending: 'scheduled', posted: 'announced', active: 'live',
    completed: 'completed', cancelled: 'cancelled', error: 'failed',
  }[status] || 'scheduled';
  const m = STATUS_META[key];
  return pfLabel(m.text, m.color);
}

function announcementStatusBadgeAdmin(status) {
  const key = {
    draft: 'scheduled', scheduled: 'scheduled', posted: 'announced',
    failed: 'failed', cancelled: 'cancelled',
  }[status] || 'scheduled';
  const m = STATUS_META[key];
  return pfLabel(m.text, m.color);
}

function tenantNameFor(tenantId) {
  const t = TENANTS.find(t => t.id === tenantId);
  return t ? t.name : null;
}

async function loadDashboard() {
  renderAllianceFilterSelect('dashboardFilter', 'dashboard', loadDashboard);
  const filter = getTabFilter('dashboard');

  try {
    const s = await api('GET', '/api/status', null, false, filter);
    const el = document.getElementById('st-alliances');
    if (el) {
      el.innerHTML = s.alliances.map(a => `
        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:4px">
          <strong style="min-width:90px">${escapeHtml(a.tenant_name)}</strong>
          ${a.discord_connected ? pfLabel(escapeHtml(a.discord_bot || 'connected'), 'pf-m-green') : pfLabel('disconnected', 'pf-m-red')}
          <span style="color:var(--muted);font-size:0.85em">last regen: ${a.scheduler.regenerate_occurrences.last_run ? new Date(a.scheduler.regenerate_occurrences.last_run).toUTCString().slice(0,25) : 'never'}</span>
        </div>`).join('') || '<p style="color:var(--muted)">No accessible alliances.</p>';
    }
  } catch(e) { toast(e.message, true); }

  // Today's events, plus an Upcoming (next 48h) window (spec §29) — "Today"
  // goes blank by evening even when something's happening early tomorrow,
  // so a coordinator checking in the night before had nothing to look at.
  try {
    const occs = await api('GET', '/api/occurrences', null, false, filter);
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

    function occurrenceCard(o) {
      const allianceName = tenantNameFor(o.owning_tenant_id);
      return `
      <div class="pf-v6-c-card pf-v6-u-mb-sm samaya-row-clickable" onclick="handleRowPreviewClick(event,'occurrence',${escapeHtml(JSON.stringify(o))})" title="Click to preview how this looks on Discord">
        <div class="pf-v6-c-card__body" style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
          ${o.scope === 'kingdom-wide' ? '<span title="Kingdom-wide">🌐</span> ' : ''}
          <strong>${escapeHtml(o.event_name)}</strong>${o.leadership_only ? ' <span title="Leadership only">👑</span>' : ' <span title="Alliance">🛡️</span>'}
          ${allianceName && filter === COMBINED_SLUG ? pfLabel(escapeHtml(allianceName), 'pf-m-purple') : ''}
          <span style="color:var(--muted)">${fmtTime(o.start_datetime_utc)}</span>
          <span style="color:var(--muted);font-size:0.85em">${formatRelativeTime(new Date(o.start_datetime_utc))}</span>
          <span style="color:var(--muted)">${escapeHtml(o.discord_channel)}</span>
          ${occurrenceStatusBadge(o.post_status)}
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

  // Delivery Health (spec §31/§38.2) — trailing-7-day rollup of both
  // delivery channels this app posts through, per alliance, so a
  // kingdom-wide "All" view doesn't collapse everyone's health into one
  // misleading sum and a problem alliance is visible at a glance.
  try {
    const h = await api('GET', '/api/delivery-health', null, false, filter);
    const el = document.getElementById('deliveryHealth');
    if (el) {
      function healthRow(label, posted, error) {
        const total = posted + error;
        const rate = total ? Math.round((posted / total) * 100) : null;
        return `
        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:6px">
          <strong style="min-width:120px">${escapeHtml(label)}</strong>
          ${pfLabel(posted + ' posted', 'pf-m-green')}
          ${error ? pfLabel(error + ' failed', 'pf-m-red') : ''}
          ${rate !== null ? `<span style="color:var(--muted);font-size:0.85em">${rate}% success</span>` : '<span style="color:var(--muted);font-size:0.85em">No activity in the last 7 days</span>'}
        </div>`;
      }
      el.innerHTML = h.alliances.map(a => `
        <div style="margin-bottom:10px">
          <div style="font-weight:600;margin-bottom:2px">${escapeHtml(a.tenant_name)}</div>
          ${healthRow('Announcements', a.announcements.posted, a.announcements.error)}
          ${healthRow('Event Posts', a.event_posts.posted, a.event_posts.error)}
        </div>`).join('') || '<p style="color:var(--muted)">No accessible alliances.</p>';
    }
  } catch(e) {}
}

// Spec §38.1: regeneration is inherently per-alliance (regenerate_occurrences
// runs against one tenant_id) — when the Dashboard's filter is "All", this
// fires it once per accessible alliance rather than guessing at a single
// target, and reports how many succeeded.
async function triggerRegen() {
  const filter = getTabFilter('dashboard');
  const targets = filter === COMBINED_SLUG ? TENANTS.map(t => t.slug) : [filter];
  let okCount = 0;
  for (const slug of targets) {
    try {
      await api('POST', '/api/scheduler/regenerate', null, false, slug);
      okCount++;
    } catch (e) { toast(`${slug}: ${e.message}`, true); }
  }
  if (okCount) toast(`Regeneration complete for ${okCount} alliance${okCount === 1 ? '' : 's'}`);
  loadDashboard();
}
