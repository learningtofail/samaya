// Events view (#v-events): the event definitions table, the create/edit
// modal, and row actions (duplicate, activate/deactivate, permanent delete).
// Depends on common.js (api, toast, escapeHtml, TENANT_COLORS).

// Cached so filterEventsTable() (spec §29) can re-render from a text
// filter without a round trip — same pattern as ANNOUNCEMENTS/ANNOUNCEMENT_TEMPLATES.
let EVENTS_CACHE = [];

function filterEventsTable() {
  renderEventsTable(EVENTS_CACHE);
}

async function loadEvents() {
  renderAllianceFilterSelect('eventsFilter', 'events', loadEvents);
  try {
    const events = await api('GET', '/api/events', null, false, getTabFilter('events'));
    EVENTS_CACHE = events;
    renderEventsTable(events);
  } catch(e) { toast(e.message, true); }
}

// spec §32 — bulk export/import, alliance-scope events only (see the
// backend's _BULK_EVENT_COLUMNS comment for why kingdom-wide/targets are
// excluded). Export follows the same fetch-blob-download pattern as
// postlog.js's exportPostLogCsv(); import posts the chosen file as
// multipart form data, outside the api() helper since api() always sends
// JSON.
async function exportEventsCsv() {
  const slug = getTabFilter('events');
  if (slug === COMBINED_SLUG) {
    toast('Pick one alliance in the filter above before exporting — CSV export is per-alliance', true);
    return;
  }
  try {
    const res = await fetch('/admin/api/events/export.csv', {
      headers: { 'X-Tenant-Slug': slug },
      credentials: 'same-origin',
    });
    if (res.status === 401) { window.location.href = '/auth/login'; return; }
    if (!res.ok) throw new Error('Export failed: ' + res.statusText);
    const blob = await res.blob();
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href = url;
    a.download = 'Events_Export.csv';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    toast(e.message, true);
  }
}

async function importEventsCsv(file) {
  if (!file) return;
  const slug = getTabFilter('events');
  if (slug === COMBINED_SLUG) {
    toast('Pick one alliance in the filter above before importing — CSV import is per-alliance', true);
    return;
  }
  const formData = new FormData();
  formData.append('file', file);
  try {
    const res = await fetch('/admin/api/events/import.csv', {
      method: 'POST',
      headers: { 'X-Tenant-Slug': slug },
      credentials: 'same-origin',
      body: formData,
    });
    if (res.status === 401) { window.location.href = '/auth/login'; return; }
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || res.statusText);
    if (body.errors && body.errors.length) {
      toast(`Imported ${body.created} event(s), ${body.errors.length} row(s) failed — see console for details`, body.created === 0);
      console.warn('Event import errors:', body.errors);
    } else {
      toast(`Imported ${body.created} event(s)`);
    }
    loadEvents();
  } catch (e) {
    toast(e.message, true);
  } finally {
    document.getElementById('eventsImportFile').value = '';
  }
}

