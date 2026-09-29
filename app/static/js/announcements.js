// Announcements view (#v-announcements), spec §13: one-off or recurring
// scheduled channel posts, not built on EventDefinition/Occurrence at all —
// no start/end, no duration, no anchor date. Depends on common.js (api,
// toast, escapeHtml, TENANTS, dualTimeString/fmtDateTime) and events.js's
// tenantName()/tenantSlugFor() helpers.

// The last list loaded by loadAnnouncements() — duplicateAnnouncement()
// reads from this instead of a second GET, since the row it's duplicating
// is already sitting in front of the user.
let ANNOUNCEMENTS = [];

// Same pattern for Announcement Templates (spec §27) — useTemplate() and
// editTemplate() read from this instead of a second GET.
let ANNOUNCEMENT_TEMPLATES = [];

async function loadAnnouncementTemplates() {
  try {
    ANNOUNCEMENT_TEMPLATES = await api('GET', '/api/announcement-templates');
    const tbody = document.getElementById('templatesBody');
    tbody.innerHTML = ANNOUNCEMENT_TEMPLATES.length
      ? ANNOUNCEMENT_TEMPLATES.map(t =>
          '<tr class="pf-v6-c-table__tr">'
          + '<td class="pf-v6-c-table__td">' + escapeHtml(t.name) + (t.leadership_only ? ' <span title="Leadership only">👑</span>' : '') + '</td>'
          + '<td class="pf-v6-c-table__td">' + escapeHtml(t.title_template) + '</td>'
          + '<td class="pf-v6-c-table__td" style="color:var(--muted)">' + (t.event_offset_minutes ? t.event_offset_minutes + ' min' : '—') + '</td>'
          + '<td class="pf-v6-c-table__td" style="display:flex;gap:6px;flex-wrap:wrap">'
          + '<button class="pf-v6-c-button pf-m-primary pf-m-small" onclick="useTemplate(' + t.id + ')" title="Open a new announcement pre-filled from this template">Use</button>'
          + '<button class="pf-v6-c-button pf-m-secondary pf-m-small" onclick="editTemplate(' + t.id + ')">Edit</button>'
          + '<button class="pf-v6-c-button pf-m-danger pf-m-small" onclick="deleteTemplate(' + t.id + ')">Delete</button>'
          + '</td>'
          + '</tr>'
        ).join('')
      : '<tr class="pf-v6-c-table__tr"><td class="pf-v6-c-table__td" colspan="4" style="color:var(--muted);padding:20px">No templates yet.</td></tr>';
  } catch (e) { toast(e.message, true); }
}

function openTemplateModal(source) {
  document.getElementById('tTemplateId').value = source ? source.id : '';
  document.getElementById('tName').value = source ? source.name : '';
  document.getElementById('tTitle').value = source ? source.title_template : '';
  document.getElementById('tBody').value = source ? source.body_template : '';
  document.getElementById('tEventOffsetMinutes').value = source ? source.event_offset_minutes : 0;
  document.getElementById('tLeadershipOnly').checked = !!(source && source.leadership_only);
  document.querySelector('#templateModalTitle .pf-v6-c-modal-box__title-text').textContent =
    source ? 'Edit Template' : 'New Template';
  document.getElementById('templateModal').classList.add('open');
}

function closeTemplateModal() {
  document.getElementById('templateModal').classList.remove('open');
}

function editTemplate(id) {
  const source = ANNOUNCEMENT_TEMPLATES.find(t => t.id === id);
  if (!source) return;
  openTemplateModal(source);
}

async function saveTemplate() {
  const id = document.getElementById('tTemplateId').value;
  const name = document.getElementById('tName').value.trim();
  const title_template = document.getElementById('tTitle').value.trim();
  const body_template = document.getElementById('tBody').value;
  if (!name || !title_template || !body_template) {
    toast('Name, title, and body are all required', true);
    return;
  }
  const payload = {
    name, title_template, body_template,
    leadership_only: document.getElementById('tLeadershipOnly').checked,
    event_offset_minutes: parseInt(document.getElementById('tEventOffsetMinutes').value, 10) || 0,
  };
  try {
    if (id) {
      await api('PATCH', `/api/announcement-templates/${id}`, payload);
      toast('Template updated');
    } else {
      await api('POST', '/api/announcement-templates', payload);
      toast('Template created');
    }
    closeTemplateModal();
    loadAnnouncementTemplates();
  } catch (e) { toast(e.message, true); }
}

