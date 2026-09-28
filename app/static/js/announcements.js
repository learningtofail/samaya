// Announcements view (#v-announcements), spec §13: one-off scheduled
// channel posts, not built on EventDefinition/Occurrence at all — no
// interval, start time, duration, or anchor date. Depends on common.js
// (api, toast, escapeHtml, TENANTS) and events.js's tenantName() helper.

async function loadAnnouncements() {
  try {
    const items = await api('GET', '/api/announcements');
    const tbody = document.getElementById('announcementsBody');
    if (!items.length) {
      tbody.innerHTML = '<tr class="pf-v6-c-table__tr"><td class="pf-v6-c-table__td" colspan="5" style="color:var(--muted);padding:20px">No announcements yet. Click &quot;+ New Announcement&quot; to schedule one.</td></tr>';
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
      return '<tr class="pf-v6-c-table__tr">'
        + '<td class="pf-v6-c-table__td">' + escapeHtml(a.title) + '</td>'
        + '<td class="pf-v6-c-table__td">' + a.scheduled_for.replace('T', ' ').slice(0, 16) + '</td>'
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

// One target row = one (tenant, Discord channel) pair (spec §13.3). The
// tenant select is limited to TENANTS, which /api/tenants already scopes
// to what this user can access — no separate permission check needed
// client-side, matching how the Event modal's Discord fields work.
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
    <input class="pf-v6-c-form-control target-channel" type="text" style="flex:1" placeholder="Discord channel ID (digits only)" value="${escapeHtml(channelId || '')}">
    <button type="button" class="pf-v6-c-button pf-m-danger pf-m-small" onclick="this.closest('.announcement-target-row').remove()">Remove</button>
  `;
  list.appendChild(row);
}

function openAnnouncementModal() {
  document.getElementById('aTitle').value = '';
  document.getElementById('aBody').value = '';
  updateAnnouncementCharCount();
  document.getElementById('aScheduledFor').value = '';
  document.getElementById('aDetectedTz').textContent = Intl.DateTimeFormat().resolvedOptions().timeZone;
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
  if (!title || !body || !scheduledLocal) {
    toast('Title, body, and send time are all required', true);
    return;
  }
  const targets = Array.from(document.querySelectorAll('#aTargetsList .announcement-target-row')).map(row => ({
    tenant_slug: row.querySelector('.target-tenant').value,
    discord_channel_id: row.querySelector('.target-channel').value.trim(),
  }));
  if (!targets.length || targets.some(t => !t.discord_channel_id)) {
    toast('Every target needs a Discord channel ID', true);
    return;
  }

  const payload = {
    title,
    body_markdown: body,
    scheduled_for: scheduledLocal + ':00+00:00', // explicit UTC offset, matching the "Send At (UTC)" field label
    targets,
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