function renderEventsTable(allEvents) {
    const filterText = (document.getElementById('eventsFilterInput')?.value || '').trim().toLowerCase();
    const events = filterText
      ? allEvents.filter(e =>
          e.name.toLowerCase().includes(filterText) ||
          tenantName(e.owning_tenant_id).toLowerCase().includes(filterText))
      : allEvents;
    const tbody = document.getElementById('eventsBody');
    const tbodyLead = document.getElementById('eventsBodyLeadership');
    if (!events.length) {
      tbody.innerHTML = '<tr class="pf-v6-c-table__tr"><td class="pf-v6-c-table__td" colspan="7" style="color:var(--muted);padding:20px">No events defined yet. Click &quot;+ Add Event&quot; to get started.</td></tr>';
      tbodyLead.innerHTML = '<tr class="pf-v6-c-table__tr"><td class="pf-v6-c-table__td" colspan="7" style="color:var(--muted);padding:20px">No leadership events defined yet.</td></tr>';
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
    // Spec §49 — the Notification Targets column: the event's own primary
    // channel (already a plain name, not an ID — see populateDiscordFields),
    // its pre-event ping channel/role (real Discord snowflake IDs, shown
    // as the bare ID until enhanceNotificationTargetLabels resolves them),
    // and a summary of any extra EventTarget rows (spec §20).
    function eventNotifTargetsHtml(e) {
      const ownerSlug = tenantSlugFor(e.owning_tenant_id);
      const parts = [];
      if (e.discord_channel) {
        parts.push('<span class="pf-v6-u-font-size-sm">#' + escapeHtml(e.discord_channel) + '</span>');
      }
      if (e.notification_channel_id) {
        parts.push(
          '<span class="pf-v6-u-font-size-sm" style="color:var(--muted)" title="Pre-event ping destination">🔔 '
          + '<span data-notif-channel="' + escapeHtml(ownerSlug) + ':' + escapeHtml(e.notification_channel_id) + '">#' + escapeHtml(e.notification_channel_id) + '</span>'
          + (e.notification_role_id ? ' <span data-notif-role="' + escapeHtml(ownerSlug) + ':' + escapeHtml(e.notification_role_id) + '">@' + escapeHtml(e.notification_role_id) + '</span>' : '')
          + '</span>'
        );
      }
      if (e.targets && e.targets.length) {
        parts.push(e.targets.map(t => {
          const slug = tenantSlugFor(t.tenant_id);
          return '<span class="pf-v6-u-font-size-sm" style="color:var(--muted)">' + escapeHtml(tenantName(t.tenant_id)) + ': '
            + '<span data-notif-channel="' + escapeHtml(slug) + ':' + escapeHtml(t.notification_channel_id) + '">#' + escapeHtml(t.notification_channel_id) + '</span></span>';
        }).join('<br>'));
      }
      return parts.length ? parts.join('<br>') : '<span style="color:var(--muted)">—</span>';
    }
    function buildRow(e) {
      var allyColor = TENANT_COLORS[e.owning_tenant_id] || '#475569';
      // Spec §49 — the Alliance column names the actual owning alliance
      // (or, for a kingdom-wide event, says so) instead of the previous
      // bug where it just echoed the literal word "Alliance" for every
      // non-kingdom-wide row.
      var scopeLabel = e.scope === 'kingdom-wide'
        ? '🌐 Kingdom-wide <span style="color:var(--muted);font-size:var(--fs-sm)">(via ' + escapeHtml(tenantName(e.owning_tenant_id)) + '’s Kingdom)</span>'
        : '<span class="cat-dot" style="background:' + allyColor + '"></span>' + escapeHtml(tenantName(e.owning_tenant_id));
      var statusLabel = pfLabel(e.active ? 'Active' : 'Inactive', e.active ? 'pf-m-green' : 'pf-m-gray');
      var btnCls    = e.active ? 'pf-m-danger' : 'pf-m-secondary';
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
        deleteBtn = '<button class="pf-v6-c-button pf-m-danger pf-m-small" '
          + 'title="Permanently delete this event. PostLog entries are preserved." '
          + 'data-id="' + e.id + '" data-name="' + safeName + '" '
          + 'onclick="permanentDelete(this)">Delete</button>';
      }
      // Spec §49 — the Alliance column (scopeLabel, above) now carries the
      // owning alliance identity on its own, so the name column no longer
      // needs a duplicate tenant tag appended to it.
      return '<tr class="pf-v6-c-table__tr samaya-row-clickable" style="border-left:3px solid ' + allyColor + '" onclick="handleRowPreviewClick(event,\'eventdef\',' + editData + ')" title="Click to preview how this looks on Discord">'
        + '<td class="pf-v6-c-table__td">' + escapeHtml(e.name) + '</td>'
        + '<td class="pf-v6-c-table__td">' + scopeLabel + '</td>'
        + '<td class="pf-v6-c-table__td">' + intervalLabel(e.interval_days) + '</td>'
        + '<td class="pf-v6-c-table__td">' + e.start_time_utc + ' UTC</td>'
        + '<td class="pf-v6-c-table__td">' + statusLabel + '</td>'
        + '<td class="pf-v6-c-table__td">' + eventNotifTargetsHtml(e) + '</td>'
        + '<td class="pf-v6-c-table__td">'
        + '<button class="pf-v6-c-button pf-m-secondary pf-m-small" title="Edit this event definition." onclick="openEventModal(' + editData + ')">Edit</button> '
        + '<button class="pf-v6-c-button pf-m-secondary pf-m-small" title="Create a new event pre-filled with these settings." onclick="duplicateEvent(' + editData + ')">Duplicate</button> '
        + '<button class="pf-v6-c-button ' + btnCls + ' pf-m-small" title="' + btnTitle + '" onclick="toggleActive(' + e.id + ',' + e.active + ')">' + btnTxt + '</button> '
        + deleteBtn
        + '</td>'
        + '</tr>';
    }
    const community = events.filter(e => !e.leadership_only);
    const leadership = events.filter(e => e.leadership_only);
    tbody.innerHTML = community.length
      ? community.map(buildRow).join('')
      : '<tr class="pf-v6-c-table__tr"><td class="pf-v6-c-table__td" colspan="7" style="color:var(--muted);padding:20px">No events defined yet. Click &quot;+ Add Event&quot; to get started.</td></tr>';
    tbodyLead.innerHTML = leadership.length
      ? leadership.map(buildRow).join('')
      : '<tr class="pf-v6-c-table__tr"><td class="pf-v6-c-table__td" colspan="7" style="color:var(--muted);padding:20px">No leadership events defined yet.</td></tr>';
    enhanceNotificationTargetLabels();
}

