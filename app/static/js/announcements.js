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
  // Thin wrapper over common.js's shared formatRelativeTime() (spec §29)
  // — this file just adds the "(preview)" qualifier the composer needs.
  return formatRelativeTime(date) + ' (preview)';
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

  // Headers — the trailing \n? (rather than anchoring on $) consumes the
  // line's own newline along with the line itself, so the block-level
  // heading tag doesn't also leave behind a literal line break that the
  // later "remaining newlines become <br>" pass would otherwise add on
  // top of the heading's own margin.
  html = html.replace(/^### (.*)\n?/gm, '<h3 class="preview-h">$1</h3>');
  html = html.replace(/^## (.*)\n?/gm, '<h2 class="preview-h">$1</h2>');
  html = html.replace(/^# (.*)\n?/gm, '<h1 class="preview-h">$1</h1>');

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
    slugs = [announcementsTenantSlug()].filter(Boolean);
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
    roleSelectId: 'aRoleMention',
  });
}

async function renderTemplatePreview() {
  await renderComposerPreview({
    bodyId: 'tBody', paneId: 'tPreviewPane', previewTenantSelectId: 'tPreviewTenant',
    dateId: null, timeId: null, offsetId: 'tEventOffsetMinutes',
    roleSelectId: 'tRoleMention',
  });
}

// Spec §38.3: Announcements' list is combined by default now
// (getTabFilter('announcements')); Templates/create/cancel/retry/delete
// remain genuinely single-tenant server-side (owning_tenant_id), so they
// resolve to whichever real alliance the tab's filter currently points
// at, falling back to the first accessible alliance while the filter is
// "All".
function announcementsTenantSlug() {
  const saved = getTabFilter('announcements');
  if (saved !== COMBINED_SLUG && TENANTS.some(t => t.slug === saved)) return saved;
  return TENANTS[0] ? TENANTS[0].slug : '';
}

// Event descriptions (spec §32) get the same composer, but an Event has
// no independently-scheduled "send time" the way an Announcement does —
// occurrences.py always resolves {send_time}/{send_time_relative} as
// "now" and {event_time}/{event_time_relative} as this occurrence's own
// start. eventDateId/eventTimeId (the Anchor Date + Start Time fields)
// stand in for that occurrence start; the offset from "now" to it is
// computed here and fed through the exact same scheduledFor+offset math
// renderComposerPreview already uses for Announcements/Templates, rather
// than a second placeholder-resolution path.
async function renderEventDescriptionPreview() {
  await renderComposerPreview({
    bodyId: 'mDescription', paneId: 'mPreviewPane', previewTenantSelectId: 'mPreviewTenant',
    eventDateId: 'mAnchor', eventTimeId: 'mStartTime',
    fallbackNote: 'Using the current time — no anchor date/start time set yet.',
    roleSelectId: 'mRoleMention',
  });
}

// Fills a body/description toolbar's "Insert role mention" dropdown from
// the same tenant's role list the live preview just fetched anyway (spec
// §34) — one @RoleName option per role, reusing ensurePreviewRolesLoaded's
// cache rather than a second /api/discord/roles call. Picking one inserts
// the raw <@&ROLE_ID> Discord mention syntax at the cursor (insertAtCursor,
// common.js) — the same thing a coordinator would otherwise have to look
// up the role's numeric ID to type by hand, which is exactly the kind of
// mistake this removes: right role, right guild, no typing an ID at all.
function refreshRoleMentionSelect(selectId, roles) {
  const select = document.getElementById(selectId);
  if (!select) return;
  select.innerHTML = '<option value="">Insert role mention…</option>'
    + roles.map(r => `<option value="${escapeHtml(r.id)}">@${escapeHtml(r.name)}</option>`).join('');
}

function insertRoleMention(selectId, textareaId) {
  const select = document.getElementById(selectId);
  if (!select || !select.value) return;
  insertAtCursor(textareaId, '<@&' + select.value + '>');
  select.value = '';
}

