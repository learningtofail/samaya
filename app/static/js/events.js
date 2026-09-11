async function loadEvents() {
  try {
    const events = await api('GET', '/api/events');
    const tbody = document.getElementById('eventsBody');
    const tbodyLead = document.getElementById('eventsBodyLeadership');
    if (!events.length) {
      tbody.innerHTML = '<tr><td colspan="8" style="color:var(--muted);padding:20px">No events defined yet. Click &quot;+ Add Event&quot; to get started.</td></tr>';
      tbodyLead.innerHTML = '<tr><td colspan="8" style="color:var(--muted);padding:20px">No leadership events defined yet.</td></tr>';
      return;
    }
    function intervalLabel(i) {
      if (i===1)  return 'Daily';
      if (i===2)  return 'Every 2 days';
      if (i===7)  return 'Weekly';
      if (i===14) return 'Biweekly';
      if (i===28) return 'Every 4 weeks';
      return 'Every ' + i + ' days';
    }
    function buildRow(e) {
      var allyColor = ALLIANCE_COLORS[e.alliance] || ALLIANCE_COLORS.Server;
      var badgeCls  = e.active ? 'badge-ok' : 'badge-pending';
      var badgeTxt  = e.active ? 'Active' : 'Inactive';
      var btnCls    = e.active ? 'btn-danger' : 'btn-ghost';
      var btnTxt    = e.active ? 'Deactivate' : 'Activate';
      var btnTitle  = e.active
        ? 'Remove this event from schedule generation and Discord posting. The event remains in the database.'
        : 'Re-enable this event for schedule generation and Discord posting.';
      // escapeHtml (not a bare quote-replace) so the embedded JSON can't
      // break out of the onclick="..." attribute via <, >, or & either.
      var editData  = escapeHtml(JSON.stringify(e));
      var deleteBtn = '';
      if (!e.active) {
        var safeName = escapeHtml(e.name);
        deleteBtn = '<button class="btn btn-danger btn-sm" '
          + 'title="Permanently delete this event. PostLog entries are preserved." '
          + 'data-id="' + e.id + '" data-name="' + safeName + '" '
          + 'onclick="permanentDelete(this)">Delete</button>';
      }
      return '<tr style="border-left:3px solid ' + allyColor + '">'
        + '<td><span class="cat-dot" style="background:' + allyColor + '"></span>' + escapeHtml(e.name) + '</td>'
        + '<td>' + escapeHtml(e.alliance || 'Server') + '</td>'
        + '<td>' + intervalLabel(e.interval_days) + '</td>'
        + '<td>' + e.start_time_utc + ' UTC</td>'
        + '<td>' + e.duration_hours + 'h</td>'
        + '<td>' + e.anchor_date + '</td>'
        + '<td><span class="badge ' + badgeCls + '">' + badgeTxt + '</span></td>'
        + '<td>'
        + '<button class="btn btn-ghost btn-sm" title="Edit this event definition." onclick="openEventModal(' + editData + ')">Edit</button> '
        + '<button class="btn btn-ghost btn-sm" title="Create a new event pre-filled with these settings." onclick="duplicateEvent(' + editData + ')">Duplicate</button> '
        + '<button class="btn ' + btnCls + ' btn-sm" title="' + btnTitle + '" onclick="toggleActive(' + e.id + ',' + e.active + ')">' + btnTxt + '</button> '
        + deleteBtn
        + '</td>'
        + '</tr>';
    }
    const community = events.filter(e => !e.leadership_only);
    const leadership = events.filter(e => e.leadership_only);
    tbody.innerHTML = community.length
      ? community.map(buildRow).join('')
      : '<tr><td colspan="8" style="color:var(--muted);padding:20px">No alliance events defined yet. Click &quot;+ Add Event&quot; to get started.</td></tr>';
    tbodyLead.innerHTML = leadership.length
      ? leadership.map(buildRow).join('')
      : '<tr><td colspan="8" style="color:var(--muted);padding:20px">No leadership events defined yet.</td></tr>';
  } catch(e) { toast(e.message, true); }
}

