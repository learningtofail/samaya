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
    html += '<div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px">';
    html += '<div class="stat" style="padding:10px 16px"><div class="stat-label">Matched</div><div style="font-size:var(--fs-lg);font-weight:600;color:#166534">' + s.matched + '</div></div>';
    html += '<div class="stat" style="padding:10px 16px"><div class="stat-label">Mismatched</div><div style="font-size:var(--fs-lg);font-weight:600;color:#b45309">' + s.mismatched + '</div></div>';
    html += '<div class="stat" style="padding:10px 16px"><div class="stat-label">Discord Only</div><div style="font-size:var(--fs-lg);font-weight:600;color:#1d4ed8">' + s.discord_only + '</div></div>';
    html += '<div class="stat" style="padding:10px 16px"><div class="stat-label">PostLog Only</div><div style="font-size:var(--fs-lg);font-weight:600;color:#991b1b">' + s.postlog_only + '</div></div>';
    html += '</div>';

    // Mismatched section
    if (data.mismatched.length) {
      html += '<h2 style="margin-bottom:8px">⚠ Mismatched (' + data.mismatched.length + ')</h2>';
      html += '<p style="font-size:var(--fs-sm);color:var(--muted);margin-bottom:10px">In both PostLog and Discord, but fields differ from the current event definition.</p>';
      html += '<div class="card" style="padding:0;overflow:hidden;margin-bottom:20px"><table>';
      html += '<thead><tr><th>Event</th><th>Date</th><th>Field</th><th>Samaya Value</th><th>Discord Value</th><th>Action</th></tr></thead><tbody>';
      data.mismatched.forEach(function(m) {
        m.diffs.forEach(function(d, i) {
          html += '<tr>';
          if (i === 0) {
            html += '<td rowspan="' + m.diffs.length + '">' + escapeHtml(m.event_name) + '</td>';
            html += '<td rowspan="' + m.diffs.length + '">' + m.occurrence_date + '</td>';
          }
          html += '<td>' + escapeHtml(d.field) + '</td>';
          html += '<td style="font-size:var(--fs-sm);color:var(--muted)">' + escapeHtml(d.samaya || '—') + '</td>';
          html += '<td style="font-size:var(--fs-sm);color:#991b1b">' + escapeHtml(d.discord || '—') + '</td>';
          if (i === 0) {
            html += '<td rowspan="' + m.diffs.length + '">'
              + '<button class="btn btn-accent btn-sm" title="Push Samaya values to Discord — updates the Discord event to match the current event definition." onclick="syncPush(' + m.post_log_id + ')">Push to Discord</button>'
              + '</td>';
          }
          html += '</tr>';
        });
      });
      html += '</tbody></table></div>';
    }

    // PostLog only section
    if (data.postlog_only.length) {
      html += '<h2 style="margin-bottom:8px">🔴 PostLog Only (' + data.postlog_only.length + ')</h2>';
      html += '<p style="font-size:var(--fs-sm);color:var(--muted);margin-bottom:10px">In PostLog as posted but not found on Discord — may have been deleted directly in Discord.</p>';
      html += '<div class="card" style="padding:0;overflow:hidden;margin-bottom:20px"><table>';
      html += '<thead><tr><th>Event</th><th>Date</th><th>Discord ID</th><th>Posted At</th><th>Action</th></tr></thead><tbody>';
      data.postlog_only.forEach(function(p) {
        html += '<tr>';
        html += '<td>' + escapeHtml(p.event_name) + '</td>';
        html += '<td>' + p.occurrence_date + '</td>';
        html += '<td style="font-size:var(--fs-xs);font-family:monospace">' + escapeHtml(p.discord_event_id.slice(0,18)) + '…</td>';
        html += '<td style="font-size:var(--fs-sm)">' + (p.posted_at_utc ? p.posted_at_utc.slice(0,16).replace('T',' ') + ' UTC' : '—') + '</td>';
        html += '<td><button class="btn btn-ghost btn-sm" title="Mark this PostLog entry as cancelled — the Discord event no longer exists." onclick="syncMarkCancelled(' + p.post_log_id + ')">Mark Cancelled</button></td>';
        html += '</tr>';
      });
      html += '</tbody></table></div>';
    }

    // Discord only section
    if (data.discord_only.length) {
      html += '<h2 style="margin-bottom:8px">🔵 Discord Only (' + data.discord_only.length + ')</h2>';
      html += '<p style="font-size:var(--fs-sm);color:var(--muted);margin-bottom:10px">On Discord but not in PostLog — created manually or outside Samaya.</p>';
      html += '<div class="card" style="padding:0;overflow:hidden;margin-bottom:20px"><table>';
      html += '<thead><tr><th>Event Name</th><th>Status</th><th>Start Time</th><th>Action</th></tr></thead><tbody>';
      data.discord_only.forEach(function(d) {
        html += '<tr>';
        html += '<td>' + escapeHtml(d.name) + '</td>';
        html += '<td>' + escapeHtml(STATUS_LABELS[d.status] || d.status) + '</td>';
        html += '<td style="font-size:var(--fs-sm)">' + (d.scheduled_start ? d.scheduled_start.slice(0,16).replace('T',' ') + ' UTC' : '—') + '</td>';
        html += '<td><button class="btn btn-ghost btn-sm" title="Add this Discord event to PostLog." ' + 'data-discord-id="' + escapeHtml(d.discord_event_id) + '" onclick="syncAcknowledge(this)">Acknowledge</button></td>';
        html += '</tr>';
      });
      html += '</tbody></table></div>';
    }

    // Matched section (collapsible)
    if (data.matched.length) {
      html += '<details style="margin-bottom:16px"><summary style="cursor:pointer;font-size:var(--fs-base);font-weight:500;padding:8px 0">✅ Matched (' + data.matched.length + ') — all good</summary>';
      html += '<div class="card" style="padding:0;overflow:hidden;margin-top:8px"><table>';
      html += '<thead><tr><th>Event</th><th>Date</th><th>Discord ID</th><th>Discord Status</th></tr></thead><tbody>';
      data.matched.forEach(function(m) {
        html += '<tr><td>' + escapeHtml(m.event_name) + '</td><td>' + m.occurrence_date + '</td>';
        html += '<td style="font-size:var(--fs-xs);font-family:monospace">' + escapeHtml(m.discord_event_id.slice(0,18)) + '…</td>';
        html += '<td>' + escapeHtml(STATUS_LABELS[m.discord_status] || m.discord_status) + '</td></tr>';
      });
      html += '</tbody></table></div></details>';
    }

    if (!data.mismatched.length && !data.discord_only.length && !data.postlog_only.length) {
      html += '<div style="text-align:center;padding:40px;color:#166534;background:#f0fdf4;border-radius:8px;border:1px solid #86efac">';
      html += '<p style="font-size:var(--fs-md);font-weight:500">✅ Everything is in sync</p>';
      html += '<p style="font-size:var(--fs-sm);margin-top:4px">All PostLog entries match their Discord counterparts.</p></div>';
    }

    el.innerHTML = html;
  } catch(e) { el.innerHTML = '<p style="color:#991b1b">Error: ' + escapeHtml(e.message) + '</p>'; }
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