function tenantName(id) {
  const t = TENANTS.find(t => t.id === id);
  return t ? t.name : ('#' + id);
}

function openEventModal(event) {
  document.getElementById('modalTitle').textContent = event ? 'Edit Event' : 'Add Event';
  document.getElementById('modalEventId').value = event && event.id ? event.id : '';

  // Spec §49 — one combined "Owning Alliance" selector (alliance or that
  // alliance's kingdom-wide option) for both create and edit; editing an
  // existing event no longer locks its owning tenant/scope — picking a
  // different option here reassigns it (see saveEvent()).
  const ownerSelect = document.getElementById('mOwningTenant');
  const resolvedTenantSlug = event ? tenantSlugFor(event.owning_tenant_id) : TENANTS[0]?.slug;
  renderOwningTenantScopeSelect('mOwningTenant', resolvedTenantSlug, event?.scope || 'alliance');
  ownerSelect.onchange = () => populateDiscordFields(event, parseOwningTenantScopeValue(ownerSelect.value).slug);

  document.getElementById('mName').value        = event?.name || '';
  document.getElementById('mInterval').value    = event?.interval_days || '';
  document.getElementById('mStartTime').value   = event?.start_time_utc || '';
  document.getElementById('mDuration').value    = event?.duration_hours || '';
  document.getElementById('mAnchor').value      = event?.anchor_date || '';
  document.getElementById('mDescription').value       = event?.description || '';
  document.getElementById('mNotifMinutes').value      = event?.notify_minutes_before || '';
  document.getElementById('mLeadershipOnly').checked  = !!event?.leadership_only;
  setCoverImagePreview(event?.cover_image_data || '');
  document.getElementById('mCoverImageFile').value = '';
  toggleLeadershipNote();
  document.getElementById('previewResult').textContent = '';
  populateDiscordFields(event, resolvedTenantSlug);

  document.getElementById('mTargetsList').innerHTML = '';
  (event?.targets || []).forEach(t => addEventTargetRow(tenantSlugFor(t.tenant_id), t.notification_channel_id, t.notification_role_id));

  document.getElementById('eventModal').classList.add('open');
  focusModal(document.getElementById('eventModal'));
  refreshPreviewTenantOptions('mPreviewTenant', null);
  renderEventDescriptionPreview();
}

// Reverse of the tenant_id the API stores a target as — the target-row
// UI keys off slug (same as TENANTS/the tenant picker everywhere else),
// same reasoning as tenantName() just above.
function tenantSlugFor(tenantId) {
  const t = TENANTS.find(t => t.id === tenantId);
  return t ? t.slug : '';
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
  document.getElementById('leadershipNote').classList.toggle('hidden', !checked);
}

// ── Time formatting ─────────────────────────────────────────
// getDisplayTz()/setDisplayTz() and the header time zone picker live in
// common.js (spec §15) — the control is global, not Events-specific,
// so it moved out of this file's old buried-in-the-modal version.

function toUtcDate(isoStr) {
  const hasTz = /Z$|[+-]\d{2}:\d{2}$/.test(isoStr);
  return new Date(hasTz ? isoStr : isoStr + 'Z');
}

