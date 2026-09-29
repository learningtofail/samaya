// Sync view (#v-sync): reconciles Samaya's PostLog against what's actually
// on Discord (matched/mismatched/discord_only/postlog_only), and the
// actions to resolve each case. Depends on common.js.
//
// Spec §38.4: GET /api/sync/discord is now get_current_tenants-based —
// it returns {"alliances": [...]}, one drift report per accessible
// alliance, filterable via the syncFilter dropdown like every other
// consolidated tab. Each alliance's report still carries its own
// tenant_slug/tenant_name/tenant_color, which every row below carries
// forward as a data attribute so the per-row fix actions (push/mark
// cancelled/acknowledge) send the *owning* alliance's header, not
// whatever this tab's filter happens to show.

const STATUS_LABELS = {1:'Scheduled', 2:'Active', 3:'Completed', 4:'Cancelled'};

let SYNC_CACHE = [];

async function loadSync() {
  renderAllianceFilterSelect('syncFilter', 'sync', loadSync);
  const el = document.getElementById('syncContent');
  el.innerHTML = '<p style="color:var(--muted)">Fetching Discord events…</p>';
  try {
    const data = await api('GET', '/api/sync/discord', null, false, getTabFilter('sync'));
    SYNC_CACHE = data.alliances;
    renderSync(SYNC_CACHE);
  } catch(e) { el.innerHTML = '<p style="color:#991b1b">Error: ' + escapeHtml(e.message) + '</p>'; }
}

