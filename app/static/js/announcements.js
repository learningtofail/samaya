// Announcements view (#v-announcements), spec §13: one-off or recurring
// scheduled channel posts, not built on EventDefinition/Occurrence at all —
// no start/end, no duration, no anchor date. Depends on common.js (api,
// toast, escapeHtml, TENANTS, dualTimeString/fmtDateTime) and events.js's
// tenantName() helper.

async function loadAnnouncements() {
  try {
    const items = await api('GET', '/api/announcements');
    const tbody = document.getElementById('announcementsBody');
    if (!items.length) {
      tbody.innerHTML = '<tr class="pf-v6-c-table__tr"><td class="pf-v6-c-table__td" colspan="6" style="color:var(--muted);padding:20px">No announcements yet. Click &quot;+ New Announcement&quot; to schedule one.</td></tr>';
      return;
    }
    const announcementStatusColor = { draft: 'pf-m-grey', scheduled: 'pf-m-blue', posted: 'pf-m-green', failed: 'pf-m-red', cancelled: 'pf-m-grey' };
    const targetStatusColor = { pending: 'pf-m-grey', posted: 'pf-m-green', error: 'pf-m-red' };

    tbody.innerHTML = items.map(a => {
      const targetsHtml = a.targets.map(t => {
        const label = tenantName(t.tenant_id) + ': ' + pfLabel(escapeHtml(t.post_status), targetStatusColor[t.post_status] || 'pf-m-grey');
        return t.status_detail
          ? '<div title="' + escapeHtml(t.status_detail) + '">' + label + '</div>'
          : '<div>' + label + '</div>';
      }).join('');
      const cancelBtn = a.status === 'scheduled'
        ? '<button class="pf-v6-c-button pf-m-danger pf-m-small" onclick="cancelAnnouncement(' + a.id + ')">Cancel</button>'
        : '';
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
        + '<td class="pf-v6-c-table__td">' + cancelBtn + '</td>'
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

function openAnnouncementModal() {
  document.getElementById('aTitle').value = '';
  document.getElementById('aBody').value = '';
  updateAnnouncementCharCount();
  document.getElementById('aScheduledFor').value = '';
  document.getElementById('aRecurring').checked = false;
  document.getElementById('aIntervalDays').value = '';
  document.getElementById('aIntervalGroup').style.display = 'none';
  document.getElementById('aLeadershipOnly').checked = false;
  document.getElementById('aTargetsList').innerHTML = '';
  // The Announcements tab is single-tenant-only (see common.js's
  // SINGLE_TENANT_ONLY_VIEWS) — the picker is never on '*' while this
  // modal is reachable, so the first target can default to it directly.
  addAnnouncementTargetRow(getCurrentTenantSlug());
  document.getElementById('announcementModal').classList.add('open');
}

function closeAnnouncementModal() {
  document.getElementById('announcementModal').classList.remove('open');
}

async function saveAnnouncement() {
  const title = document.getElementById('aTitle').value.trim();
  const body = document.getElementById('aBody').value;
  const scheduledLocal = document.getElementById('aScheduledFor').value; // "YYYY-MM-DDTHH:MM", entered as UTC
  const recurring = document.getElementById('aRecurring').checked;
  const intervalRaw = document.getElementById('aIntervalDays').value;
  if (!title || !body || !scheduledLocal) {
    toast('Title, body, and send time are all required', true);
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
    scheduled_for: scheduledLocal + ':00+00:00', // explicit UTC offset, matching the "Send At (UTC)" field label
    targets,
    leadership_only: document.getElementById('aLeadershipOnly').checked,
    recurring,
    interval_days: recurring ? parseInt(intervalRaw) : null,
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
