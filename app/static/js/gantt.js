// Gantt view (#v-gantt): renders the occurrence timeline grid. Reads the
// same occurrenceData global common.js/schedule.js populate — does not
// re-fetch it. Depends on common.js (GANTT_PALETTE, DOW3).

async function loadGantt() {
  try {
    const occs = await api('GET', '/api/occurrences');
    renderGantt(occs);
  } catch(e) { toast(e.message, true); }
}

function renderGantt(occs) {
  const community = occs.filter(o => !o.leadership_only);
  const leadership = occs.filter(o => o.leadership_only);

  buildGanttGrid(community, 'ganttWrap', 'ganttLegend', 'No alliance occurrences in this window.');

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
    if (!eventMap.has(o.event_name)) eventMap.set(o.event_name, { alliance: o.alliance, occs: new Set() });
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
  events.forEach(([name, {alliance, occs: occDates}], idx) => {
    const color = palette[idx % palette.length];
    const allyColor = ALLIANCE_COLORS[alliance] || ALLIANCE_COLORS.Server;
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
      html += `<td class="gantt-cell ${occ?'gantt-occ':''}" style="background:${bg};${border}" title="${escapeHtml(name)} — ${ds}">`;
      if (occ) {
        const occObj = occs.find(o => o.event_name===name && o.occurrence_date===ds);
        html += occObj ? fmtTimeShort(occObj.start_datetime_utc) : '';
      }
      html += '</td>';
    });
    html += '</tr>';
  });

  html += '</table>';
  wrapEl.innerHTML = html;

  // Legend — swatches match the per-event cell-fill colours above so they stay
  // usable for identifying overlapping events; the alliance colour is shown as
  // the name-cell border accent instead (consistent with Schedule/Events tables).
  legendEl.innerHTML = events.map(([name], idx) => {
    const color = palette[idx % palette.length];
    return `<div style="display:flex;align-items:center;gap:5px;font-size:var(--fs-xs)">
      <div style="width:10px;height:10px;border-radius:2px;background:${color}"></div>
      ${escapeHtml(name)}
    </div>`;
  }).join('');
}