async function renderComposerPreview({
  bodyId, paneId, previewTenantSelectId, dateId, timeId, offsetId, eventDateId, eventTimeId, fallbackNote, roleSelectId,
}) {
  const pane = document.getElementById(paneId);
  if (!pane) return;
  const body = document.getElementById(bodyId).value;
  const tenantSlug = document.getElementById(previewTenantSelectId).value || announcementsTenantSlug();
  const tenant = TENANTS.find(t => t.slug === tenantSlug);

  let scheduledFor, eventOffsetMinutes, usingFallbackTime;
  if (eventDateId && eventTimeId) {
    scheduledFor = new Date();
    const dateVal = document.getElementById(eventDateId).value;
    const timeVal = document.getElementById(eventTimeId).value.trim();
    if (dateVal && /^([01]\d|2[0-3]):[0-5]\d$/.test(timeVal)) {
      const eventStart = new Date(dateVal + 'T' + timeVal + ':00Z');
      eventOffsetMinutes = Math.round((eventStart.getTime() - scheduledFor.getTime()) / 60000);
      usingFallbackTime = false;
    } else {
      eventOffsetMinutes = 0;
      usingFallbackTime = true;
    }
  } else {
    scheduledFor = null;
    if (dateId && timeId) {
      const dateVal = document.getElementById(dateId).value;
      const timeVal = document.getElementById(timeId).value.trim();
      if (dateVal && /^([01]\d|2[0-3]):[0-5]\d$/.test(timeVal)) {
        scheduledFor = new Date(dateVal + 'T' + timeVal + ':00Z');
      }
    }
    usingFallbackTime = !scheduledFor;
    if (!scheduledFor) scheduledFor = new Date();
    eventOffsetMinutes = parseInt(document.getElementById(offsetId).value, 10) || 0;
  }

  const kingdomNames = await ensureKingdomNamesLoaded();
  const [roles, channels] = await Promise.all([
    ensurePreviewRolesLoaded(tenantSlug),
    ensurePreviewChannelsLoaded(tenantSlug),
  ]);
  if (roleSelectId) refreshRoleMentionSelect(roleSelectId, roles);

  const resolved = clientRenderPlaceholders(body, {
    allianceName: tenant ? tenant.name : tenantSlug,
    kingdomName: tenant ? kingdomNames[tenant.kingdom_id] : '',
    scheduledFor,
    eventOffsetMinutes,
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
    ${usingFallbackTime ? `<div class="discord-preview-note">${fallbackNote || 'Using the current time — no send time set yet.'}</div>` : ''}
  `;
}

// ANNOUNCEMENTS / ANNOUNCEMENT_TEMPLATES are declared in common.js (the
// first-loaded file) rather than here — see that declaration's comment for
// why a script-load-order hazard made this file the wrong place for them.

async function loadAnnouncementTemplates() {
  try {
    ANNOUNCEMENT_TEMPLATES = await api('GET', '/api/announcement-templates', null, false, announcementsTenantSlug());
    const tbody = document.getElementById('templatesBody');
    tbody.innerHTML = ANNOUNCEMENT_TEMPLATES.length
      ? ANNOUNCEMENT_TEMPLATES.map(t =>
          '<tr class="pf-v6-c-table__tr">'
          + '<td class="pf-v6-c-table__td">' + escapeHtml(t.name) + (t.leadership_only ? ' <span title="Leadership only">👑</span>' : '') + '</td>'
          + '<td class="pf-v6-c-table__td">' + escapeHtml(t.title_template) + '</td>'
          + '<td class="pf-v6-c-table__td" style="color:var(--muted)">' + (t.event_offset_minutes ? t.event_offset_minutes + ' min' : '—') + '</td>'
          + '<td class="pf-v6-c-table__td">'
          + '<div style="display:flex;gap:6px;flex-wrap:wrap">'
          + '<button class="pf-v6-c-button pf-m-primary pf-m-small" onclick="useTemplate(' + t.id + ')" title="Open a new announcement pre-filled from this template">Use</button>'
          + '<button class="pf-v6-c-button pf-m-secondary pf-m-small" onclick="editTemplate(' + t.id + ')">Edit</button>'
          + '<button class="pf-v6-c-button pf-m-danger pf-m-small" onclick="deleteTemplate(' + t.id + ')">Delete</button>'
          + '</div>'
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
  focusModal(document.getElementById('templateModal'));
  refreshPreviewTenantOptions('tPreviewTenant', null);
  renderTemplatePreview();
}

function closeTemplateModal() {
  document.getElementById('templateModal').classList.remove('open');
  unfocusModal();
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
      await api('PATCH', `/api/announcement-templates/${id}`, payload, false, announcementsTenantSlug());
      toast('Template updated');
    } else {
      await api('POST', '/api/announcement-templates', payload, false, announcementsTenantSlug());
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
    await api('DELETE', `/api/announcement-templates/${id}`, null, false, announcementsTenantSlug());
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
    await api('POST', '/api/announcement-templates', payload, false, announcementsTenantSlug());
    toast(`Template "${name}" saved`);
    loadAnnouncementTemplates();
  } catch (e) { toast(e.message, true); }
}

async function loadAnnouncements() {
  renderAllianceFilterSelect('announcementsFilter', 'announcements', loadAnnouncements);
  try {
    const items = await api('GET', '/api/announcements', null, false, getTabFilter('announcements'));
    ANNOUNCEMENTS = items;
    const tbody = document.getElementById('announcementsBody');
    if (!items.length) {
      tbody.innerHTML = '<tr class="pf-v6-c-table__tr"><td class="pf-v6-c-table__td" colspan="7" style="color:var(--muted);padding:20px">No announcements yet. Click &quot;+ New Announcement&quot; to schedule one.</td></tr>';
      return;
    }
    const deletableStatuses = ['posted', 'failed', 'cancelled'];

    tbody.innerHTML = items.map(a => {
      // Spec §49 — Alliance column, matching events.js's own scopeLabel:
      // the owning tenant's name/color dot, or a Kingdom-wide badge naming
      // the owning alliance whose Kingdom this is labeled for — scope is a
      // display/ownership label only for an announcement (delivery is
      // entirely governed by its explicit AnnouncementTarget list either
      // way), so this alliance neither posts nor fans out anything on its
      // own.
      const allianceLabel = a.scope === 'kingdom-wide'
        ? '🌐 Kingdom-wide <span style="color:var(--muted);font-size:var(--fs-sm)">(via ' + escapeHtml(a.owning_tenant_name || tenantName(a.owning_tenant_id)) + '’s Kingdom)</span>'
        : '<span class="cat-dot" style="background:' + (TENANT_COLORS[a.owning_tenant_id] || '#475569') + '"></span>' + escapeHtml(a.owning_tenant_name || tenantName(a.owning_tenant_id));
      // A cancelled announcement's targets never got a real post attempt
      // past that point, so showing their pre-cancel post_status (usually
      // a stale "pending") is misleading — the announcement-level status
      // is what actually governs them once cancelled. Spec §49 — each
      // target now also names its Discord server and channel (not just
      // the alliance), same data-notif-channel deferred-resolution pattern
      // events.js's Notification Targets column uses.
      const targetsHtml = a.targets.map(t => {
        // AnnouncementTarget.post_status (pending/posted/error) uses the
        // exact same three underlying meanings as an Occurrence's
        // pending/posted/error, so occurrenceStatusBadge's mapping
        // (dashboard.js) applies unchanged — same word/color everywhere.
        const displayStatus = a.status === 'cancelled' ? 'cancelled' : t.post_status;
        const targetTenant = TENANTS.find(x => x.id === t.tenant_id);
        const slug = targetTenant ? targetTenant.slug : '';
        const serverName = targetTenant && targetTenant.server_name
          ? ' <span style="color:var(--muted);font-size:var(--fs-xs)">(' + escapeHtml(targetTenant.server_name) + ')</span>'
          : '';
        const channelSpan = '<span data-notif-channel="' + escapeHtml(slug) + ':' + escapeHtml(t.discord_channel_id) + '">#' + escapeHtml(t.discord_channel_id) + '</span>';
        const label = escapeHtml(tenantName(t.tenant_id)) + serverName + ': ' + channelSpan + ' ' + occurrenceStatusBadge(displayStatus);
        return t.status_detail && a.status !== 'cancelled'
          ? '<div title="' + escapeHtml(t.status_detail) + '">' + label + '</div>'
          : '<div>' + label + '</div>';
      }).join('');
      // Spec §49 — editing in place is only offered while still
      // 'scheduled', matching the backend's own restriction (once posted
      // the message already went out; once failed/cancelled the row is
      // terminal) — same condition cancelBtn already uses.
      const editBtn = a.status === 'scheduled'
        ? '<button class="pf-v6-c-button pf-m-secondary pf-m-small" onclick="editAnnouncement(' + a.id + ')" title="Edit this announcement in place">Edit</button>'
        : '';
      const cancelBtn = a.status === 'scheduled'
        ? '<button class="pf-v6-c-button pf-m-danger pf-m-small" onclick="cancelAnnouncement(' + a.id + ')">Cancel</button>'
        : '';
      const deleteBtn = deletableStatuses.includes(a.status)
        ? '<button class="pf-v6-c-button pf-m-danger pf-m-small" onclick="deleteAnnouncement(' + a.id + ')" title="Remove this finished announcement from the list">Delete</button>'
        : '';
      const duplicateBtn = '<button class="pf-v6-c-button pf-m-secondary pf-m-small" onclick="duplicateAnnouncement(' + a.id + ')" title="Open a new announcement pre-filled with this one\'s title, body, and targets">Duplicate</button>';
      // Requeues errored targets for the next delivery tick (spec §30) —
      // only shown when there's actually a failed target to retry, and
      // never for a cancelled announcement (retrying would un-cancel it).
      const hasFailedTargets = a.status !== 'cancelled' && a.targets.some(t => t.post_status === 'error');
      const retryBtn = hasFailedTargets
        ? '<button class="pf-v6-c-button pf-m-secondary pf-m-small" onclick="retryFailedTargets(' + a.id + ')" title="Requeue only the failed target(s) for delivery on the next scheduler tick">Retry Failed</button>'
        : '';
      const leadershipBadge = a.leadership_only
        ? ' <span title="Leadership only">👑</span>'
        : ' <span title="General">🛡️</span>';
      const recurringBadge = a.recurring
        ? pfLabel('Every ' + a.interval_days + 'd', 'pf-m-purple')
        : pfLabel('One-time', 'pf-m-gray');
      const previewData = escapeHtml(JSON.stringify(a));
      return '<tr class="pf-v6-c-table__tr samaya-row-clickable" onclick="handleRowPreviewClick(event,\'announcement\',' + previewData + ')" title="Click to preview how this looks on Discord">'
        + '<td class="pf-v6-c-table__td">' + escapeHtml(a.title) + leadershipBadge + '</td>'
        + '<td class="pf-v6-c-table__td">' + allianceLabel + '</td>'
        + '<td class="pf-v6-c-table__td">' + recurringBadge + '</td>'
        + '<td class="pf-v6-c-table__td">' + fmtDateTime(a.scheduled_for) + '</td>'
        + '<td class="pf-v6-c-table__td">' + announcementStatusBadgeAdmin(a.status) + '</td>'
        + '<td class="pf-v6-c-table__td">' + targetsHtml + '</td>'
        + '<td class="pf-v6-c-table__td"><div style="display:flex;gap:6px;flex-wrap:wrap">' + [editBtn, cancelBtn, retryBtn, deleteBtn, duplicateBtn].filter(Boolean).join('') + '</div></td>'
        + '</tr>';
    }).join('');
    enhanceNotificationTargetLabels();
  } catch (e) { toast(e.message, true); }
}

function updateAnnouncementCharCount() {
  document.getElementById('aBodyCount').textContent = document.getElementById('aBody').value.length;
  renderAnnouncementPreview();
}

function toggleAnnouncementRecurringNote() {
  const on = document.getElementById('aRecurring').checked;
  document.getElementById('aIntervalGroup').classList.toggle('hidden', !on);
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
  row.querySelector('.target-channel-fallback').addEventListener('input', () => row.classList.remove('target-row-invalid'));
  const select = row.querySelector('.target-tenant');
  select.addEventListener('change', () => {
    populateAnnouncementTargetChannels(row, select.value);
    refreshPreviewTenantOptions('aPreviewTenant', 'aTargetsList');
    renderAnnouncementPreview();
  });
  // channelId is only ever passed explicitly for a real stored target
  // (editing/duplicating an announcement) — a brand-new row has none, so
  // fall back to whatever channel was last used for this tenant (spec §29).
  const initialTenantSlug = tenantSlug || select.value;
  populateAnnouncementTargetChannels(row, initialTenantSlug, channelId || getLastChannelForTenant(initialTenantSlug));
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
    select.addEventListener('change', () => {
      setLastChannelForTenant(tenantSlug, select.value);
      row.classList.remove('target-row-invalid');
    });
    if (select.value) setLastChannelForTenant(tenantSlug, select.value);
  } catch (e) {
    select.style.display = 'none';
    fallback.style.display = '';
    fallback.value = channelId || '';
    // Surface the server's actual reason (403 = no access to that tenant,
    // 502 = the tenant's own Discord server/bot rejected the call) rather
    // than a single generic line — the two failure modes need entirely
    // different fixes (get access granted vs. fix that server's bot
    // config), and hiding which one it is just moves the confusion from
    // here to a support conversation.
    toast(`Could not load Discord channels for ${tenantSlug} (${e.message}) — enter the channel ID manually`, true);
  }
}

// "+ Add all alliances" (spec §29) — the one-click version of adding a
// target row for every alliance not already targeted, since a kingdom-wide
// announcement otherwise means picking tenant+channel one row at a time
// for every alliance. Each new row still defaults to that alliance's own
// last-used channel (or "— none —" if it's never been posted to before),
// same as adding a single row would.
function addAllAllianceTargets() {
  const existingSlugs = Array.from(document.querySelectorAll('#aTargetsList .target-tenant')).map(s => s.value);
  const missing = TENANTS.filter(t => !existingSlugs.includes(t.slug));
  if (!missing.length) {
    toast('Every alliance already has a target row');
    return;
  }
  missing.forEach(t => addAnnouncementTargetRow(t.slug));
}

function announcementTargetChannelValue(row) {
  const select = row.querySelector('.target-channel');
  const fallback = row.querySelector('.target-channel-fallback');
  return select.style.display !== 'none' ? select.value : fallback.value;
}

// source, when passed, pre-fills every field. For a duplicate (isEdit
// falsy) the send date/time is left blank — a duplicate always needs a
// fresh future schedule, never the original's (which is either already
// past, or the very thing that got cancelled/failed). For an edit
// (isEdit true, spec §49) the existing schedule is shown too, since
// there's a real row being modified in place rather than a fresh copy.
function openAnnouncementModal(source, isEdit) {
  document.getElementById('aAnnouncementId').value = isEdit && source ? source.id : '';
  document.getElementById('aTitle').value = source ? source.title : '';
  document.getElementById('aBody').value = source ? source.body_markdown : '';
  updateAnnouncementCharCount();
  if (isEdit && source) {
    const d = new Date(source.scheduled_for);
    document.getElementById('aScheduledDate').value = new Intl.DateTimeFormat('en-CA', { timeZone: 'UTC', year: 'numeric', month: '2-digit', day: '2-digit' }).format(d);
    document.getElementById('aScheduledTime').value = new Intl.DateTimeFormat('en-GB', { timeZone: 'UTC', hour: '2-digit', minute: '2-digit', hour12: false }).format(d);
  } else {
    document.getElementById('aScheduledDate').value = '';
    document.getElementById('aScheduledTime').value = '';
  }
  document.getElementById('aRecurring').checked = !!(source && source.recurring);
  document.getElementById('aIntervalDays').value = (source && source.interval_days) ? source.interval_days : '';
  document.getElementById('aIntervalGroup').classList.toggle('hidden', !(source && source.recurring));
  document.getElementById('aLeadershipOnly').checked = !!(source && source.leadership_only);
  document.getElementById('aEventOffsetMinutes').value = (source && source.event_offset_minutes) ? source.event_offset_minutes : 0;
  // Spec §38.3/§49 — an announcement is owned by one alliance, or (like
  // Events) that alliance's kingdom-wide option, via the same combined
  // selector events.js uses; there's no more ambient "current tenant" to
  // imply which one, so this always shows an explicit picker, defaulting
  // to the source's own owning alliance/scope when editing/duplicating/
  // using a template-derived source, or the Announcements tab's own
  // filter otherwise.
  const ownerDefault = (source && source.owning_tenant_slug) || announcementsTenantSlug();
  const scopeDefault = (source && source.scope) || 'alliance';
  renderOwningTenantScopeSelect('aOwningTenant', ownerDefault, scopeDefault);
  document.getElementById('aTargetsList').innerHTML = '';
  if (source && source.targets && source.targets.length) {
    source.targets.forEach(t => addAnnouncementTargetRow(tenantSlugFor(t.tenant_id), t.discord_channel_id));
  } else {
    addAnnouncementTargetRow(ownerDefault);
  }
  document.querySelector('#announcementModalTitle .pf-v6-c-modal-box__title-text').textContent =
    isEdit ? 'Edit Announcement' : (source ? 'Duplicate Announcement' : 'New Announcement');
  document.getElementById('announcementModal').classList.add('open');
  focusModal(document.getElementById('announcementModal'));
  renderAnnouncementPreview();
}

function closeAnnouncementModal() {
  document.getElementById('announcementModal').classList.remove('open');
  unfocusModal();
}

function duplicateAnnouncement(id) {
  const source = ANNOUNCEMENTS.find(a => a.id === id);
  if (!source) return;
  openAnnouncementModal(source, false);
}

function editAnnouncement(id) {
  const source = ANNOUNCEMENTS.find(a => a.id === id);
  if (!source) return;
  openAnnouncementModal(source, true);
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
  const targetRows = Array.from(document.querySelectorAll('#aTargetsList .announcement-target-row'));
  const targets = targetRows.map(row => ({
    tenant_slug: row.querySelector('.target-tenant').value,
    discord_channel_id: announcementTargetChannelValue(row).trim(),
  }));
  // Flag exactly which row(s) are missing a channel rather than leaving
  // the coordinator to guess — a brand-new target row defaults to
  // "— none —" until a channel is actively picked (spec §29), which is
  // easy to miss since nothing about the row itself looks wrong.
  targetRows.forEach((row, i) => row.classList.toggle('target-row-invalid', !targets[i].discord_channel_id));
  if (!targets.length || targets.some(t => !t.discord_channel_id)) {
    toast('Every target needs a Discord channel selected — the highlighted row(s) don\'t have one yet', true);
    const firstInvalid = document.querySelector('#aTargetsList .target-row-invalid');
    if (firstInvalid) firstInvalid.scrollIntoView({ block: 'center', behavior: 'smooth' });
    return;
  }
  targetRows.forEach(row => row.classList.remove('target-row-invalid'));

  const owningSelection = parseOwningTenantScopeValue(document.getElementById('aOwningTenant').value);
  const payload = {
    title,
    body_markdown: body,
    scheduled_for: scheduledDate + 'T' + scheduledTime + ':00+00:00', // explicit UTC offset, matching the "Send Date/Time (UTC)" field labels
    targets,
    scope: owningSelection.scope,
    leadership_only: document.getElementById('aLeadershipOnly').checked,
    recurring,
    interval_days: recurring ? parseInt(intervalRaw) : null,
    event_offset_minutes: parseInt(document.getElementById('aEventOffsetMinutes').value, 10) || 0,
  };

  // Spec §49 — editing sends the header as the announcement's ORIGINAL
  // owning tenant (update_announcement looks the row up by that, and
  // needs it to authorize a reassignment) and the modal's (possibly
  // different) selection as owning_tenant_slug, same split saveEvent()
  // uses for events.
  const id = document.getElementById('aAnnouncementId').value;
  let owningTenantOverride;
  if (id) {
    owningTenantOverride = announcementOwningSlug(parseInt(id, 10));
    payload.owning_tenant_slug = owningSelection.slug;
  } else {
    owningTenantOverride = owningSelection.slug;
  }

  try {
    if (id) {
      await api('PATCH', `/api/announcements/${id}`, payload, false, owningTenantOverride);
      toast('Announcement updated');
    } else {
      await api('POST', '/api/announcements', payload, false, owningTenantOverride);
      toast('Announcement scheduled');
    }
    closeAnnouncementModal();
    loadAnnouncements();
  } catch (e) { toast(e.message, true); }
}

// Spec §38.3 — an announcement's owning alliance is fixed once created;
// these three actions resolve it from ANNOUNCEMENTS' own cached
// owning_tenant_slug (populated by list_announcements' combined-mode
// response) rather than any ambient/current tenant, since the row being
// acted on may belong to a different alliance than whatever this tab's
// filter currently shows.
function announcementOwningSlug(id) {
  const a = ANNOUNCEMENTS.find(x => x.id === id);
  return a ? a.owning_tenant_slug : undefined;
}

async function cancelAnnouncement(id) {
  if (!confirm('Cancel this announcement? It will not be posted.')) return;
  try {
    await api('POST', `/api/announcements/${id}/cancel`, null, false, announcementOwningSlug(id));
    toast('Announcement cancelled');
    loadAnnouncements();
  } catch (e) { toast(e.message, true); }
}

async function retryFailedTargets(id) {
  try {
    await api('POST', `/api/announcements/${id}/retry-failed-targets`, null, false, announcementOwningSlug(id));
    toast('Failed target(s) requeued — will retry on the next delivery tick');
    loadAnnouncements();
  } catch (e) { toast(e.message, true); }
}

async function deleteAnnouncement(id) {
  if (!confirm('Delete this announcement permanently? This cannot be undone.')) return;
  try {
    await api('DELETE', `/api/announcements/${id}`, null, false, announcementOwningSlug(id));
    toast('Announcement deleted');
    loadAnnouncements();
  } catch (e) { toast(e.message, true); }
}

// Wire the Announcement and Template modals' markdown toolbars once at
// load (see common.js's wireMarkdownToolbar) — same reasoning as events.js's
// own call for the Event modal's toolbar.
wireMarkdownToolbar('aBodyToolbar');
wireMarkdownToolbar('tBodyToolbar');

// Wire the Announcements view and its Announcement/Template modals' own
// controls — replaces their onclick/onchange/oninput attributes (Phase 3
// audit remediation).
document.getElementById('btnOpenTemplateModal')?.addEventListener('click', () => openTemplateModal());
document.getElementById('btnOpenAnnouncementModal')?.addEventListener('click', () => openAnnouncementModal());
document.getElementById('aBody')?.addEventListener('input', () => updateAnnouncementCharCount());
document.getElementById('aPreviewTenant')?.addEventListener('change', () => renderAnnouncementPreview());
document.getElementById('aScheduledDate')?.addEventListener('change', () => renderAnnouncementPreview());
document.getElementById('aScheduledTime')?.addEventListener('input', () => renderAnnouncementPreview());
document.getElementById('aEventOffsetMinutes')?.addEventListener('input', () => renderAnnouncementPreview());
document.getElementById('aRecurring')?.addEventListener('change', () => toggleAnnouncementRecurringNote());
document.getElementById('btnAddAnnouncementTarget')?.addEventListener('click', () => addAnnouncementTargetRow());
document.getElementById('btnAddAllAllianceTargets')?.addEventListener('click', () => addAllAllianceTargets());
document.getElementById('btnSaveAnnouncement')?.addEventListener('click', () => saveAnnouncement());
document.getElementById('btnSaveAsTemplate')?.addEventListener('click', () => saveCurrentAsTemplate());
document.getElementById('tBody')?.addEventListener('input', () => renderTemplatePreview());
document.getElementById('tPreviewTenant')?.addEventListener('change', () => renderTemplatePreview());
document.getElementById('tEventOffsetMinutes')?.addEventListener('input', () => renderTemplatePreview());
document.getElementById('btnSaveTemplate')?.addEventListener('click', () => saveTemplate());