// The building block for "show both" (spec §15.2): UTC stays in its
// existing 24-hour HH:MM form (the value everything else in the app
// already keys off); the local half uses 12-hour + a short zone
// abbreviation (e.g. EDT, not the raw IANA name) so the two read as
// visually distinct values rather than two numbers that only differ by
// an hour count. Collapses to a single value when the display zone IS
// UTC — "19:00 UTC · 19:00 UTC" would be noise, not information.
function dualTimeString(utcIso) {
  const d = toUtcDate(utcIso);
  const utcPart = new Intl.DateTimeFormat('en-GB', { hour:'2-digit', minute:'2-digit', hour12:false, timeZone:'UTC' }).format(d) + ' UTC';
  const tz = getDisplayTz();
  if (tz === 'UTC') return utcPart;
  const localPart = new Intl.DateTimeFormat('en-US', { hour:'numeric', minute:'2-digit', hour12:true, timeZoneName:'short', timeZone: tz }).format(d);
  return utcPart + ' · ' + localPart;
}

function fmtTime(utcIso) {
  return dualTimeString(utcIso);
}

function fmtTimeShort(utcIso) {
  const d = toUtcDate(utcIso);
  return new Intl.DateTimeFormat('en-GB', { hour:'2-digit', minute:'2-digit', hour12:false, timeZone: getDisplayTz() }).format(d);
}

// Post Log's "Posted At" column and Announcements' "Scheduled for"
// column (spec §13.4/§15.2). Dates can differ across the UTC/local
// split, not just times — an event at 01:00 UTC on the 28th is 9:00 PM
// on the 27th in an American zone — so each half is computed against
// its OWN correct calendar date rather than one date reused for both.
// The local date prefix is shown only when it actually differs from the
// UTC date, to avoid clutter in the (overwhelmingly common) case where
// it doesn't.
function fmtDateTime(utcIso) {
  const d = toUtcDate(utcIso);

  const utcParts = new Intl.DateTimeFormat('en-CA', {
    year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hour12:false, timeZone:'UTC'
  }).formatToParts(d).reduce((a,p) => (a[p.type]=p.value, a), {});
  const utcDateKey = `${utcParts.year}-${utcParts.month}-${utcParts.day}`;
  const utcStr = `${utcDateKey} ${utcParts.hour}:${utcParts.minute} UTC`;

  const tz = getDisplayTz();
  if (tz === 'UTC') return utcStr;

  // en-CA reliably formats as YYYY-MM-DD regardless of locale display
  // conventions — used here purely as a stable comparison key, not shown.
  const localKeyParts = new Intl.DateTimeFormat('en-CA', {
    year:'numeric', month:'2-digit', day:'2-digit', timeZone: tz
  }).formatToParts(d).reduce((a,p) => (a[p.type]=p.value, a), {});
  const localDateKey = `${localKeyParts.year}-${localKeyParts.month}-${localKeyParts.day}`;

  const localTimeStr = new Intl.DateTimeFormat('en-US', {
    hour:'numeric', minute:'2-digit', hour12:true, timeZoneName:'short', timeZone: tz
  }).format(d);

  if (localDateKey === utcDateKey) {
    return `${utcStr} · ${localTimeStr}`;
  }
  const localDateStr = new Intl.DateTimeFormat('en-US', { month:'short', day:'numeric', timeZone: tz }).format(d);
  return `${utcStr} · ${localDateStr}, ${localTimeStr}`;
}

// ── Discord channel/role dropdowns ─────────────────────────────

