// Announcements view (#v-announcements), spec §13: one-off or recurring
// scheduled channel posts, not built on EventDefinition/Occurrence at all —
// no start/end, no duration, no anchor date. Depends on common.js (api,
// toast, escapeHtml, TENANTS, dualTimeString/fmtDateTime) and events.js's
// tenantName()/tenantSlugFor() helpers.

// ── Composer: markdown toolbar, live preview, emoji picker (spec §28) ──

// role/channel lookups the preview needs, cached per tenant slug for the
// life of the page — re-fetching on every keystroke would be wasteful,
// and neither list changes while a modal is open.
const PREVIEW_ROLES_CACHE = {};
const PREVIEW_CHANNELS_CACHE = {};
// kingdom_id -> name, lazy-loaded once (GET /api/kingdoms is available to
// any authenticated user, not just superadmins) since TENANTS only
// carries kingdom_id, not the resolved name §28's {kingdom_name} needs.
let KINGDOM_NAMES_CACHE = null;

async function ensureKingdomNamesLoaded() {
  if (KINGDOM_NAMES_CACHE) return KINGDOM_NAMES_CACHE;
  KINGDOM_NAMES_CACHE = {};
  try {
    const kingdoms = await api('GET', '/api/kingdoms', null, /*skipTenantHeader=*/true);
    kingdoms.forEach(k => { KINGDOM_NAMES_CACHE[k.id] = k.name; });
  } catch (e) { /* preview degrades to blank kingdom name; not fatal */ }
  return KINGDOM_NAMES_CACHE;
}

async function ensurePreviewRolesLoaded(tenantSlug) {
  if (PREVIEW_ROLES_CACHE[tenantSlug]) return PREVIEW_ROLES_CACHE[tenantSlug];
  try {
    const roles = await api('GET', '/api/discord/roles', null, false, tenantSlug);
    PREVIEW_ROLES_CACHE[tenantSlug] = roles;
    return roles;
  } catch (e) { return []; }
}

async function ensurePreviewChannelsLoaded(tenantSlug) {
  if (PREVIEW_CHANNELS_CACHE[tenantSlug]) return PREVIEW_CHANNELS_CACHE[tenantSlug];
  try {
    const channels = await api('GET', '/api/discord/channels', null, false, tenantSlug);
    PREVIEW_CHANNELS_CACHE[tenantSlug] = channels;
    return channels;
  } catch (e) { return []; }
}

// Wraps the current selection in `before`/`after`. With nothing selected,
// inserts both markers around `placeholder` and selects it, so typing
// immediately overwrites it — same "type over the placeholder" pattern
// as a native form field's placeholder text.
function wrapSelection(textareaId, before, after, placeholder) {
  const ta = document.getElementById(textareaId);
  if (!ta) return;
  const start = ta.selectionStart;
  const end = ta.selectionEnd;
  const selected = ta.value.slice(start, end);
  const text = selected || placeholder || '';
  ta.value = ta.value.slice(0, start) + before + text + after + ta.value.slice(end);
  ta.focus();
  ta.setSelectionRange(start + before.length, start + before.length + text.length);
  ta.dispatchEvent(new Event('input', { bubbles: true }));
}

// Quote (`> `) is a line-level prefix, not a wrap — applied to every
// line touched by the current selection (or just the current line, with
// nothing selected).
function prefixSelectedLines(textareaId, prefix) {
  const ta = document.getElementById(textareaId);
  if (!ta) return;
  const start = ta.selectionStart;
  const end = ta.selectionEnd;
  const lineStart = ta.value.lastIndexOf('\n', start - 1) + 1;
  let lineEnd = ta.value.indexOf('\n', end);
  if (lineEnd === -1) lineEnd = ta.value.length;
  const block = ta.value.slice(lineStart, lineEnd);
  const prefixed = block.split('\n').map(l => prefix + l).join('\n');
  ta.value = ta.value.slice(0, lineStart) + prefixed + ta.value.slice(lineEnd);
  ta.focus();
  ta.setSelectionRange(lineStart, lineStart + prefixed.length);
  ta.dispatchEvent(new Event('input', { bubbles: true }));
}

function insertCodeBlock(textareaId) {
  const ta = document.getElementById(textareaId);
  if (!ta) return;
  const start = ta.selectionStart;
  const end = ta.selectionEnd;
  const selected = ta.value.slice(start, end) || 'code';
  const block = '```\n' + selected + '\n```';
  ta.value = ta.value.slice(0, start) + block + ta.value.slice(end);
  ta.focus();
  ta.setSelectionRange(start + 4, start + 4 + selected.length);
  ta.dispatchEvent(new Event('input', { bubbles: true }));
}