function renderSync(alliances) {
  const el = document.getElementById('syncContent');
  const showTenantTag = alliances.length > 1;

  const totals = alliances.reduce((acc, a) => {
    if (a.error) { acc.errors.push(a); return acc; }
    acc.matched             += a.summary.matched;
    acc.mismatched          += a.summary.mismatched;
    acc.discord_only        += a.summary.discord_only;
    acc.postlog_only        += a.summary.postlog_only;
    acc.naturally_completed += a.summary.naturally_completed || 0;
    acc.issues              += a.summary.issues;
    return acc;
  }, {matched:0, mismatched:0, discord_only:0, postlog_only:0, naturally_completed:0, issues:0, errors:[]});

  updateSyncBadge(totals.issues);

  var html = '';

  html += '<div class="pf-v6-l-gallery pf-m-gutter pf-v6-u-mb-lg" style="--pf-v6-l-gallery--GridTemplateColumns--min: 140px">';
  html += statCard('Matched', totals.matched, '#166534');
  html += statCard('Mismatched', totals.mismatched, '#b45309');
  html += statCard('Discord Only', totals.discord_only, '#1d4ed8');
  html += statCard('PostLog Only', totals.postlog_only, '#991b1b');
  html += statCard('Completed', totals.naturally_completed, '#166534');
  html += '</div>';

  if (totals.errors.length) {
    totals.errors.forEach(a => {
      html += '<div class="pf-v6-c-alert pf-m-warning pf-m-inline pf-v6-u-mb-md">';
      html += '<div class="pf-v6-c-alert__icon">⚠</div>';
      html += '<p class="pf-v6-c-alert__title">' + escapeHtml(a.tenant_name) + ': ' + escapeHtml(a.error) + '</p>';
      html += '</div>';
    });
  }

  const reportable = alliances.filter(a => !a.error);
  const anyIssues = reportable.some(a => a.mismatched.length || a.discord_only.length || a.postlog_only.length);

  reportable.forEach(a => {
    const tag = showTenantTag ? `<span style="color:${a.tenant_color || 'var(--muted)'};font-weight:600"> — ${escapeHtml(a.tenant_name)}</span>` : '';

    if (a.mismatched.length) {
      html += '<h2 class="pf-v6-c-title pf-m-md pf-v6-u-mb-xs">⚠ Mismatched (' + a.mismatched.length + ')' + tag + '</h2>';
      html += '<p class="pf-v6-u-font-size-sm pf-v6-u-mb-sm" style="color:var(--pf-t--global--text--color--subtle)">In both PostLog and Discord, but fields differ from the current event definition.</p>';
      html += '<table class="pf-v6-c-table pf-m-grid-md pf-v6-u-mb-lg">';
      html += '<thead class="pf-v6-c-table__thead"><tr class="pf-v6-c-table__tr"><th class="pf-v6-c-table__th">Event</th><th class="pf-v6-c-table__th">Date</th><th class="pf-v6-c-table__th">Field</th><th class="pf-v6-c-table__th">Samaya Value</th><th class="pf-v6-c-table__th">Discord Value</th><th class="pf-v6-c-table__th">Action</th></tr></thead><tbody class="pf-v6-c-table__tbody">';
      a.mismatched.forEach(function(m) {
        m.diffs.forEach(function(d, i) {
          html += '<tr class="pf-v6-c-table__tr">';
          if (i === 0) {
            html += '<td class="pf-v6-c-table__td" rowspan="' + m.diffs.length + '">' + escapeHtml(m.event_name) + '</td>';
            html += '<td class="pf-v6-c-table__td" rowspan="' + m.diffs.length + '">' + m.occurrence_date + '</td>';
          }
          html += '<td class="pf-v6-c-table__td">' + escapeHtml(d.field) + '</td>';
          html += '<td class="pf-v6-c-table__td" style="font-size:var(--fs-sm);color:var(--muted)">' + escapeHtml(d.samaya || '—') + '</td>';
          html += '<td class="pf-v6-c-table__td" style="font-size:var(--fs-sm);color:#991b1b">' + escapeHtml(d.discord || '—') + '</td>';
          if (i === 0) {
            html += '<td class="pf-v6-c-table__td" rowspan="' + m.diffs.length + '">'
              + '<button class="pf-v6-c-button pf-m-primary pf-m-small" title="Push Samaya values to Discord — updates the Discord event to match the current event definition." onclick="syncPush(' + m.post_log_id + ',\'' + a.tenant_slug + '\')">Push to Discord</button>'
              + '</td>';
          }
          html += '</tr>';
        });
      });
      html += '</tbody></table>';
    }

    if (a.postlog_only.length) {
      html += '<h2 class="pf-v6-c-title pf-m-md pf-v6-u-mb-xs">🔴 PostLog Only (' + a.postlog_only.length + ')' + tag + '</h2>';
      html += '<p class="pf-v6-u-font-size-sm pf-v6-u-mb-sm" style="color:var(--pf-t--global--text--color--subtle)">In PostLog as posted but not found on Discord, and not yet due to have ended — likely deleted directly in Discord. (An event that simply finished shows up under Completed below instead.)</p>';
      html += '<table class="pf-v6-c-table pf-m-grid-md pf-v6-u-mb-lg">';
      html += '<thead class="pf-v6-c-table__thead"><tr class="pf-v6-c-table__tr"><th class="pf-v6-c-table__th">Event</th><th class="pf-v6-c-table__th">Date</th><th class="pf-v6-c-table__th">Discord ID</th><th class="pf-v6-c-table__th">Posted At</th><th class="pf-v6-c-table__th">Action</th></tr></thead><tbody class="pf-v6-c-table__tbody">';
      a.postlog_only.forEach(function(p) {
        html += '<tr class="pf-v6-c-table__tr">';
        html += '<td class="pf-v6-c-table__td">' + escapeHtml(p.event_name) + '</td>';
        html += '<td class="pf-v6-c-table__td">' + p.occurrence_date + '</td>';
        html += '<td class="pf-v6-c-table__td" style="font-size:var(--fs-xs);font-family:monospace">' + escapeHtml(p.discord_event_id.slice(0,18)) + '…</td>';
        html += '<td class="pf-v6-c-table__td" style="font-size:var(--fs-sm)">' + (p.posted_at_utc ? p.posted_at_utc.slice(0,16).replace('T',' ') + ' UTC' : '—') + '</td>';
        html += '<td class="pf-v6-c-table__td"><button class="pf-v6-c-button pf-m-secondary pf-m-small" title="Mark this PostLog entry as cancelled — the Discord event no longer exists." onclick="syncMarkCancelled(' + p.post_log_id + ',\'' + a.tenant_slug + '\')">Mark Cancelled</button></td>';
        html += '</tr>';
      });
      html += '</tbody></table>';
    }

    // Spec §44 — an event that simply ran its course: posted in PostLog,
    // no longer listed on Discord (Discord stops surfacing a COMPLETED
    // scheduled event fairly quickly, and this app has no way to be told
    // that happened in real time — see _sync_discord_for_tenant's own
    // comment), and Samaya's own schedule confirms it was already due to
    // have ended. Shown separately from PostLog Only, in green rather
    // than red, and deliberately left out of the "issues"/red badge count
    // — this is the expected, unremarkable outcome, not a drift problem.
    if (a.naturally_completed && a.naturally_completed.length) {
      html += '<h2 class="pf-v6-c-title pf-m-md pf-v6-u-mb-xs">✅ Completed (' + a.naturally_completed.length + ')' + tag + '</h2>';
      html += '<p class="pf-v6-u-font-size-sm pf-v6-u-mb-sm" style="color:var(--pf-t--global--text--color--subtle)">These simply finished — Discord stops listing a scheduled event once it ends, which looks like "not found" but isn’t a deletion. Nothing to fix; mark them completed to clear them from this report.</p>';
      html += '<table class="pf-v6-c-table pf-m-grid-md pf-v6-u-mb-lg">';
      html += '<thead class="pf-v6-c-table__thead"><tr class="pf-v6-c-table__tr"><th class="pf-v6-c-table__th">Event</th><th class="pf-v6-c-table__th">Date</th><th class="pf-v6-c-table__th">Discord ID</th><th class="pf-v6-c-table__th">Posted At</th><th class="pf-v6-c-table__th">Action</th></tr></thead><tbody class="pf-v6-c-table__tbody">';
      a.naturally_completed.forEach(function(p) {
        html += '<tr class="pf-v6-c-table__tr">';
        html += '<td class="pf-v6-c-table__td">' + escapeHtml(p.event_name) + '</td>';
        html += '<td class="pf-v6-c-table__td">' + p.occurrence_date + '</td>';
        html += '<td class="pf-v6-c-table__td" style="font-size:var(--fs-xs);font-family:monospace">' + escapeHtml(p.discord_event_id.slice(0,18)) + '…</td>';
        html += '<td class="pf-v6-c-table__td" style="font-size:var(--fs-sm)">' + (p.posted_at_utc ? p.posted_at_utc.slice(0,16).replace('T',' ') + ' UTC' : '—') + '</td>';
        html += '<td class="pf-v6-c-table__td"><button class="pf-v6-c-button pf-m-secondary pf-m-small" title="Mark this PostLog entry as completed and clear it from this report." onclick="syncMarkCompleted(' + p.post_log_id + ',\'' + a.tenant_slug + '\')">Mark Completed</button></td>';
        html += '</tr>';
      });
      html += '</tbody></table>';
    }

    if (a.discord_only.length) {
      html += '<h2 class="pf-v6-c-title pf-m-md pf-v6-u-mb-xs">🔵 Discord Only (' + a.discord_only.length + ')' + tag + '</h2>';
      html += '<p class="pf-v6-u-font-size-sm pf-v6-u-mb-sm" style="color:var(--pf-t--global--text--color--subtle)">On Discord but not in PostLog — created manually or outside Samaya.</p>';
      html += '<table class="pf-v6-c-table pf-m-grid-md pf-v6-u-mb-lg">';
      html += '<thead class="pf-v6-c-table__thead"><tr class="pf-v6-c-table__tr"><th class="pf-v6-c-table__th">Event Name</th><th class="pf-v6-c-table__th">Status</th><th class="pf-v6-c-table__th">Start Time</th><th class="pf-v6-c-table__th">Action</th></tr></thead><tbody class="pf-v6-c-table__tbody">';
      a.discord_only.forEach(function(d) {
        html += '<tr class="pf-v6-c-table__tr">';
        html += '<td class="pf-v6-c-table__td">' + escapeHtml(d.name) + '</td>';
        html += '<td class="pf-v6-c-table__td">' + escapeHtml(STATUS_LABELS[d.status] || d.status) + '</td>';
        html += '<td class="pf-v6-c-table__td" style="font-size:var(--fs-sm)">' + (d.scheduled_start ? d.scheduled_start.slice(0,16).replace('T',' ') + ' UTC' : '—') + '</td>';
        html += '<td class="pf-v6-c-table__td"><button class="pf-v6-c-button pf-m-secondary pf-m-small" title="Add this Discord event to PostLog." ' + 'data-discord-id="' + escapeHtml(d.discord_event_id) + '" data-tenant-slug="' + escapeHtml(a.tenant_slug) + '" onclick="syncAcknowledge(this)">Acknowledge</button></td>';
        html += '</tr>';
      });
      html += '</tbody></table>';
    }

    if (a.matched.length) {
      html += '<details class="pf-v6-u-mb-md"><summary style="cursor:pointer;font-weight:500;padding:8px 0">✅ Matched (' + a.matched.length + ')' + tag + ' — all good</summary>';
      html += '<table class="pf-v6-c-table pf-m-grid-md">';
      html += '<thead class="pf-v6-c-table__thead"><tr class="pf-v6-c-table__tr"><th class="pf-v6-c-table__th">Event</th><th class="pf-v6-c-table__th">Date</th><th class="pf-v6-c-table__th">Discord ID</th><th class="pf-v6-c-table__th">Discord Status</th></tr></thead><tbody class="pf-v6-c-table__tbody">';
      a.matched.forEach(function(m) {
        html += '<tr class="pf-v6-c-table__tr"><td class="pf-v6-c-table__td">' + escapeHtml(m.event_name) + '</td><td class="pf-v6-c-table__td">' + m.occurrence_date + '</td>';
        html += '<td class="pf-v6-c-table__td" style="font-size:var(--fs-xs);font-family:monospace">' + escapeHtml(m.discord_event_id.slice(0,18)) + '…</td>';
        html += '<td class="pf-v6-c-table__td">' + escapeHtml(STATUS_LABELS[m.discord_status] || m.discord_status) + '</td></tr>';
      });
      html += '</tbody></table></details>';
    }
  });

  if (!anyIssues && reportable.length) {
    html += '<div class="pf-v6-c-alert pf-m-success pf-m-inline">';
    html += '<div class="pf-v6-c-alert__icon">✅</div>';
    html += '<p class="pf-v6-c-alert__title">Everything is in sync</p>';
    html += '<p class="pf-v6-c-alert__description">All PostLog entries match their Discord counterparts.</p>';
    html += '</div>';
  }

  el.innerHTML = html || '<p style="color:var(--muted)">No alliances to report on.</p>';
}

