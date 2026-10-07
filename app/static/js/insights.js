// Insights tab (#v-insights, spec §85): read-only views over the schedule, the
// delivery log and gift code runs. State lives in INSIGHTS; render*() functions
// draw from it. Every API string goes through escapeHtml. Depends on common.js.

const INSIGHTS = { heat: null, overlaps: null, trends: null, coverage: null, slots: null };
const WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
const WEEKDAYS_SHORT = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const HEAT_LEVELS = 4;

const WHEN_FORMAT = new Intl.DateTimeFormat('en-GB', {
  weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC',
});

function insightsWhen(iso) { return WHEN_FORMAT.format(new Date(iso)); }
function insightsClock(iso) {
  const d = new Date(iso);
  return String(d.getUTCHours()).padStart(2, '0') + ':' + String(d.getUTCMinutes()).padStart(2, '0');
}
function insightsHour(hour) { return String(hour).padStart(2, '0') + ':00'; }
function insightsPlural(n, word) { return `${n} ${word}${n === 1 ? '' : 's'}`; }

// 0 for an empty cell, otherwise 1..HEAT_LEVELS relative to the busiest cell.
function heatLevel(count, max) {
  if (!count || !max) return 0;
  return Math.max(1, Math.ceil((count / max) * HEAT_LEVELS));
}

function insightsSeconds(value) {
  if (value === null || value === undefined) return 'n/a';
  if (value < 90) return Math.round(value) + ' s';
  return (value / 60).toFixed(1) + ' min';
}

function insightsFilter() { return getTabFilter('insights'); }

async function insightsGet(path) {
  return api('GET', '/api/analytics/' + path, null, false, insightsFilter());
}

async function loadInsights() {
  renderAllianceFilterSelect('insightsAlliance', 'insights', loadInsights);
  renderSlotAlliances();
  byId('insightsDays').onchange = loadInsights;
  byId('trendWindow').onchange = loadTrends;
  byId('trendBy').onchange = loadTrends;
  byId('slotForm').onsubmit = (event) => { event.preventDefault(); findSlots(); };
  byId('insightsStatus').textContent = 'Loading...';
  const days = byId('insightsDays').value;
  try {
    [INSIGHTS.heat, INSIGHTS.overlaps, INSIGHTS.coverage] = await Promise.all([
      insightsGet('schedule-heatmap?days=' + days),
      insightsGet('schedule-overlaps?days=' + days),
      insightsGet('redemption-coverage'),
    ]);
    await loadTrends();
    renderHeatmap();
    renderOverlaps();
    renderCoverage();
    byId('insightsStatus').textContent = '';
  } catch (e) {
    byId('insightsStatus').textContent = 'Could not load insights: ' + e.message;
    toast(e.message, true);
  }
}

async function loadTrends() {
  try {
    INSIGHTS.trends = await insightsGet(`delivery-trends?days=${byId('trendWindow').value}&by=${byId('trendBy').value}`);
    renderTrends();
  } catch (e) {
    byId('trendSummary').textContent = 'Could not load delivery trends: ' + e.message;
    byId('trendBody').innerHTML = '';
  }
}

// ── Heatmap ──────────────────────────────────────────────────
function renderHeatmap() {
  const data = INSIGHTS.heat;
  const head = ['<tr><th scope="col" class="heat__corner"><span class="sr-only">Weekday</span></th>'];
  for (let h = 0; h < 24; h += 1) head.push(`<th scope="col" class="heat__hour">${String(h).padStart(2, '0')}</th>`);
  byId('heatHead').innerHTML = head.join('') + '</tr>';
  const max = data.busiest ? data.busiest.count : 0;
  const rows = data.grid.map((cells, day) => {
    const tds = cells.map((count, hour) => {
      const names = (data.events[`${day},${hour}`] || []).map((e) => e.name);
      const title = count ? `${WEEKDAYS[day]} ${insightsHour(hour)} UTC: ${names.join(', ')}${count > names.length ? ', ...' : ''}` : '';
      return `<td class="heat__cell heat--${heatLevel(count, max)}"${title ? ` title="${escapeHtml(title)}"` : ''}>${count}</td>`;
    }).join('');
    return `<tr><th scope="row" class="heat__day">${WEEKDAYS_SHORT[day]}</th>${tds}</tr>`;
  });
  byId('heatBody').innerHTML = rows.join('');
  const b = data.busiest;
  byId('heatSummary').textContent = b
    ? `${insightsPlural(data.total, 'event')} in the next ${data.days} days. Busiest: ${WEEKDAYS[b.weekday]} ${insightsHour(b.hour)} UTC, ${insightsPlural(b.count, 'event')}.`
    : `No events in the next ${data.days} days.`;
}