// Small client-side port of services/templates.py's render_placeholders()
// — kept as a second implementation (one runs in the browser, one on the
// server; this app has no shared-code mechanism between them, §22) and
// deliberately produces a human-*readable* approximation rather than
// literal `<t:UNIX:F>` tags, since the preview can't reproduce what every
// future Discord viewer's own client will render in their own locale —
// it shows one representative rendering, in the admin's own browser
// time zone, labeled "(preview)" so that's clear.
function formatDiscordAbsolutePreview(date) {
  return date.toLocaleString(undefined, {
    weekday: 'long', year: 'numeric', month: 'long', day: 'numeric',
    hour: 'numeric', minute: '2-digit',
  }) + ' (preview)';
}

function formatDiscordRelativePreview(date) {
  const diffSeconds = Math.round((date.getTime() - Date.now()) / 1000);
  const abs = Math.abs(diffSeconds);
  const units = [
    ['day', 86400], ['hour', 3600], ['minute', 60], ['second', 1],
  ];
  for (const [name, secs] of units) {
    if (abs >= secs || name === 'second') {
      const count = Math.max(1, Math.round(abs / secs));
      const plural = count === 1 ? name : name + 's';
      return (diffSeconds >= 0 ? `in ${count} ${plural}` : `${count} ${plural} ago`) + ' (preview)';
    }
  }
}

function clientRenderPlaceholders(text, { allianceName, kingdomName, scheduledFor, eventOffsetMinutes }) {
  const eventTime = new Date(scheduledFor.getTime() + (eventOffsetMinutes || 0) * 60000);
  const values = {
    alliance_name: allianceName || '',
    kingdom_name: kingdomName || '',
    send_time: formatDiscordAbsolutePreview(scheduledFor),
    send_time_relative: formatDiscordRelativePreview(scheduledFor),
    event_time: formatDiscordAbsolutePreview(eventTime),
    event_time_relative: formatDiscordRelativePreview(eventTime),
  };
  return text.replace(/\{(alliance_name|kingdom_name|send_time|send_time_relative|event_time|event_time_relative)\}/g,
    (m, name) => (values[name] !== undefined ? values[name] : m));
}

