// Gantt timeline, now a layout within the Schedule tab (#scheduleGanttView)
// rather than its own tab — rendered by schedule.js's loadSchedule() from
// the same occurrenceData it already fetched, so switching layouts is
// instant and never re-fetches. Depends on common.js (GANTT_PALETTE, DOW3).

function renderGantt(occs) {
  const community = occs.filter(o => !o.leadership_only);
  const leadership = occs.filter(o => o.leadership_only);

  buildGanttGrid(community, 'ganttWrap', 'ganttLegend', 'No occurrences in this window.');

  const leadWrap = document.getElementById('ganttLeadershipWrap');
  if (leadership.length) {
    leadWrap.style.display = 'block';
    buildGanttGrid(leadership, 'ganttWrapLeadership', 'ganttLegendLeadership', 'No leadership occurrences in this window.');
  } else {
    leadWrap.style.display = 'none';
    document.getElementById('ganttWrapLeadership').innerHTML = '';
    document.getElementById('ganttLegendLeadership').innerHTML = '';
  }
}

// Spec §49 — Announcements' own timeline section, separate from the Events
// timeline above (see admin.html's ganttAnnouncementsWrap comment for why).
// Reuses buildGanttGrid by mapping each Announcement into the same
// {event_name, occurrence_date, owning_tenant_id, start_datetime_utc} shape
// an Occurrence already has. Unlike an Event's recurrence — expanded into
// real Occurrence rows up front by regenerate_occurrences — a recurring
// Announcement only ever has ONE live scheduled_for at a time (the next
// send, advanced in place after each delivery per scheduler/announcements.py),
// so this only ever plots a single marker per announcement, never a
// projected range of future sends.
function renderAnnouncementGantt(announcements) {
  const wrap = document.getElementById('ganttAnnouncementsWrap');
  if (!announcements.length) {
    wrap.style.display = 'none';
    document.getElementById('ganttWrapAnnouncements').innerHTML = '';
    document.getElementById('ganttLegendAnnouncements').innerHTML = '';
    return;
  }
  wrap.style.display = 'block';
  const mapped = announcements.map(a => ({
    event_name: a.title,
    occurrence_date: a.scheduled_for.slice(0, 10),
    owning_tenant_id: a.owning_tenant_id,
    start_datetime_utc: a.scheduled_for,
  }));
  buildGanttGrid(mapped, 'ganttWrapAnnouncements', 'ganttLegendAnnouncements', 'No scheduled announcements in this window.');
}

