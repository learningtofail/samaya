// Schedule view (#v-schedule): the per-occurrence list (post/cancel to
// Discord, toggle post_to_discord), plus the Gantt timeline as a second
// layout over the same data (merged in per user feedback on the original
// spec §38 build — Table and Timeline are one tab, not two). Depends on
// common.js and gantt.js's renderGantt().
//
// Spec §49 — scheduled Announcements are fetched and shown here too, in
// their own table/timeline sections (schedAnnouncementsWrap/
// ganttAnnouncementsWrap in admin.html), not merged into schedTable's rows:
// an Announcement has no start/end time, duration, or post_to_discord
// toggle the way an Occurrence does. This reuses announcements.js's own
// ANNOUNCEMENTS cache (rather than a Schedule-local one) so editAnnouncement/
// cancelAnnouncement/deleteAnnouncement work identically whether opened from
// here or from the Announcements tab itself, even if that tab was never
// visited this session. The cache itself is declared in common.js, not
// announcements.js — see that declaration's comment.

async function loadSchedule() {
  renderAllianceFilterSelect('scheduleFilter', 'schedule', loadSchedule);
  try {
    occurrenceData = await api('GET', '/api/occurrences', null, false, getTabFilter('schedule'));
    renderSchedule();
    renderGantt(occurrenceData);
    ANNOUNCEMENTS = await api('GET', '/api/announcements', null, false, getTabFilter('schedule'));
    const scheduled = ANNOUNCEMENTS.filter(a => a.status === 'scheduled');
    renderScheduleAnnouncements(scheduled);
    renderAnnouncementGantt(scheduled);
  } catch(e) { toast(e.message, true); }
}

function renderScheduleAnnouncements(items) {
  const wrap = document.getElementById('schedAnnouncementsWrap');
  const tbody = document.getElementById('schedAnnouncementsBody');
  if (!items.length) {
    wrap.classList.add('hidden');
    tbody.innerHTML = '';
    return;
  }
  wrap.classList.remove('hidden');

  function targetsHtml(a) {
    return a.targets.map(t => {
      const targetTenant = TENANTS.find(x => x.id === t.tenant_id);
      const slug = targetTenant ? targetTenant.slug : '';
      const channelSpan = '<span data-notif-channel="' + escapeHtml(slug) + ':' + escapeHtml(t.discord_channel_id) + '">#' + escapeHtml(t.discord_channel_id) + '</span>';
      return '<div>' + escapeHtml(tenantName(t.tenant_id)) + ': ' + channelSpan + ' ' + occurrenceStatusBadge(t.post_status) + '</div>';
    }).join('');
  }

  tbody.innerHTML = items.map(a => {
    const allianceLabel = a.scope === 'kingdom-wide'
      ? '🌐 Kingdom-wide <span style="color:var(--muted);font-size:var(--fs-sm)">(via ' + escapeHtml(a.owning_tenant_name || tenantName(a.owning_tenant_id)) + '’s Kingdom)</span>'
      : '<span class="cat-dot" style="background:' + (TENANT_COLORS[a.owning_tenant_id] || '#475569') + '"></span>' + escapeHtml(a.owning_tenant_name || tenantName(a.owning_tenant_id));
    const recurringBadge = a.recurring
      ? pfLabel('Every ' + a.interval_days + 'd', 'pf-m-purple')
      : pfLabel('One-time', 'pf-m-gray');
    // Spec §49 — no post-to-discord toggle/Post/Cancel-occurrence controls
    // here (those are Events-only concepts); Edit and Cancel cover a still-
    // 'scheduled' announcement (this table only ever shows 'scheduled'
    // ones, so there's nothing here yet in a terminal state a Delete button
    // would actually work on — the full Delete/Retry/Duplicate action set
    // remains on the Announcements tab itself).
    return '<tr class="pf-v6-c-table__tr">'
      + '<td class="pf-v6-c-table__td">' + escapeHtml(a.title) + (a.leadership_only ? ' 👑' : ' 🛡️') + '</td>'
      + '<td class="pf-v6-c-table__td">' + allianceLabel + '</td>'
      + '<td class="pf-v6-c-table__td">' + recurringBadge + '</td>'
      + '<td class="pf-v6-c-table__td">' + fmtDateTime(a.scheduled_for) + '</td>'
      + '<td class="pf-v6-c-table__td">' + announcementStatusBadgeAdmin(a.status) + '</td>'
      + '<td class="pf-v6-c-table__td">' + targetsHtml(a) + '</td>'
      + '<td class="pf-v6-c-table__td"><div style="display:flex;gap:6px;flex-wrap:wrap">'
      + '<button class="pf-v6-c-button pf-m-secondary pf-m-small" onclick="editAnnouncement(' + a.id + ')" title="Edit this announcement in place">Edit</button>'
      + '<button class="pf-v6-c-button pf-m-danger pf-m-small" onclick="cancelAnnouncement(' + a.id + ')">Cancel</button>'
      + '</div></td>'
      + '</tr>';
  }).join('');
  enhanceNotificationTargetLabels();
}