async function deleteTemplate(id) {
  const t = ANNOUNCEMENT_TEMPLATES.find(x => x.id === id);
  if (!confirm(`Delete the template "${t ? t.name : ''}"? This cannot be undone.`)) return;
  try {
    await api('DELETE', `/api/announcement-templates/${id}`);
    toast('Template deleted');
    loadAnnouncementTemplates();
  } catch (e) { toast(e.message, true); }
}

// Opens the New Announcement modal pre-filled from a template — a
// one-time copy, same relationship duplicateAnnouncement() has to the
// announcement it copies from (see openAnnouncementModal's own comment).
// Deliberately built as a source object shaped like (a subset of) an
// Announcement, so openAnnouncementModal needs no template-specific branch.
function useTemplate(id) {
  const t = ANNOUNCEMENT_TEMPLATES.find(x => x.id === id);
  if (!t) return;
  openAnnouncementModal({
    title: t.title_template,
    body_markdown: t.body_template,
    leadership_only: t.leadership_only,
    event_offset_minutes: t.event_offset_minutes,
  });
}

// The reverse direction — captures whatever's currently in the open
// announcement modal (title/body/leadership/event offset only, never the
// schedule or targets) as a new named template. Prompt-based, matching
// this app's established convention for a single-field quick-create
// (Kingdom/Tenant/Discord Server all do the same) rather than opening a
// second modal on top of the first.
async function saveCurrentAsTemplate() {
  const name = prompt('Save this title/body as a template named:');
  if (!name) return;
  const payload = {
    name,
    title_template: document.getElementById('aTitle').value.trim(),
    body_template: document.getElementById('aBody').value,
    leadership_only: document.getElementById('aLeadershipOnly').checked,
    event_offset_minutes: parseInt(document.getElementById('aEventOffsetMinutes').value, 10) || 0,
  };
  if (!payload.title_template || !payload.body_template) {
    toast('Title and body are both required to save as a template', true);
    return;
  }
  try {
    await api('POST', '/api/announcement-templates', payload);
    toast(`Template "${name}" saved`);
    loadAnnouncementTemplates();
  } catch (e) { toast(e.message, true); }
}

async function loadAnnouncements() {
  try {
    const items = await api('GET', '/api/announcements');
    ANNOUNCEMENTS = items;
    const tbody = document.getElementById('announcementsBody');
    if (!items.length) {
      tbody.innerHTML = '<tr class="pf-v6-c-table__tr"><td class="pf-v6-c-table__td" colspan="6" style="color:var(--muted);padding:20px">No announcements yet. Click &quot;+ New Announcement&quot; to schedule one.</td></tr>';
      return;
    }
    const announcementStatusColor = { draft: 'pf-m-grey', scheduled: 'pf-m-blue', posted: 'pf-m-green', failed: 'pf-m-red', cancelled: 'pf-m-grey' };
    const targetStatusColor = { pending: 'pf-m-grey', posted: 'pf-m-green', error: 'pf-m-red' };
    const deletableStatuses = ['posted', 'failed', 'cancelled'];

    tbody.innerHTML = items.map(a => {
      // A cancelled announcement's targets never got a real post attempt
      // past that point, so showing their pre-cancel post_status (usually
      // a stale "pending") is misleading — the announcement-level status
      // is what actually governs them once cancelled.
      const targetsHtml = a.targets.map(t => {
        const displayStatus = a.status === 'cancelled' ? 'cancelled' : t.post_status;
        const color = a.status === 'cancelled' ? 'pf-m-grey' : (targetStatusColor[t.post_status] || 'pf-m-grey');
        const label = tenantName(t.tenant_id) + ': ' + pfLabel(escapeHtml(displayStatus), color);
        return t.status_detail && a.status !== 'cancelled'
          ? '<div title="' + escapeHtml(t.status_detail) + '">' + label + '</div>'
          : '<div>' + label + '</div>';
      }).join('');
      const cancelBtn = a.status === 'scheduled'
        ? '<button class="pf-v6-c-button pf-m-danger pf-m-small" onclick="cancelAnnouncement(' + a.id + ')">Cancel</button>'
        : '';
      const deleteBtn = deletableStatuses.includes(a.status)
        ? '<button class="pf-v6-c-button pf-m-danger pf-m-small" onclick="deleteAnnouncement(' + a.id + ')" title="Remove this finished announcement from the list">Delete</button>'
        : '';
      const duplicateBtn = '<button class="pf-v6-c-button pf-m-secondary pf-m-small" onclick="duplicateAnnouncement(' + a.id + ')" title="Open a new announcement pre-filled with this one\'s title, body, and targets">Duplicate</button>';
      const leadershipBadge = a.leadership_only
        ? ' <span title="Leadership only">👑</span>'
        : ' <span title="General">🛡️</span>';
      const recurringBadge = a.recurring
        ? pfLabel('Every ' + a.interval_days + 'd', 'pf-m-purple')
        : pfLabel('One-time', 'pf-m-grey');
      return '<tr class="pf-v6-c-table__tr">'
        + '<td class="pf-v6-c-table__td">' + escapeHtml(a.title) + leadershipBadge + '</td>'
        + '<td class="pf-v6-c-table__td">' + fmtDateTime(a.scheduled_for) + '</td>'
        + '<td class="pf-v6-c-table__td">' + recurringBadge + '</td>'
        + '<td class="pf-v6-c-table__td">' + pfLabel(escapeHtml(a.status), announcementStatusColor[a.status] || 'pf-m-grey') + '</td>'
        + '<td class="pf-v6-c-table__td">' + targetsHtml + '</td>'
        + '<td class="pf-v6-c-table__td" style="display:flex;gap:6px;flex-wrap:wrap">' + [cancelBtn, deleteBtn, duplicateBtn].filter(Boolean).join('') + '</td>'
        + '</tr>';
    }).join('');
  } catch (e) { toast(e.message, true); }
}