// ── Overlaps ─────────────────────────────────────────────────
function overlapRowHtml(o) {
  const side = (e) => `<strong>${escapeHtml(e.name)}</strong><div class="samaya-muted">${escapeHtml(e.alliances.join(', '))}</div>`;
  const channels = o.shared_channels.length ? pfLabel('Same channel', 'pf-m-orange') : '<span class="samaya-muted">No</span>';
  return `<tr class="pf-v6-c-table__tr">
    <td class="pf-v6-c-table__td" data-label="Event">${side(o.first)}</td>
    <td class="pf-v6-c-table__td" data-label="Overlaps with">${side(o.second)}</td>
    <td class="pf-v6-c-table__td" data-label="When (UTC)">${escapeHtml(insightsWhen(o.start))}</td>
    <td class="pf-v6-c-table__td" data-label="Minutes">${o.minutes}</td>
    <td class="pf-v6-c-table__td" data-label="Same channel">${channels}</td>
  </tr>`;
}

function renderOverlaps() {
  const data = INSIGHTS.overlaps;
  byId('overlapSummary').textContent = data.count
    ? `${insightsPlural(data.count, 'overlap')} in the next ${data.days} days.`
    : `No overlaps in the next ${data.days} days.`;
  byId('overlapBody').innerHTML = data.count ? data.overlaps.map(overlapRowHtml).join('') : emptyRow(5, 'Nothing overlaps.');
}

// ── Free slots ───────────────────────────────────────────────
function renderSlotAlliances() {
  const box = byId('slotAlliances');
  const legend = box.querySelector('legend');
  const checked = new Set(Array.from(box.querySelectorAll('input:checked')).map((i) => i.value));
  const items = TENANTS.map((t) => `<label class="check"><input type="checkbox" value="${escapeHtml(t.slug)}"${checked.has(t.slug) ? ' checked' : ''}> ${escapeHtml(t.name)}</label>`);
  box.innerHTML = '';
  box.appendChild(legend);
  box.insertAdjacentHTML('beforeend', items.join(''));
}

async function findSlots() {
  const params = new URLSearchParams({
    duration: byId('slotDuration').value, days: byId('slotDays').value,
    from_hour: byId('slotFrom').value, to_hour: byId('slotTo').value,
  });
  const picked = Array.from(byId('slotAlliances').querySelectorAll('input:checked')).map((i) => i.value);
  if (picked.length) params.set('alliances', picked.join(','));
  byId('slotSummary').textContent = 'Searching...';
  try {
    INSIGHTS.slots = await insightsGet('free-slots?' + params.toString());
    renderSlots();
  } catch (e) {
    INSIGHTS.slots = null;
    byId('slotList').innerHTML = '';
    byId('slotSummary').textContent = 'Could not search: ' + e.message;
  }
}

function renderSlots() {
  const data = INSIGHTS.slots;
  const slots = data.slots;
  byId('slotSummary').textContent = slots.length
    ? `${insightsPlural(slots.length, 'slot')} of ${data.duration} minutes, furthest from other events first.`
    : 'No free slot fits those limits. Try a shorter event, more days or wider hours.';
  byId('slotList').innerHTML = slots.map((s) =>
    `<li class="slot-list__item"><strong>${escapeHtml(insightsWhen(s.start))}</strong> to ${escapeHtml(insightsClock(s.end))} UTC
      <span class="samaya-muted">${s.clearance_minutes >= 1440 ? 'nothing else nearby' : s.clearance_minutes + ' min from the nearest event'}</span></li>`
  ).join('');
}