function openEventModal(event, presetLeadership) {
  document.getElementById('modalTitle').textContent = event ? 'Edit Event' : (presetLeadership ? 'Add Leadership Event' : 'Add Event');
  document.getElementById('modalEventId').value = event && event.id ? event.id : '';
  document.getElementById('mName').value        = event?.name || '';
  document.getElementById('mAlliance').value    = event?.alliance || 'Server';
  document.getElementById('mInterval').value    = event?.interval_days || '';
  document.getElementById('mStartTime').value   = event?.start_time_utc || '';
  document.getElementById('mDuration').value    = event?.duration_hours || '';
  document.getElementById('mAnchor').value      = event?.anchor_date || '';
  document.getElementById('mDescription').value       = event?.description || '';
  document.getElementById('mNotifMinutes').value      = event?.notify_minutes_before || '';
  document.getElementById('mLeadershipOnly').checked  = event ? !!event.leadership_only : !!presetLeadership;
  toggleLeadershipNote();
  document.getElementById('mDetectedTz').textContent = Intl.DateTimeFormat().resolvedOptions().timeZone;
  document.getElementById('mTimezone').value = getDisplayTz();
  document.getElementById('previewResult').textContent = '';
  populateDiscordFields(event);
  document.getElementById('eventModal').classList.add('open');
}

function duplicateEvent(event) {
  const copy = Object.assign({}, event);
  delete copy.id;
  copy.name = event.name + ' (Copy)';
  openEventModal(copy);
  document.getElementById('modalTitle').textContent = 'Duplicate Event';
}

function toggleLeadershipNote() {
  const checked = document.getElementById('mLeadershipOnly').checked;
  document.getElementById('leadershipNote').style.display = checked ? 'block' : 'none';
}

// ── Display timezone (persists across the admin UI via localStorage) ──

function getDisplayTz() {
  return localStorage.getItem('samaya_display_tz') || 'UTC';
}

function applyDisplayTzChange() {
  localStorage.setItem('samaya_display_tz', document.getElementById('mTimezone').value);
  if (document.getElementById('v-dashboard').classList.contains('active')) loadDashboard();
  if (document.getElementById('v-schedule').classList.contains('active')) renderSchedule();
  if (document.getElementById('v-gantt').classList.contains('active')) loadGantt();
  if (document.getElementById('v-postlog').classList.contains('active')) loadPostLog();
}

function toUtcDate(isoStr) {
  const hasTz = /Z$|[+-]\d{2}:\d{2}$/.test(isoStr);
  return new Date(hasTz ? isoStr : isoStr + 'Z');
}

function fmtTime(utcIso) {
  const d = toUtcDate(utcIso);
  const tz = getDisplayTz();
  const t = new Intl.DateTimeFormat('en-GB', { hour:'2-digit', minute:'2-digit', hour12:false, timeZone: tz }).format(d);
  return tz === 'UTC' ? t + ' UTC' : t + ' ' + tz;
}

function fmtTimeShort(utcIso) {
  const d = toUtcDate(utcIso);
  return new Intl.DateTimeFormat('en-GB', { hour:'2-digit', minute:'2-digit', hour12:false, timeZone: getDisplayTz() }).format(d);
}