// Small markdown-to-HTML renderer covering exactly the Discord markdown
// syntax spec §27 documents as supported, plus role/channel mention
// resolution for the preview's benefit only (§28) — never sent anywhere,
// the stored/sent text is always the literal markdown. HTML-escapes the
// raw input first so nothing the coordinator types can inject markup.
function renderDiscordMarkdownPreview(text, { roles, channels }) {
  let html = escapeHtml(text);

  // Code blocks and inline code first, and protected from every later
  // transform via placeholder tokens, so markdown characters inside code
  // are shown literally rather than re-processed.
  const codeStash = [];
  html = html.replace(/```([\s\S]*?)```/g, (m, code) => {
    codeStash.push('<pre class="preview-codeblock"><code>' + code.replace(/^\n/, '') + '</code></pre>');
    return `\u0000CODEBLOCK${codeStash.length - 1}\u0000`;
  });
  html = html.replace(/`([^`\n]+)`/g, (m, code) => {
    codeStash.push('<code class="preview-inline-code">' + code + '</code>');
    return `\u0000CODEBLOCK${codeStash.length - 1}\u0000`;
  });

  // Block quotes — one or more consecutive "&gt; " lines (already
  // HTML-escaped) become a single <blockquote>.
  html = html.replace(/^(?:&gt; ?.*(?:\n|$))+/gm, (m) =>
    '<blockquote class="preview-quote">' + m.replace(/^&gt; ?/gm, '').trim() + '</blockquote>'
  );

  // Headers
  html = html.replace(/^### (.*)$/gm, '<h3 class="preview-h">$1</h3>');
  html = html.replace(/^## (.*)$/gm, '<h2 class="preview-h">$1</h2>');
  html = html.replace(/^# (.*)$/gm, '<h1 class="preview-h">$1</h1>');

  // Inline emphasis — bold before italic so **x** isn't first read as
  // italic-inside-italic; underline/strike/spoiler are unambiguous.
  html = html.replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>');
  html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/__([^_]+)__/g, '<u>$1</u>');
  html = html.replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g, '<em>$1</em>');
  html = html.replace(/~~([^~]+)~~/g, '<s>$1</s>');
  html = html.replace(/\|\|([^|]+)\|\|/g, '<span class="preview-spoiler" onclick="this.classList.add(\'revealed\')">$1</span>');

  // Role/channel mentions — HTML-escaping turned <@&123> into
  // &lt;@&amp;123&gt; and <#123> into &lt;#123&gt;, so match those escaped forms.
  html = html.replace(/&lt;@&amp;(\d+)&gt;/g, (m, id) => {
    const role = (roles || []).find(r => String(r.id) === id);
    return '<span class="preview-mention">@' + (role ? escapeHtml(role.name) : 'role') + '</span>';
  });
  html = html.replace(/&lt;#(\d+)&gt;/g, (m, id) => {
    const channel = (channels || []).find(c => String(c.id) === id);
    return '<span class="preview-mention">#' + (channel ? escapeHtml(channel.name) : 'channel') + '</span>';
  });

  // Bare list markers get a little visual indent; not full list rendering.
  html = html.replace(/^- (.*)$/gm, '&nbsp;&nbsp;• $1');

  // Remaining single newlines are line breaks, matching Discord's own
  // (non-paragraph) line-break behavior.
  html = html.replace(/\n/g, '<br>');

  // Restore protected code spans.
  html = html.replace(/\u0000CODEBLOCK(\d+)\u0000/g, (m, i) => codeStash[Number(i)]);

  return html || '<span style="color:var(--muted)">(nothing to preview yet)</span>';
}

// Rebuilds the "Preview As" dropdown from the modal's own live target
// rows (announcement modal) or the current tenant (template modal, which
// has no targets) — deliberately never a fabricated tenant, since that
// was §27.3's original objection to a preview at all.
function refreshPreviewTenantOptions(selectId, targetsListId) {
  const select = document.getElementById(selectId);
  if (!select) return;
  const previousValue = select.value;
  let slugs;
  if (targetsListId) {
    slugs = Array.from(document.querySelectorAll('#' + targetsListId + ' .target-tenant'))
      .map(s => s.value)
      .filter((v, i, arr) => v && arr.indexOf(v) === i);
  } else {
    slugs = [getCurrentTenantSlug()].filter(Boolean);
  }
  select.innerHTML = slugs.map(slug => {
    const t = TENANTS.find(x => x.slug === slug);
    return '<option value="' + escapeHtml(slug) + '">' + escapeHtml(t ? t.name : slug) + '</option>';
  }).join('');
  if (slugs.includes(previousValue)) select.value = previousValue;
}

async function renderAnnouncementPreview() {
  await renderComposerPreview({
    bodyId: 'aBody', paneId: 'aPreviewPane', previewTenantSelectId: 'aPreviewTenant',
    dateId: 'aScheduledDate', timeId: 'aScheduledTime', offsetId: 'aEventOffsetMinutes',
  });
}

async function renderTemplatePreview() {
  await renderComposerPreview({
    bodyId: 'tBody', paneId: 'tPreviewPane', previewTenantSelectId: 'tPreviewTenant',
    dateId: null, timeId: null, offsetId: 'tEventOffsetMinutes',
  });
}

async function renderComposerPreview({ bodyId, paneId, previewTenantSelectId, dateId, timeId, offsetId }) {
  const pane = document.getElementById(paneId);
  if (!pane) return;
  const body = document.getElementById(bodyId).value;
  const tenantSlug = document.getElementById(previewTenantSelectId).value || getCurrentTenantSlug();
  const tenant = TENANTS.find(t => t.slug === tenantSlug);

  let scheduledFor = null;
  if (dateId && timeId) {
    const dateVal = document.getElementById(dateId).value;
    const timeVal = document.getElementById(timeId).value.trim();
    if (dateVal && /^([01]\d|2[0-3]):[0-5]\d$/.test(timeVal)) {
      scheduledFor = new Date(dateVal + 'T' + timeVal + ':00Z');
    }
  }
  const usingFallbackTime = !scheduledFor;
  if (!scheduledFor) scheduledFor = new Date();

  const kingdomNames = await ensureKingdomNamesLoaded();
  const [roles, channels] = await Promise.all([
    ensurePreviewRolesLoaded(tenantSlug),
    ensurePreviewChannelsLoaded(tenantSlug),
  ]);

  const resolved = clientRenderPlaceholders(body, {
    allianceName: tenant ? tenant.name : tenantSlug,
    kingdomName: tenant ? kingdomNames[tenant.kingdom_id] : '',
    scheduledFor,
    eventOffsetMinutes: parseInt(document.getElementById(offsetId).value, 10) || 0,
  });
  const html = renderDiscordMarkdownPreview(resolved, { roles, channels });

  pane.innerHTML = `
    <div class="discord-preview-msg">
      <div class="discord-preview-avatar">S</div>
      <div class="discord-preview-body">
        <div class="discord-preview-header"><span class="discord-preview-name">Samaya</span><span class="discord-preview-bot-tag">BOT</span></div>
        <div class="discord-preview-text">${html}</div>
      </div>
    </div>
    ${usingFallbackTime ? '<div class="discord-preview-note">Using the current time — no send time set yet.</div>' : ''}
  `;
}

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
  refreshPreviewTenantOptions('tPreviewTenant', null);
  renderTemplatePreview();
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
  renderAnnouncementPreview();
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
  select.addEventListener('change', () => {
    populateAnnouncementTargetChannels(row, select.value);
    refreshPreviewTenantOptions('aPreviewTenant', 'aTargetsList');
    renderAnnouncementPreview();
  });
  populateAnnouncementTargetChannels(row, tenantSlug || select.value, channelId);
  refreshPreviewTenantOptions('aPreviewTenant', 'aTargetsList');
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
  renderAnnouncementPreview();
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