async function populateDiscordFields(event, tenantSlug) {
  const chanSelect = document.getElementById('mChannel');
  const chanFallback = document.getElementById('mChannelFallback');
  const notifChanSelect = document.getElementById('mNotifChannel');
  const notifChanFallback = document.getElementById('mNotifChannelFallback');
  const roleSelect = document.getElementById('mNotifRole');
  const roleFallback = document.getElementById('mNotifRoleFallback');

  [chanSelect, notifChanSelect, roleSelect].forEach(s => {
    s.innerHTML = '<option>Loading…</option>';
    s.disabled = true;
    s.classList.remove('hidden');
  });
  [chanFallback, notifChanFallback, roleFallback].forEach(f => f.classList.add('hidden'));

  try {
    const [channels, roles] = await Promise.all([
      api('GET', '/api/discord/channels', null, false, tenantSlug),
      api('GET', '/api/discord/roles', null, false, tenantSlug),
    ]);

    fillSelect(chanSelect, channels.map(c => ({ value: c.name, label: '#' + c.name })), event?.discord_channel);
    fillSelect(notifChanSelect, channels.map(c => ({ value: c.id, label: '#' + c.name })), event?.notification_channel_id);
    fillSelect(roleSelect, roles.map(r => ({ value: r.id, label: '@' + r.name })), event?.notification_role_id);

    [chanSelect, notifChanSelect, roleSelect].forEach(s => s.disabled = false);
  } catch (e) {
    [chanSelect, notifChanSelect, roleSelect].forEach(s => s.classList.add('hidden'));
    chanFallback.classList.remove('hidden');
    notifChanFallback.classList.remove('hidden');
    roleFallback.classList.remove('hidden');
    chanFallback.value = event?.discord_channel || '';
    notifChanFallback.value = event?.notification_channel_id || '';
    roleFallback.value = event?.notification_role_id || '';
    toast('Could not load Discord channels/roles — enter values manually', true);
  }
}

// ── Extra Notification Targets (spec §20) ───────────────────────
// One target row = one (tenant, notification channel, notification
// role) triple — an EXTRA destination beyond the owning tenant's own
// bare notification fields above and beyond kingdom-wide's automatic
// same-Kingdom fan-out (EventTenantNotification, set via the "ping
// settings" action on a kingdom-wide event from another alliance).
// Mirrors Announcements' addAnnouncementTargetRow (announcements.js)
// with one addition: a role dropdown, since an event's ping always
// carries a role mention where an announcement doesn't need one.
function addEventTargetRow(tenantSlug, channelId, roleId) {
  const list = document.getElementById('mTargetsList');
  const row = document.createElement('div');
  row.className = 'event-target-row';
  row.style = 'display:flex;gap:8px;align-items:center';
  const tenantOptions = TENANTS.map(t =>
    `<option value="${escapeHtml(t.slug)}" ${t.slug === tenantSlug ? 'selected' : ''}>${escapeHtml(t.name)}</option>`
  ).join('');
  row.innerHTML = `
    <select class="pf-v6-c-form-control target-tenant" style="flex:1">${tenantOptions}</select>
    <select class="pf-v6-c-form-control target-channel" style="flex:1"><option>Loading…</option></select>
    <input class="pf-v6-c-form-control target-channel-fallback" type="text" style="flex:1;display:none" placeholder="Channel ID">
    <select class="pf-v6-c-form-control target-role" style="flex:1"><option>Loading…</option></select>
    <input class="pf-v6-c-form-control target-role-fallback" type="text" style="flex:1;display:none" placeholder="Role ID (optional)">
    <button type="button" class="pf-v6-c-button pf-m-danger pf-m-small" onclick="this.closest('.event-target-row').remove()">Remove</button>
  `;
  list.appendChild(row);
  const select = row.querySelector('.target-tenant');
  select.addEventListener('change', () => populateEventTargetFields(row, select.value));
  // channelId is only passed for a real stored target (edit/duplicate) —
  // a fresh row has none, so default to that tenant's last-used channel
  // (spec §29), same convention as the announcement composer.
  const initialTenantSlug = tenantSlug || select.value;
  populateEventTargetFields(row, initialTenantSlug, channelId || getLastChannelForTenant(initialTenantSlug), roleId);
}