function fmtDateTime(utcIso) {
  const d = toUtcDate(utcIso);
  const tz = getDisplayTz();
  const parts = new Intl.DateTimeFormat('en-GB', {
    year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hour12:false, timeZone: tz
  }).formatToParts(d).reduce((a,p) => (a[p.type]=p.value, a), {});
  const s = `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
  return tz === 'UTC' ? s + ' UTC' : s + ' ' + tz;
}

// ── Discord channel/role dropdowns ─────────────────────────────

async function populateDiscordFields(event) {
  const chanSelect = document.getElementById('mChannel');
  const chanFallback = document.getElementById('mChannelFallback');
  const notifChanSelect = document.getElementById('mNotifChannel');
  const notifChanFallback = document.getElementById('mNotifChannelFallback');
  const roleSelect = document.getElementById('mNotifRole');
  const roleFallback = document.getElementById('mNotifRoleFallback');

  [chanSelect, notifChanSelect, roleSelect].forEach(s => {
    s.innerHTML = '<option>Loading…</option>';
    s.disabled = true;
    s.style.display = '';
  });
  [chanFallback, notifChanFallback, roleFallback].forEach(f => f.style.display = 'none');

  try {
    const [channels, roles] = await Promise.all([
      api('GET', '/api/discord/channels'),
      api('GET', '/api/discord/roles'),
    ]);

    fillSelect(chanSelect, channels.map(c => ({ value: c.name, label: '#' + c.name })), event?.discord_channel);
    fillSelect(notifChanSelect, channels.map(c => ({ value: c.id, label: '#' + c.name })), event?.notification_channel_id);
    fillSelect(roleSelect, roles.map(r => ({ value: r.id, label: '@' + r.name })), event?.notification_role_id);

    [chanSelect, notifChanSelect, roleSelect].forEach(s => s.disabled = false);
  } catch (e) {
    [chanSelect, notifChanSelect, roleSelect].forEach(s => s.style.display = 'none');
    chanFallback.style.display = '';
    notifChanFallback.style.display = '';
    roleFallback.style.display = '';
    chanFallback.value = event?.discord_channel || '';
    notifChanFallback.value = event?.notification_channel_id || '';
    roleFallback.value = event?.notification_role_id || '';
    toast('Could not load Discord channels/roles — enter values manually', true);
  }
}

function fillSelect(selectEl, options, currentValue) {
  let matched = false;
  let optionsHtml = options.map(o => {
    const isSelected = currentValue != null && String(o.value) === String(currentValue);
    if (isSelected) matched = true;
    return `<option value="${o.value}" ${isSelected ? 'selected' : ''}>${o.label}</option>`;
  }).join('');
  if (currentValue && !matched) {
    optionsHtml = `<option value="${currentValue}" selected>${currentValue} (not found — kept as-is)</option>` + optionsHtml;
  }
  if (!currentValue) {
    optionsHtml = `<option value="">— none —</option>` + optionsHtml;
  }
  selectEl.innerHTML = optionsHtml;
}

function fieldValue(selectId, fallbackId) {
  const sel = document.getElementById(selectId);
  const fb = document.getElementById(fallbackId);
  return sel.style.display !== 'none' ? sel.value : fb.value;
}

function closeModal() {
  document.getElementById('eventModal').classList.remove('open');
}

async function saveEvent() {
  const id = document.getElementById('modalEventId').value;
  const startTime = document.getElementById('mStartTime').value.trim();
  if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(startTime)) {
    toast('Start time must be 24-hour HH:MM (e.g. 19:00)', true);
    return;
  }
  const payload = {
    name:            document.getElementById('mName').value,
    alliance:        document.getElementById('mAlliance').value,
    leadership_only: document.getElementById('mLeadershipOnly').checked,
    interval_days:   parseInt(document.getElementById('mInterval').value),
    start_time_utc:  startTime,
    duration_hours:  parseFloat(document.getElementById('mDuration').value),
    anchor_date:     document.getElementById('mAnchor').value,
    discord_channel:         fieldValue('mChannel', 'mChannelFallback'),
    notification_channel_id: fieldValue('mNotifChannel', 'mNotifChannelFallback'),
    notification_role_id:    fieldValue('mNotifRole', 'mNotifRoleFallback'),
    notify_minutes_before:   document.getElementById('mNotifMinutes').value
                             ? parseInt(document.getElementById('mNotifMinutes').value)
                             : null,
    description:     document.getElementById('mDescription').value,
  };
  try {
    if (id) {
      await api('PATCH', `/api/events/${id}`, payload);
      toast('Event updated');
    } else {
      await api('POST', '/api/events', payload);
      toast('Event created');
    }
    closeModal();
    loadEvents();
  } catch(e) { toast(e.message, true); }
}

async function toggleActive(id, current) {
  try {
    await api('PATCH', `/api/events/${id}`, { active: !current });
    toast(current ? 'Event deactivated' : 'Event activated');
    loadEvents();
  } catch(e) { toast(e.message, true); }
}

async function previewOccurrences() {
  const payload = {
    name:           document.getElementById('mName').value || 'preview',
    interval_days:  parseInt(document.getElementById('mInterval').value),
    start_time_utc: document.getElementById('mStartTime').value || '00:00',
    duration_hours: parseFloat(document.getElementById('mDuration').value) || 1,
    anchor_date:    document.getElementById('mAnchor').value,
    discord_channel:'', description:'',
  };
  if (!payload.anchor_date || !payload.interval_days) {
    document.getElementById('previewResult').textContent = 'Enter interval and anchor date first.';
    return;
  }
  try {
    const dates = await api('POST', '/api/scheduler/preview', payload);
    document.getElementById('previewResult').textContent =
      dates.map(d => `${d.date} (${d.day})`).join('  ·  ');
  } catch(e) {
    document.getElementById('previewResult').textContent = e.message;
  }
}

// ── Schedule ────────────────────────────────────────────────