// ── Delivery trends ──────────────────────────────────────────
function trendRowHtml(label, r, labelName) {
  return `<tr class="pf-v6-c-table__tr">
    <td class="pf-v6-c-table__td" data-label="${labelName}">${escapeHtml(label)}</td>
    <td class="pf-v6-c-table__td" data-label="Due">${r.due}</td>
    <td class="pf-v6-c-table__td" data-label="Posted">${r.posted}</td>
    <td class="pf-v6-c-table__td" data-label="Failed">${r.errors ? pfLabel(String(r.errors), 'pf-m-red') : '0'}</td>
    <td class="pf-v6-c-table__td" data-label="Median late">${escapeHtml(insightsSeconds(r.median_lateness_seconds))}</td>
    <td class="pf-v6-c-table__td" data-label="95th percentile">${escapeHtml(insightsSeconds(r.p95_lateness_seconds))}</td>
  </tr>`;
}

function renderTrends() {
  const data = INSIGHTS.trends;
  const byDestination = data.by === 'destination';
  const first = byDestination ? 'Destination' : 'Day';
  byId('trendHead').innerHTML = `<tr class="pf-v6-c-table__tr">${[first, 'Due', 'Posted', 'Failed', 'Median late', '95th percentile'].map((h) => `<th class="pf-v6-c-table__th" scope="col">${h}</th>`).join('')}</tr>`;
  const rows = byDestination
    ? data.destinations.map((r) => trendRowHtml(r.destination, r, first))
    : data.days.slice().reverse().map((r) => trendRowHtml(r.date, r, first));
  const due = (byDestination ? data.destinations : data.days).reduce((n, r) => n + r.due, 0);
  const errors = (byDestination ? data.destinations : data.days).reduce((n, r) => n + r.errors, 0);
  byId('trendSummary').textContent = due
    ? `${insightsPlural(due, 'reminder')} due in the last ${data.window_days} days, ${errors} failed. Lateness is time from due to posted.`
    : `No reminders came due in the last ${data.window_days} days.`;
  byId('trendBody').innerHTML = rows.length ? rows.join('') : emptyRow(6, 'No reminders in this window.');
}

// ── Gift code coverage ───────────────────────────────────────
function renderCoverage() {
  const runs = INSIGHTS.coverage.runs;
  if (!runs.length) {
    byId('coverageBody').innerHTML = '<p class="samaya-muted">No gift code runs yet.</p>';
    return;
  }
  byId('coverageBody').innerHTML = runs.map((run) => {
    const rows = run.alliances.map((a) => `<tr class="pf-v6-c-table__tr">
      <td class="pf-v6-c-table__td" data-label="Alliance">${escapeHtml(a.alliance)}</td>
      <td class="pf-v6-c-table__td" data-label="Players">${a.players}</td>
      <td class="pf-v6-c-table__td" data-label="Redeemed">${a.redeemed}</td>
      <td class="pf-v6-c-table__td" data-label="Already had it">${a.already}</td>
      <td class="pf-v6-c-table__td" data-label="Wrong kingdom">${a.wrong_kingdom}</td>
      <td class="pf-v6-c-table__td" data-label="Other">${a.other}</td>
      <td class="pf-v6-c-table__td" data-label="Coverage"><strong>${a.coverage_percent}%</strong></td>
    </tr>`).join('');
    return `<h4 class="panel__subtitle">Code <code>${escapeHtml(run.code)}</code></h4>
      <div class="table-wrap"><table class="pf-v6-c-table pf-m-grid-md responsive-table">
        <caption class="sr-only">Coverage for code ${escapeHtml(run.code)}</caption>
        <thead><tr class="pf-v6-c-table__tr">${['Alliance', 'Players', 'Redeemed', 'Already had it', 'Wrong kingdom', 'Other', 'Coverage'].map((h) => `<th class="pf-v6-c-table__th" scope="col">${h}</th>`).join('')}</tr></thead>
        <tbody>${rows || emptyRow(7, 'No results for your alliances.')}</tbody>
      </table></div>`;
  }).join('');
}

VIEW_LOADERS.insights = loadInsights;