// Persisted so returning to the tab keeps whichever layout was last picked.
function setScheduleLayout(layout) {
  localStorage.setItem('samaya_schedule_layout', layout);
  document.getElementById('scheduleTableView').classList.toggle('hidden', layout !== 'table');
  document.getElementById('scheduleGanttView').classList.toggle('hidden', layout !== 'gantt');
  document.getElementById('scheduleLayoutTableBtn').className = 'pf-v6-c-button pf-m-small ' + (layout === 'table' ? 'pf-m-primary' : 'pf-m-secondary');
  document.getElementById('scheduleLayoutGanttBtn').className = 'pf-v6-c-button pf-m-small ' + (layout === 'gantt' ? 'pf-m-primary' : 'pf-m-secondary');
}

// Spec §38.1: an occurrence's own owning alliance (not whatever this
// tab's filter is currently set to) is what the write endpoints below
// actually need — get_occurrence_with_event checks the header tenant
// against the occurrence's real tenant/kingdom, so the wrong slug 404s.
function occurrenceOwningSlug(id) {
  const o = occurrenceData.find(x => x.id === id);
  return o ? tenantSlugFor(o.owning_tenant_id) : undefined;
}

function renderSchedule() {
  const today = new Date().toISOString().slice(0,10);
  const tbody = document.getElementById('schedBody');
  const tbodyLead = document.getElementById('schedBodyLeadership');
  const leadWrap = document.getElementById('schedLeadershipWrap');
  if (!occurrenceData.length) {
    tbody.innerHTML = '<tr class="pf-v6-c-table__tr"><td class="pf-v6-c-table__td" colspan="9" style="color:var(--muted);padding:20px">No occurrences. Run "Regenerate Now" from the Dashboard.</td></tr>';
    leadWrap.classList.add('hidden');
    return;
  }

  function buildRow(o, sectionScope) {
    // sectionScope is 'Alliance' or 'Leadership' (which table section this
    // row is in, used by postSelected()'s bulk actions) — distinct from
    // o.scope, the event's own alliance/kingdom-wide field below.
    const isToday = o.occurrence_date === today;
    const allyColor = TENANT_COLORS[o.owning_tenant_id] || '#475569';
    const rowStyle = 'border-left:3px solid ' + allyColor + (isToday ? ';background:var(--amber)' : '');
    return `<tr class="pf-v6-c-table__tr samaya-row-clickable" style="${rowStyle}" onclick="handleRowPreviewClick(event,'occurrence',${escapeHtml(JSON.stringify(o))})" title="Click to preview how this looks on Discord">
      <td class="pf-v6-c-table__td" style="${isToday?'font-weight:600':''}">${o.occurrence_date}</td>
      <td class="pf-v6-c-table__td">${DOW3[new Date(o.occurrence_date+'T12:00:00Z').getUTCDay()]}</td>
      <td class="pf-v6-c-table__td"><span class="cat-dot" style="background:${allyColor}"></span>${escapeHtml(o.event_name)}${o.scope === 'kingdom-wide' ? ' 🌐' : ''}${o.leadership_only ? ' 👑' : ' 🛡️'}${getTabFilter('schedule') === COMBINED_SLUG ? ' <span style="color:var(--muted);font-size:var(--fs-sm)">(' + escapeHtml(tenantName(o.owning_tenant_id)) + ')</span>' : ''}</td>
      <td class="pf-v6-c-table__td">${fmtTime(o.start_datetime_utc)}<br><span style="color:var(--muted);font-size:0.8em">${formatRelativeTime(new Date(o.start_datetime_utc))}</span></td>
      <td class="pf-v6-c-table__td">${o.duration_hours}h</td>
      <td class="pf-v6-c-table__td" style="color:var(--muted);font-size:var(--fs-sm)">${escapeHtml(o.discord_channel)}</td>
      <td class="pf-v6-c-table__td" style="text-align:center">
        <span class="pf-v6-c-check pf-m-standalone">
          <input class="pf-v6-c-check__input" type="checkbox" data-occ-id="${o.id}" data-scope="${sectionScope}"
            ${o.post_to_discord?'checked':''}
            ${['posted','cancelled'].includes(o.post_status)?'disabled':''}
            onchange="togglePostFlag(${o.id},this.checked)">
        </span>
      </td>
      <td class="pf-v6-c-table__td">
        ${occurrenceStatusBadge(o.post_status)}
        ${o.status_detail?`<span title="${escapeHtml(o.status_detail)}" style="cursor:help;margin-left:4px">⚠</span>`:''}
      </td>
      <td class="pf-v6-c-table__td">
        ${o.post_status==='posted'
          ? `<button class="pf-v6-c-button pf-m-danger pf-m-small" onclick="cancelOccurrence(${o.id})">Cancel</button>`
          : `<button class="pf-v6-c-button pf-m-primary pf-m-small" onclick="postOne(${o.id})" ${o.post_status==='posted'?'disabled':''}>Post</button>`
        }
      </td>
    </tr>`;
  }

  const community = occurrenceData.filter(o => !o.leadership_only);
  const leadership = occurrenceData.filter(o => o.leadership_only);

  tbody.innerHTML = community.length
    ? community.map(o => buildRow(o, 'Alliance')).join('')
    : '<tr class="pf-v6-c-table__tr"><td class="pf-v6-c-table__td" colspan="9" style="color:var(--muted);padding:20px">No occurrences in this window.</td></tr>';

  if (leadership.length) {
    leadWrap.classList.remove('hidden');
    tbodyLead.innerHTML = leadership.map(o => buildRow(o, 'Leadership')).join('');
  } else {
    leadWrap.classList.add('hidden');
    tbodyLead.innerHTML = '';
  }
}