async function populateEventTargetFields(row, tenantSlug, channelId, roleId) {
  const chanSelect   = row.querySelector('.target-channel');
  const chanFallback = row.querySelector('.target-channel-fallback');
  const roleSelect   = row.querySelector('.target-role');
  const roleFallback = row.querySelector('.target-role-fallback');
  [chanSelect, roleSelect].forEach(s => { s.innerHTML = '<option>Loading…</option>'; s.disabled = true; s.style.display = ''; });
  [chanFallback, roleFallback].forEach(f => f.style.display = 'none');
  try {
    const [channels, roles] = await Promise.all([
      api('GET', '/api/discord/channels', null, false, tenantSlug),
      api('GET', '/api/discord/roles', null, false, tenantSlug),
    ]);
    fillSelect(chanSelect, channels.map(c => ({ value: c.id, label: '#' + c.name })), channelId);
    fillSelect(roleSelect, roles.map(r => ({ value: r.id, label: '@' + r.name })), roleId);
    chanSelect.disabled = false;
    roleSelect.disabled = false;
    chanSelect.addEventListener('change', () => setLastChannelForTenant(tenantSlug, chanSelect.value));
    if (chanSelect.value) setLastChannelForTenant(tenantSlug, chanSelect.value);
  } catch (e) {
    chanSelect.style.display = 'none';
    roleSelect.style.display = 'none';
    chanFallback.style.display = '';
    roleFallback.style.display = '';
    chanFallback.value = channelId || '';
    roleFallback.value = roleId || '';
    toast(`Could not load Discord channels/roles for ${tenantSlug} — enter values manually`, true);
  }
}

function eventTargetRowValue(row) {
  const chanSelect   = row.querySelector('.target-channel');
  const chanFallback = row.querySelector('.target-channel-fallback');
  const roleSelect   = row.querySelector('.target-role');
  const roleFallback = row.querySelector('.target-role-fallback');
  return {
    tenant_slug:              row.querySelector('.target-tenant').value,
    notification_channel_id:  (chanSelect.style.display !== 'none' ? chanSelect.value : chanFallback.value).trim(),
    notification_role_id:     (roleSelect.style.display !== 'none' ? roleSelect.value : roleFallback.value).trim(),
  };
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
  return !sel.classList.contains('hidden') ? sel.value : fb.value;
}

// Event cover image (spec §33) — read client-side as a data URI via
// FileReader and stored as-is (no server-side re-encoding needed, since
// that's exactly the format Discord's own Scheduled Event "image" field
// takes). #mCoverImageData is the value saveEvent() actually reads;
// #mCoverImageFile is just the picker, reset on every modal open so a
// stale filename never lingers after a save.
const COVER_IMAGE_MAX_BYTES = 8 * 1024 * 1024;

function setCoverImagePreview(dataUri) {
  document.getElementById('mCoverImageData').value = dataUri || '';
  const img = document.getElementById('mCoverImagePreview');
  const removeBtn = document.getElementById('mCoverImageRemove');
  if (dataUri) {
    img.src = dataUri;
    img.classList.remove('hidden');
    removeBtn.classList.remove('hidden');
  } else {
    img.classList.add('hidden');
    img.src = '';
    removeBtn.classList.add('hidden');
  }
}

function handleCoverImageFile(input) {
  const file = input.files && input.files[0];
  if (!file) return;
  if (file.size > COVER_IMAGE_MAX_BYTES) {
    toast('Cover image is too large (max 8MB) — pick a smaller file', true);
    input.value = '';
    return;
  }
  const reader = new FileReader();
  reader.onload = () => setCoverImagePreview(reader.result);
  reader.onerror = () => toast('Could not read that image file', true);
  reader.readAsDataURL(file);
}

function removeCoverImage() {
  setCoverImagePreview('');
  document.getElementById('mCoverImageFile').value = '';
}

function closeModal() {
  document.getElementById('eventModal').classList.remove('open');
  unfocusModal();
}