function buildGanttGrid(occs, wrapId, legendId, emptyMsg) {
  const wrapEl = document.getElementById(wrapId);
  const legendEl = document.getElementById(legendId);

  if (!occs.length) {
    wrapEl.innerHTML = `<p style="color:var(--muted)">${emptyMsg}</p>`;
    legendEl.innerHTML = '';
    return;
  }

  const today = new Date();
  today.setHours(0,0,0,0);
  const days = Array.from({length:28}, (_,i) => {
    const d = new Date(today);
    d.setDate(d.getDate()+i);
    return d;
  });

  // Group by event name
  const eventMap = new Map();
  occs.forEach(o => {
    if (!eventMap.has(o.event_name)) eventMap.set(o.event_name, { owningTenantId: o.owning_tenant_id, occs: new Set() });
    eventMap.get(o.event_name).occs.add(o.occurrence_date);
  });

  const events = [...eventMap.entries()];
  const palette = GANTT_PALETTE;

  // Build month header
  const months = [];
  days.forEach(d => {
    const label = d.toLocaleString('default',{month:'short',year:'numeric'});
    if (!months.length || months[months.length-1].label !== label) {
      months.push({label, count: 1});
    } else {
      months[months.length-1].count++;
    }
  });

  let html = '<table class="gantt-table"><colgroup><col style="min-width:160px">';
  html += '<col style="width:28px">'; // count col
  days.forEach(() => html += '<col style="width:32px">');
  html += '</colgroup>';

  // Month row
  html += '<tr><th class="gantt-name gantt-header">Event</th><th class="gantt-header">×</th>';
  months.forEach(m => html += `<th class="gantt-header" colspan="${m.count}">${m.label}</th>`);
  html += '</tr>';

  // DOW row
  html += '<tr><td class="gantt-name gantt-header"></td><td class="gantt-header"></td>';
  days.forEach(d => {
    const isT = d.toISOString().slice(0,10) === today.toISOString().slice(0,10);
    const isW = d.getDay()===0||d.getDay()===6;
    html += `<td class="gantt-header ${isT?'gantt-today':isW?'gantt-weekend':''}">${DOW3[d.getDay()][0]}</td>`;
  });
  html += '</tr>';

  // Date row
  html += '<tr><td class="gantt-name gantt-header"></td><td class="gantt-header"></td>';
  days.forEach(d => {
    const isT = d.toISOString().slice(0,10) === today.toISOString().slice(0,10);
    const isW = d.getDay()===0||d.getDay()===6;
    html += `<td class="gantt-header ${isT?'gantt-today':isW?'gantt-weekend':''}">${d.getDate()}</td>`;
  });
  html += '</tr>';

  // Event rows
  events.forEach(([name, {owningTenantId, occs: occDates}], idx) => {
    const color = palette[idx % palette.length];
    const allyColor = TENANT_COLORS[owningTenantId] || '#475569';
    const count = [...occDates].filter(d => {
      const dd = new Date(d+'T12:00:00Z');
      return dd >= today;
    }).length;

    html += `<tr>`;
    html += `<td class="gantt-name" style="border-left:3px solid ${allyColor}">`;
    html += `<span class="cat-dot" style="background:${color}"></span>${escapeHtml(name)}</td>`;
    html += `<td style="text-align:center;font-size:var(--fs-xs);color:var(--muted);background:var(--bg3)">${count}</td>`;

    days.forEach(d => {
      const ds = d.toISOString().slice(0,10);
      const isT = ds === today.toISOString().slice(0,10);
      const isW = d.getDay()===0||d.getDay()===6;
      const isWeekSep = days.indexOf(d) > 0 && days.indexOf(d) % 7 === 0;
      const occ = occDates.has(ds);
      let bg = occ ? color : isT ? 'var(--amber)' : isW ? 'var(--bg3)' : 'var(--bg)';
      const border = isWeekSep ? 'border-left:2px solid #94A3B8' : '';
      // Cell space is too tight for a full dual-time string (spec §15.5)
      // — the visible label stays a single short local time
      // (fmtTimeShort), and the full "UTC · local" pairing goes in the
      // tooltip instead, where hovering is the "shown" for this view.
      const occObj = occ ? occs.find(o => o.event_name===name && o.occurrence_date===ds) : null;
      const title = occObj ? `${escapeHtml(name)} — ${ds} — ${dualTimeString(occObj.start_datetime_utc)}` : `${escapeHtml(name)} — ${ds}`;
      html += `<td class="gantt-cell ${occ?'gantt-occ':''}" style="background:${bg};${border}" title="${title}">`;
      if (occObj) {
        html += fmtTimeShort(occObj.start_datetime_utc);
      }
      html += '</td>';
    });
    html += '</tr>';
  });

  html += '</table>';
  wrapEl.innerHTML = html;

  // Legend — swatches match the per-event cell-fill colours above so they stay
  // usable for identifying overlapping events; the owning alliance's colour is shown as
  // the name-cell border accent instead (consistent with Schedule/Events tables).
  legendEl.innerHTML = events.map(([name], idx) => {
    const color = palette[idx % palette.length];
    return `<div style="display:flex;align-items:center;gap:5px;font-size:var(--fs-xs)">
      <div style="width:10px;height:10px;border-radius:2px;background:${color}"></div>
      ${escapeHtml(name)}
    </div>`;
  }).join('');
}

