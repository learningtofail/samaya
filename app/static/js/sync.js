// Sync view (#v-sync): reconciles Samaya's PostLog against what's actually
// on Discord (matched/mismatched/discord_only/postlog_only), and the
// actions to resolve each case. Depends on common.js.

const STATUS_LABELS = {1:'Scheduled', 2:'Active', 3:'Completed', 4:'Cancelled'};

async function loadSync() {
  const el = document.getElementById('syncContent');
  el.innerHTML = '<p style="color:var(--muted)">Fetching Discord events…</p>';
  try {
    const data = await api('GET', '/api/sync/discord');
    const s = data.summary;

    // Update badge and dashboard card
    updateSyncBadge(s.issues);

    var html = '';

    // Summary bar
    html += '<div class="pf-v6-l-gallery pf-m-gutter pf-v6-u-mb-lg" style="--pf-v6-l-gallery--GridTemplateColumns--min: 140px">';
    html += statCard('Matched', s.matched, '#166534');
    html += statCard('Mismatched', s.mismatched, '#b45309');
    html += statCard('Discord Only', s.discord_only, '#1d4ed8');
    html += statCard('PostLog Only', s.postlog_only, '#991b1b');
    html += '</div>';

    // Mismatched section
    if (data.mismatched.length) {
      html += '<h2 class="pf-v6-c-title pf-m-md pf-v6-u-mb-xs">⚠ Mismatched (' + data.mismatched.length + ')</h2>';
      html += '<p class="pf-v6-u-font-size-sm pf-v6-u-mb-sm" style="color:var(--pf-t--global--text--color--subtle)">In both PostLog and Discord, but fields differ from the current event definition.</p>';
      html += '<table class="pf-v6-c-table pf-m-grid-md pf-v6-u-mb-lg">';
      html += '<thead class="pf-v6-c-table__thead"><tr class="pf-v6-c-table__tr"><th class="pf-v6-c-table__th">Event</th><th class="pf-v6-c-table__th">Date</th><th class="pf-v6-c-table__th">Field</th><th class="pf-v6-c-table__th">Samaya Value</th><th class="pf-v6-c-table__th">Discord Value</th><th class="pf-v6-c-table__th">Action</th></tr></thead><tbody class="pf-v6-c-table__tbody">';
      data.mismatched.forEach(function(m) {
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
              + '<button class="pf-v6-c-button pf-m-primary pf-m-small" title="Push Samaya values to Discord — updates the Discord event to match the current event definition." onclick="syncPush(' + m.post_log_id + ')">Push to Discord</button>'
              + '</td>';
          }
          html += '</tr>';
        });
      });
      html += '</tbody></table>';
    }

    // PostLog only section
    if (data.postlog_only.length) {
      html += '<h2 class="pf-v6-c-title pf-m-md pf-v6-u-mb-xs">🔴 PostLog Only (' + data.postlog_only.length + ')</h2>';
      html += '<p class="pf-v6-u-font-size-sm pf-v6-u-mb-sm" style="color:var(--pf-t--global--text--color--subtle)">In PostLog as posted but not found on Discord — may have been deleted directly in Discord.</p>';
      html += '<table class="pf-v6-c-table pf-m-grid-md pf-v6-u-mb-lg">';
      html += '<thead class="pf-v6-c-table__thead"><tr class="pf-v6-c-table__tr"><th class="pf-v6-c-table__th">Event</th><th class="pf-v6-c-table__th">Date</th><th class="pf-v6-c-table__th">Discord ID</th><th class="pf-v6-c-table__th">Posted At</th><th class="pf-v6-c-table__th">Action</th></tr></thead><tbody class="pf-v6-c-table__tbody">';
      data.postlog_only.forEach(function(p) {
        html += '<tr class="pf-v6-c-table__tr">';
        html += '<td class="pf-v6-c-table__td">' + escapeHtml(p.event_name) + '</td>';
        html += '<td class="pf-v6-c-table__td">' + p.occurrence_date + '</td>';
        html += '<td class="pf-v6-c-table__td" style="font-size:var(--fs-xs);font-family:monospace">' + escapeHtml(p.discord_event_id.slice(0,18)) + '…</td>';
        html += '<td class="pf-v6-c-table__td" style="font-size:var(--fs-sm)">' + (p.posted_at_utc ? p.posted_at_utc.slice(0,16).replace('T',' ') + ' UTC' : '—') + '</td>';
        html += '<td class="pf-v6-c-table__td"><button class="pf-v6-c-button pf-m-secondary pf-m-small" title="Mark this PostLog entry as cancelled — the Discord event no longer exists." onclick="syncMarkCancelled(' + p.post_log_id + ')">Mark Cancelled</button></td>';
        html += '</tr>';
      });
      html += '</tbody></table>';
    }

    // Discord only section
    if (data.discord_only.length) {
      html += '<h2 class="pf-v6-c-title pf-m-md pf-v6-u-mb-xs">🔵 Discord Only (' + data.discord_only.length + ')</h2>';
      html += '<p class="pf-v6-u-font-size-sm pf-v6-u-mb-sm" style="color:var(--pf-t--global--text--color--subtle)">On Discord but not in PostLog — created manually or outside Samaya.</p>';
      html += '<table class="pf-v6-c-table pf-m-grid-md pf-v6-u-mb-lg">';
      html += '<thead class="pf-v6-c-table__thead"><tr class="pf-v6-c-table__tr"><th class="pf-v6-c-table__th">Event Name</th><th class="pf-v6-c-table__th">Status</th><th class="pf-v6-c-table__th">Start Time</th><th class="pf-v6-c-table__th">Action</th></tr></thead><tbody class="pf-v6-c-table__tbody">';
      data.discord_only.forEach(function(d) {
        html += '<tr class="pf-v6-c-table__tr">';
        html += '<td class="pf-v6-c-table__td">' + escapeHtml(d.name) + '</td>';
        html += '<td class="pf-v6-c-table__td">' + escapeHtml(STATUS_LABELS[d.status] || d.status) + '</td>';
        html += '<td class="pf-v6-c-table__td" style="font-size:var(--fs-sm)">' + (d.scheduled_start ? d.scheduled_start.slice(0,16).replace('T',' ') + ' UTC' : '—') + '</td>';
        html += '<td class="pf-v6-c-table__td"><button class="pf-v6-c-button pf-m-secondary pf-m-small" title="Add this Discord event to PostLog." ' + 'data-discord-id="' + escapeHtml(d.discord_event_id) + '" onclick="syncAcknowledge(this)">Acknowledge</button></td>';
        html += '</tr>';
      });
      html += '</tbody></table>';
    }

    // Matched section (collapsible)
    if (data.matched.length) {
      html += '<details class="pf-v6-u-mb-md"><summary style="cursor:pointer;font-weight:500;padding:8px 0">✅ Matched (' + data.matched.length + ') — all good</summary>';
      html += '<table class="pf-v6-c-table pf-m-grid-md">';
      html += '<thead class="pf-v6-c-table__thead"><tr class="pf-v6-c-table__tr"><th class="pf-v6-c-table__th">Event</th><th class="pf-v6-c-table__th">Date</th><th class="pf-v6-c-table__th">Discord ID</th><th class="pf-v6-c-table__th">Discord Status</th></tr></thead><tbody class="pf-v6-c-table__tbody">';
      data.matched.forEach(function(m) {
        html += '<tr class="pf-v6-c-table__tr"><td class="pf-v6-c-table__td">' + escapeHtml(m.event_name) + '</td><td class="pf-v6-c-table__td">' + m.occurrence_date + '</td>';
        html += '<td class="pf-v6-c-table__td" style="font-size:var(--fs-xs);font-family:monospace">' + escapeHtml(m.discord_event_id.slice(0,18)) + '…</td>';
        html += '<td class="pf-v6-c-table__td">' + escapeHtml(STATUS_LABELS[m.discord_status] || m.discord_status) + '</td></tr>';
      });
      html += '</tbody></table></details>';
    }

    if (!data.mismatched.length && !data.discord_only.length && !data.postlog_only.length) {
      html += '<div class="pf-v6-c-alert pf-m-success pf-m-inline">';
      html += '<div class="pf-v6-c-alert__icon">✅</div>';
      html += '<p class="pf-v6-c-alert__title">Everything is in sync</p>';
      html += '<p class="pf-v6-c-alert__description">All PostLog entries match their Discord counterparts.</p>';
      html += '</div>';
    }

    el.innerHTML = html;
  } catch(e) { el.innerHTML = '<p style="color:#991b1b">Error: ' + escapeHtml(e.message) + '</p>'; }
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

async function syncPush(postLogId) {
  if (!confirm('Push Samaya values to Discord? This will update the Discord event name and description to match the current event definition.')) return;
  try {
    await api('POST', '/api/sync/push-by-log/' + postLogId);
    toast('Discord event updated successfully');
    loadSync();
  } catch(e) { toast(e.message, true); }
}

async function syncMarkCancelled(postLogId) {
  if (!confirm('Mark this PostLog entry as cancelled? This confirms the Discord event no longer exists.')) return;
  try {
    await api('POST', '/api/sync/mark-cancelled/' + postLogId);
    toast('PostLog entry marked as cancelled');
    loadSync();
  } catch(e) { toast(e.message, true); }
}

async function syncAcknowledge(btn) {
  var discordEventId = btn.getAttribute('data-discord-id');
  if (!confirm('Add this Discord event to PostLog as a manually-created record?')) return;
  try {
    await api('POST', '/api/sync/acknowledge/' + discordEventId);
    toast('Event acknowledged and added to PostLog');
    loadSync();
  } catch(e) { toast(e.message, true); }
}