async function saveEvent() {
  const id = document.getElementById('modalEventId').value;
  const startTime = document.getElementById('mStartTime').value.trim();
  if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(startTime)) {
    toast('Start time must be 24-hour HH:MM (e.g. 19:00)', true);
    return;
  }
  const owningSelection = parseOwningTenantScopeValue(document.getElementById('mOwningTenant').value);
  const payload = {
    name:            document.getElementById('mName').value,
    scope:           owningSelection.scope,
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
    // Always sent, even empty — the modal shows the full current target
    // list every time it's opened (see openEventModal), so the full list
    // is what gets saved back, same convention as every other field here.
    targets: Array.from(document.querySelectorAll('#mTargetsList .event-target-row'))
      .map(eventTargetRowValue)
      .filter(t => t.notification_channel_id),
    description:     document.getElementById('mDescription').value,
    cover_image_data: document.getElementById('mCoverImageData').value,
  };
  // Spec §49 — a create sends the modal's selection straight as the
  // X-Tenant-Slug header (that tenant becomes the owner). An edit's
  // header must stay the event's ORIGINAL owning tenant (update_event
  // looks the row up by that, and needs it to authorize a reassignment);
  // the modal's (possibly different) selection goes in the payload as
  // owning_tenant_slug instead, so the backend can move it.
  let owningTenantOverride;
  if (id) {
    owningTenantOverride = tenantSlugFor((EVENTS_CACHE.find(e => String(e.id) === String(id)) || {}).owning_tenant_id);
    payload.owning_tenant_slug = owningSelection.slug;
  } else {
    owningTenantOverride = owningSelection.slug;
  }

  try {
    if (id) {
      await api('PATCH', `/api/events/${id}`, payload, false, owningTenantOverride);
      toast('Event updated');
    } else {
      await api('POST', '/api/events', payload, false, owningTenantOverride);
      toast('Event created');
    }
    closeModal();
    loadEvents();
  } catch(e) { toast(e.message, true); }
}

async function permanentDelete(btn) {
  var id   = btn.getAttribute('data-id');
  var name = btn.getAttribute('data-name');

  if (!confirm('Permanently delete "' + name + '"?\n\nThis will:\n- Remove the event definition\n- Delete all future occurrences\n- Preserve PostLog history\n\nThis cannot be undone.')) return;

  var input = prompt('Type the event name to confirm:\n\n' + name);
  if (input === null) return;
  if (input.trim() !== name.trim()) {
    alert('Name did not match. Deletion cancelled.');
    return;
  }

  const tenantSlug = tenantSlugFor((EVENTS_CACHE.find(e => String(e.id) === String(id)) || {}).owning_tenant_id);
  try {
    var data = await api('DELETE', '/api/events/' + id + '/permanent', null, false, tenantSlug);
    toast('Deleted "' + data.event_name + '". ' + data.post_log_entries_preserved + ' PostLog entries preserved.');
    loadEvents();
  } catch(e) { toast(e.message, true); }
}

async function toggleActive(id, current) {
  const tenantSlug = tenantSlugFor((EVENTS_CACHE.find(e => String(e.id) === String(id)) || {}).owning_tenant_id);
  try {
    await api('PATCH', `/api/events/${id}`, { active: !current }, false, tenantSlug);
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


// Wire the Event modal's markdown toolbar once at load (see common.js's
// wireMarkdownToolbar) — the modal itself is hidden until openEventModal()
// shows it, but its markup (and this toolbar) exist in the static page
// from the start, so this can run immediately rather than waiting for a
// first open.
wireMarkdownToolbar('mDescriptionToolbar');

// Wire the Events view/modal's own controls — replaces their onclick/
// onchange/oninput attributes (Phase 3 audit remediation).
document.getElementById('btnAddEvent')?.addEventListener('click', () => openEventModal());
document.getElementById('btnExportEventsCsv')?.addEventListener('click', () => exportEventsCsv());
document.getElementById('btnImportEventsCsv')?.addEventListener('click', () => document.getElementById('eventsImportFile').click());
document.getElementById('eventsImportFile')?.addEventListener('change', function () { importEventsCsv(this.files[0]); });
document.getElementById('mStartTime')?.addEventListener('input', () => renderEventDescriptionPreview());
document.getElementById('mAnchor')?.addEventListener('change', () => renderEventDescriptionPreview());
document.getElementById('mDescription')?.addEventListener('input', () => renderEventDescriptionPreview());
document.getElementById('mPreviewTenant')?.addEventListener('change', () => renderEventDescriptionPreview());
document.getElementById('mCoverImageFile')?.addEventListener('change', function () { handleCoverImageFile(this); });
document.getElementById('mCoverImageRemove')?.addEventListener('click', () => removeCoverImage());
document.getElementById('mLeadershipOnly')?.addEventListener('change', () => toggleLeadershipNote());
document.getElementById('btnAddEventTarget')?.addEventListener('click', () => addEventTargetRow());
document.getElementById('btnPreviewOccurrences')?.addEventListener('click', () => previewOccurrences());
document.getElementById('btnSaveEvent')?.addEventListener('click', () => saveEvent());
