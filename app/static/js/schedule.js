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
    tbody.innerHTML = '<tr class="pf-v6-c-table__tr"><td class="pf-v6-c-table__td" colspan="9" style="color:var(--muted);padding:20px">No occurrences. Run "Regenerate Now" from the Dashboard.</td></tr>';
    leadWrap.style.display = 'none';
    return;
  }

  const statusColor = { posted: 'pf-m-green', active: 'pf-m-blue', completed: 'pf-m-grey', cancelled: 'pf-m-red', pending: 'pf-m-grey', queued: 'pf-m-blue', error: 'pf-m-red' };

  function buildRow(o, sectionScope) {
    // sectionScope is 'Alliance' or 'Leadership' (which table section this
    // row is in, used by postSelected()'s bulk actions) — distinct from
    // o.scope, the event's own alliance/kingdom-wide field below.
    const isToday = o.occurrence_date === today;
    const allyColor = TENANT_COLORS[o.owning_tenant_id] || '#475569';
    const rowStyle = 'border-left:3px solid ' + allyColor + (isToday ? ';background:var(--amber)' : '');
    return `<tr class="pf-v6-c-table__tr" style="${rowStyle}">
      <td class="pf-v6-c-table__td" style="${isToday?'font-weight:600':''}">${o.occurrence_date}</td>
      <td class="pf-v6-c-table__td">${DOW3[new Date(o.occurrence_date+'T12:00:00Z').getUTCDay()]}</td>
      <td class="pf-v6-c-table__td"><span class="cat-dot" style="background:${allyColor}"></span>${escapeHtml(o.event_name)}${o.scope === 'kingdom-wide' ? ' 🌐' : ''}${o.leadership_only ? ' 👑' : ' 🛡️'}</td>
      <td class="pf-v6-c-table__td">${fmtTime(o.start_datetime_utc)}</td>
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
        ${pfLabel(escapeHtml(o.post_status), statusColor[o.post_status] || 'pf-m-grey')}
        ${o.status_detail?`<span title="${escapeHtml(o.status_detail)}" style="cursor:help;margin-left:4px">⚠</span>`:''}
      </td>
      <td class="pf-v6-c-table__td">
        ${o.post_status==='posted'
          ? `<button class="pf-v6-c-button pf-m-danger pf-m-small" onclick="cancelOccurrence(${o.id})">Cancel</button>`
          : `<button class="pf-v6-c-button pf-m-primary pf-m-small" onclick="postOne(${o.id})" ${['posted','queued'].includes(o.post_status)?'disabled':''}>Post</button>`
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