function statCard(label, value, color) {
  return '<div class="pf-v6-c-card"><div class="pf-v6-c-card__body">'
    + '<p class="pf-v6-u-font-size-sm" style="color:var(--pf-t--global--text--color--subtle);text-transform:uppercase;letter-spacing:.04em">' + label + '</p>'
    + '<div style="font-size:var(--pf-t--global--font--size--heading--lg);font-weight:600;color:' + color + '">' + value + '</div>'
    + '</div></div>';
}

function updateSyncBadge(issueCount) {
  var badge = document.getElementById('syncBadge');
  var badgeText = document.getElementById('syncBadgeText');
  var card  = document.getElementById('syncSummaryCard');
  var countEl = document.getElementById('syncIssueCount');
  if (issueCount > 0) {
    badgeText.textContent = issueCount;
    badge.style.display = 'inline-flex';
    card.style.display = 'block';
    countEl.textContent = issueCount;
  } else {
    badge.style.display = 'none';
    card.style.display = 'none';
  }
}

async function syncPush(postLogId, tenantSlug) {
  if (!confirm('Push Samaya values to Discord? This will update the Discord event name and description to match the current event definition.')) return;
  try {
    await api('POST', '/api/sync/push-by-log/' + postLogId, null, false, tenantSlug);
    toast('Discord event updated successfully');
    loadSync();
  } catch(e) { toast(e.message, true); }
}

async function syncMarkCancelled(postLogId, tenantSlug) {
  if (!confirm('Mark this PostLog entry as cancelled? This confirms the Discord event no longer exists.')) return;
  try {
    await api('POST', '/api/sync/mark-cancelled/' + postLogId, null, false, tenantSlug);
    toast('PostLog entry marked as cancelled');
    loadSync();
  } catch(e) { toast(e.message, true); }
}

async function syncMarkCompleted(postLogId, tenantSlug) {
  try {
    await api('POST', '/api/sync/mark-completed/' + postLogId, null, false, tenantSlug);
    toast('PostLog entry marked as completed');
    loadSync();
  } catch(e) { toast(e.message, true); }
}

async function syncAcknowledge(btn) {
  var discordEventId = btn.getAttribute('data-discord-id');
  var tenantSlug = btn.getAttribute('data-tenant-slug');
  if (!confirm('Add this Discord event to PostLog as a manually-created record?')) return;
  try {
    await api('POST', '/api/sync/acknowledge/' + discordEventId, null, false, tenantSlug);
    toast('Event acknowledged and added to PostLog');
    loadSync();
  } catch(e) { toast(e.message, true); }
}
