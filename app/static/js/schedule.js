// Schedule view (#v-schedule): the per-occurrence list (post/cancel to
// Discord, toggle post_to_discord). Depends on common.js.

async function loadSchedule() {
  try {
    occurrenceData = await api('GET', '/api/occurrences');
    renderSchedule();
  } catch(e) { toast(e.message, true); }
}

function renderSchedule() {
  const today = new Date().toISOString().slice(0,10);
  const tbody = document.getElementById('schedBody');
  const tbodyLead = document.getElementById('schedBodyLeadership');
  const leadWrap = document.getElementById('schedLeadershipWrap');
  if (!occurrenceData.length) {
    tbody.innerHTML = '<tr><td colspan="9" style="color:var(--muted);padding:20px">No occurrences. Run "Regenerate Now" from the Dashboard.</td></tr>';
    leadWrap.style.display = 'none';
    return;
  }

  function buildRow(o, scope) {
    const isToday = o.occurrence_date === today;
    const allyColor = ALLIANCE_COLORS[o.alliance] || ALLIANCE_COLORS.Server;
    const rowStyle = 'border-left:3px solid ' + allyColor + (isToday ? ';background:var(--amber)' : '');
    const badgeClass = `badge-${escapeHtml(o.post_status)}`;
    return `<tr style="${rowStyle}">
      <td style="${isToday?'font-weight:600':''}">${o.occurrence_date}</td>
      <td>${DOW3[new Date(o.occurrence_date+'T12:00:00Z').getUTCDay()]}</td>
      <td><span class="cat-dot" style="background:${allyColor}"></span>${escapeHtml(o.event_name)}${o.leadership_only ? ' 👑' : ' 🛡️'}</td>
      <td>${fmtTime(o.start_datetime_utc)}</td>
      <td>${o.duration_hours}h</td>
      <td style="color:var(--muted);font-size:var(--fs-sm)">${escapeHtml(o.discord_channel)}</td>
      <td style="text-align:center">
        <input type="checkbox" data-occ-id="${o.id}" data-scope="${scope}"
          ${o.post_to_discord?'checked':''}
          ${['posted','cancelled'].includes(o.post_status)?'disabled':''}
          onchange="togglePostFlag(${o.id},this.checked)">
      </td>
      <td>
        <span class="badge ${badgeClass}">${escapeHtml(o.post_status)}</span>
        ${o.status_detail?`<span title="${escapeHtml(o.status_detail)}" style="cursor:help;margin-left:4px">⚠</span>`:''}
      </td>
      <td>
        ${o.post_status==='posted'
          ? `<button class="btn btn-danger btn-sm" onclick="cancelOccurrence(${o.id})">Cancel</button>`
          : `<button class="btn btn-accent btn-sm" onclick="postOne(${o.id})" ${['posted','queued'].includes(o.post_status)?'disabled':''}>Post</button>`
        }
      </td>
    </tr>`;
  }

  const community = occurrenceData.filter(o => !o.leadership_only);
  const leadership = occurrenceData.filter(o => o.leadership_only);

  tbody.innerHTML = community.length
    ? community.map(o => buildRow(o, 'Alliance')).join('')
    : '<tr><td colspan="9" style="color:var(--muted);padding:20px">No alliance occurrences in this window.</td></tr>';

  if (leadership.length) {
    leadWrap.style.display = 'block';
    tbodyLead.innerHTML = leadership.map(o => buildRow(o, 'Leadership')).join('');
  } else {
    leadWrap.style.display = 'none';
    tbodyLead.innerHTML = '';
  }
}

async function togglePostFlag(id, checked) {
  try {
    await api('PATCH', `/api/occurrences/${id}`, { post_to_discord: checked });
    const occ = occurrenceData.find(o => o.id === id);
    if (occ) occ.post_to_discord = checked;
  } catch(e) { toast(e.message, true); }
}

async function postOne(id) {
  try {
    const result = await api('POST', `/api/occurrences/${id}/post`);
    toast(`Posted — Discord ID: ${result.discord_event_id}`);
    loadSchedule();
  } catch(e) { toast(e.message, true); }
}

async function cancelOccurrence(id) {
  if (!confirm('Cancel this Discord event? This cannot be undone.')) return;
  try {
    await api('DELETE', `/api/occurrences/${id}/discord`);
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
      await api('POST', `/api/occurrences/${cb.dataset.occId}/post`);
      posted++;
    } catch(e) { errors++; }
  }
  toast(`Posted: ${posted}${errors?`  ·  Errors: ${errors}`:''}`);
  loadSchedule();
}