function updateAnnouncementCharCount() {
  document.getElementById('aBodyCount').textContent = document.getElementById('aBody').value.length;
}

function toggleAnnouncementRecurringNote() {
  const on = document.getElementById('aRecurring').checked;
  document.getElementById('aIntervalGroup').style.display = on ? '' : 'none';
  if (!on) document.getElementById('aIntervalDays').value = '';
}

// One target row = one (tenant, Discord channel) pair (spec §13.3). Each
// row loads its own tenant's live channel list — separate from the Event
// modal's single-tenant populateDiscordFields(), since here every row can
// point at a different alliance's Discord server. Falls back to a free-text
// channel ID input if that tenant's channel list can't be fetched, same
// resilience as the Event modal's own fallback.
function addAnnouncementTargetRow(tenantSlug, channelId) {
  const list = document.getElementById('aTargetsList');
  const row = document.createElement('div');
  row.className = 'announcement-target-row';
  row.style = 'display:flex;gap:8px;align-items:center';
  const tenantOptions = TENANTS.map(t =>
    `<option value="${escapeHtml(t.slug)}" ${t.slug === tenantSlug ? 'selected' : ''}>${escapeHtml(t.name)}</option>`
  ).join('');
  row.innerHTML = `
    <select class="pf-v6-c-form-control target-tenant" style="flex:1">${tenantOptions}</select>
    <select class="pf-v6-c-form-control target-channel" style="flex:1"><option>Loading…</option></select>
    <input class="pf-v6-c-form-control target-channel-fallback" type="text" style="flex:1;display:none" placeholder="Discord channel ID (digits only)">
    <button type="button" class="pf-v6-c-button pf-m-danger pf-m-small" onclick="this.closest('.announcement-target-row').remove()">Remove</button>
  `;
  list.appendChild(row);
  const select = row.querySelector('.target-tenant');
  select.addEventListener('change', () => populateAnnouncementTargetChannels(row, select.value));
  populateAnnouncementTargetChannels(row, tenantSlug || select.value, channelId);
}

async function populateAnnouncementTargetChannels(row, tenantSlug, channelId) {
  const select = row.querySelector('.target-channel');
  const fallback = row.querySelector('.target-channel-fallback');
  select.innerHTML = '<option>Loading…</option>';
  select.disabled = true;
  select.style.display = '';
  fallback.style.display = 'none';
  try {
    const channels = await api('GET', '/api/discord/channels', null, false, tenantSlug);
    fillSelect(select, channels.map(c => ({ value: c.id, label: '#' + c.name })), channelId);
    select.disabled = false;
  } catch (e) {
    select.style.display = 'none';
    fallback.style.display = '';
    fallback.value = channelId || '';
    toast(`Could not load Discord channels for ${tenantSlug} — enter the channel ID manually`, true);
  }
}

function announcementTargetChannelValue(row) {
  const select = row.querySelector('.target-channel');
  const fallback = row.querySelector('.target-channel-fallback');
  return select.style.display !== 'none' ? select.value : fallback.value;
}