async function togglePostFlag(id, checked) {
  try {
    await api('PATCH', `/api/occurrences/${id}`, { post_to_discord: checked }, false, occurrenceOwningSlug(id));
    const occ = occurrenceData.find(o => o.id === id);
    if (occ) occ.post_to_discord = checked;
  } catch(e) { toast(e.message, true); }
}

async function postOne(id) {
  try {
    const result = await api('POST', `/api/occurrences/${id}/post`, null, false, occurrenceOwningSlug(id));
    toast(`Posted — Discord ID: ${result.discord_event_id}`);
    loadSchedule();
  } catch(e) { toast(e.message, true); }
}

async function cancelOccurrence(id) {
  if (!confirm('Cancel this Discord event? This cannot be undone.')) return;
  try {
    await api('DELETE', `/api/occurrences/${id}/discord`, null, false, occurrenceOwningSlug(id));
    toast('Event cancelled on Discord');
    loadSchedule();
  } catch(e) { toast(e.message, true); }
}

async function postSelected(scope) {
  const checked = document.querySelectorAll(`input[data-occ-id][data-scope="${scope}"]:checked:not(:disabled)`);
  if (!checked.length) { toast('No events checked for posting'); return; }
  let posted = 0, errors = 0;
  for (const cb of checked) {
    try {
      await api('POST', `/api/occurrences/${cb.dataset.occId}/post`, null, false, occurrenceOwningSlug(parseInt(cb.dataset.occId, 10)));
      posted++;
    } catch(e) { errors++; }
  }
  toast(`Posted: ${posted}${errors?`  ·  Errors: ${errors}`:''}`);
  loadSchedule();
}


// Wire this view's layout toggle/refresh/bulk-post controls — replaces
// their onclick attributes (Phase 3 audit remediation).
document.getElementById('scheduleLayoutTableBtn')?.addEventListener('click', () => setScheduleLayout('table'));
document.getElementById('scheduleLayoutGanttBtn')?.addEventListener('click', () => setScheduleLayout('gantt'));
document.getElementById('btnScheduleRefresh')?.addEventListener('click', () => loadSchedule());
document.getElementById('btnPostSelectedAlliance')?.addEventListener('click', () => postSelected('Alliance'));
document.getElementById('btnPostSelectedLeadership')?.addEventListener('click', () => postSelected('Leadership'));