// source, when passed (duplicateAnnouncement below), pre-fills every
// field except the send date/time — a duplicate always needs a fresh
// future schedule, never the original's (which is either already past,
// or the very thing that got cancelled/failed).
function openAnnouncementModal(source) {
  document.getElementById('aTitle').value = source ? source.title : '';
  document.getElementById('aBody').value = source ? source.body_markdown : '';
  updateAnnouncementCharCount();
  document.getElementById('aScheduledDate').value = '';
  document.getElementById('aScheduledTime').value = '';
  document.getElementById('aRecurring').checked = !!(source && source.recurring);
  document.getElementById('aIntervalDays').value = (source && source.interval_days) ? source.interval_days : '';
  document.getElementById('aIntervalGroup').style.display = (source && source.recurring) ? '' : 'none';
  document.getElementById('aLeadershipOnly').checked = !!(source && source.leadership_only);
  document.getElementById('aEventOffsetMinutes').value = (source && source.event_offset_minutes) ? source.event_offset_minutes : 0;
  document.getElementById('aTargetsList').innerHTML = '';
  if (source && source.targets.length) {
    source.targets.forEach(t => addAnnouncementTargetRow(tenantSlugFor(t.tenant_id), t.discord_channel_id));
  } else {
    // The Announcements tab is single-tenant-only (see common.js's
    // SINGLE_TENANT_ONLY_VIEWS) — the picker is never on '*' while this
    // modal is reachable, so the first target can default to it directly.
    addAnnouncementTargetRow(getCurrentTenantSlug());
  }
  document.querySelector('#announcementModalTitle .pf-v6-c-modal-box__title-text').textContent =
    source ? 'Duplicate Announcement' : 'New Announcement';
  document.getElementById('announcementModal').classList.add('open');
}

function closeAnnouncementModal() {
  document.getElementById('announcementModal').classList.remove('open');
}

function duplicateAnnouncement(id) {
  const source = ANNOUNCEMENTS.find(a => a.id === id);
  if (!source) return;
  openAnnouncementModal(source);
}

async function saveAnnouncement() {
  const title = document.getElementById('aTitle').value.trim();
  const body = document.getElementById('aBody').value;
  const scheduledDate = document.getElementById('aScheduledDate').value; // "YYYY-MM-DD"
  const scheduledTime = document.getElementById('aScheduledTime').value.trim(); // "HH:MM", 24-hour, entered as UTC
  const recurring = document.getElementById('aRecurring').checked;
  const intervalRaw = document.getElementById('aIntervalDays').value;
  if (!title || !body || !scheduledDate || !scheduledTime) {
    toast('Title, body, and send date/time are all required', true);
    return;
  }
  // Same 24-hour HH:MM pattern events.js's saveEvent() enforces on
  // mStartTime — a plain text field rather than a native time/
  // datetime-local input specifically so the format can't drift to
  // AM/PM display depending on the browser's locale.
  if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(scheduledTime)) {
    toast('Send time must be 24-hour HH:MM (e.g. 19:00)', true);
    return;
  }
  // Same rule the server enforces (routers/admin/announcements.py) —
  // checked here too so a past date/time is caught before the round
  // trip, not just after.
  if (new Date(scheduledDate + 'T' + scheduledTime + ':00Z').getTime() <= Date.now()) {
    toast('Send date/time must be in the future', true);
    return;
  }
  if (recurring && (!intervalRaw || parseInt(intervalRaw) <= 0)) {
    toast('Recurring announcements need a positive "Repeat every (days)" value', true);
    return;
  }
  const targets = Array.from(document.querySelectorAll('#aTargetsList .announcement-target-row')).map(row => ({
    tenant_slug: row.querySelector('.target-tenant').value,
    discord_channel_id: announcementTargetChannelValue(row).trim(),
  }));
  if (!targets.length || targets.some(t => !t.discord_channel_id)) {
    toast('Every target needs a Discord channel selected', true);
    return;
  }

  const payload = {
    title,
    body_markdown: body,
    scheduled_for: scheduledDate + 'T' + scheduledTime + ':00+00:00', // explicit UTC offset, matching the "Send Date/Time (UTC)" field labels
    targets,
    leadership_only: document.getElementById('aLeadershipOnly').checked,
    recurring,
    interval_days: recurring ? parseInt(intervalRaw) : null,
    event_offset_minutes: parseInt(document.getElementById('aEventOffsetMinutes').value, 10) || 0,
  };

  try {
    await api('POST', '/api/announcements', payload);
    toast('Announcement scheduled');
    closeAnnouncementModal();
    loadAnnouncements();
  } catch (e) { toast(e.message, true); }
}

async function cancelAnnouncement(id) {
  if (!confirm('Cancel this announcement? It will not be posted.')) return;
  try {
    await api('POST', `/api/announcements/${id}/cancel`);
    toast('Announcement cancelled');
    loadAnnouncements();
  } catch (e) { toast(e.message, true); }
}

async function deleteAnnouncement(id) {
  if (!confirm('Delete this announcement permanently? This cannot be undone.')) return;
  try {
    await api('DELETE', `/api/announcements/${id}`);
    toast('Announcement deleted');
    loadAnnouncements();
  } catch (e) { toast(e.message, true); }
}
