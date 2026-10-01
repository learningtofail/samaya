// Message composer: markdown toolbar, emoji picker, role-mention insert, the
// six placeholders and a live "as it would look on Discord" preview (spec §28,
// §34, kept by §66). Moved out of announcements.js when announcements were
// folded into events (spec §66). Depends on common.js and pickers.js.
//
// createComposer(host, opts) renders one composer into `host`, so the event
// message, a per-alliance override and an occurrence's own message all use the
// same code. The pure helpers below (placeholder rendering and the markdown
// renderer) are plain globals.

const COMPOSER_MAX_CHARS = 2000;

const MD_TOOLBAR_COMMANDS = {
  bold:      (id) => wrapSelection(id, '**', '**', 'bold'),
  italic:    (id) => wrapSelection(id, '*', '*', 'italic'),
  underline: (id) => wrapSelection(id, '__', '__', 'underline'),
  strike:    (id) => wrapSelection(id, '~~', '~~', 'strike'),
  spoiler:   (id) => wrapSelection(id, '||', '||', 'spoiler'),
  heading:   (id) => prefixSelectedLines(id, '# '),
  quote:     (id) => prefixSelectedLines(id, '> '),
  code:      (id) => wrapSelection(id, '`', '`', 'code'),
  codeblock: (id) => insertCodeBlock(id),
};

const MD_TOOLBAR_BUTTONS = [
  ['bold', '<strong>B</strong>', 'Bold'],
  ['italic', '<em>I</em>', 'Italic'],
  ['underline', '<u>U</u>', 'Underline'],
  ['strike', '<s>S</s>', 'Strikethrough'],
  ['spoiler', '||', 'Spoiler'],
  ['heading', 'H', 'Heading'],
  ['quote', '&gt;', 'Quote'],
  ['code', '&lt;/&gt;', 'Inline code'],
  ['codeblock', '{ }', 'Code block'],
];

// ── Text editing commands (operate on a textarea by id) ──────

// Wraps the selection in `before`/`after`. With nothing selected, inserts both
// markers around `placeholder` and selects it so typing replaces it.
function wrapSelection(textareaId, before, after, placeholder) {
  const ta = document.getElementById(textareaId);
  if (!ta) return;
  const start = ta.selectionStart;
  const end = ta.selectionEnd;
  const text = ta.value.slice(start, end) || placeholder || '';
  ta.value = ta.value.slice(0, start) + before + text + after + ta.value.slice(end);
  ta.focus();
  ta.setSelectionRange(start + before.length, start + before.length + text.length);
  ta.dispatchEvent(new Event('input', { bubbles: true }));
}

// A line-level prefix (heading, quote) applied to every line the selection touches.
function prefixSelectedLines(textareaId, prefix) {
  const ta = document.getElementById(textareaId);
  if (!ta) return;
  const start = ta.selectionStart;
  const end = ta.selectionEnd;
  const lineStart = ta.value.lastIndexOf('\n', start - 1) + 1;
  let lineEnd = ta.value.indexOf('\n', end);
  if (lineEnd === -1) lineEnd = ta.value.length;
  const prefixed = ta.value.slice(lineStart, lineEnd).split('\n').map((l) => prefix + l).join('\n');
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
  ta.value = ta.value.slice(0, start) + '```\n' + selected + '\n```' + ta.value.slice(end);
  ta.focus();
  ta.setSelectionRange(start + 4, start + 4 + selected.length);
  ta.dispatchEvent(new Event('input', { bubbles: true }));
}

// Inserts text at the cursor (replacing any selection), fires `input` so the
// counter and preview update, and puts the cursor right after the text.
function insertAtCursor(textareaId, text) {
  const ta = document.getElementById(textareaId);
  if (!ta) return;
  const start = ta.selectionStart;
  const end = ta.selectionEnd;
  ta.value = ta.value.slice(0, start) + text + ta.value.slice(end);
  const cursor = start + text.length;
  ta.focus();
  ta.setSelectionRange(cursor, cursor);
  ta.dispatchEvent(new Event('input', { bubbles: true }));
}

// ── Emoji picker ─────────────────────────────────────────────
// Discord's default Unicode emoji only, never a server's custom ones.
const EMOJI_PICKER_LIST = {
  'Faces': ['😀', '😁', '😂', '🤣', '😊', '😇', '🙂', '😉', '😍', '🤩', '😎', '🤔', '😐', '😴', '😭', '😡', '🤯', '🥳', '😅', '🤗'],
  'Gestures': ['👍', '👎', '👏', '🙌', '🤝', '🙏', '💪', '✌️', '🤞', '👋', '🫡', '🤙', '👀', '🖐️', '☝️'],
  'Symbols': ['🔥', '⭐', '✨', '💯', '⚔️', '🛡️', '🏆', '⚠️', '✅', '❌', '❗', '❓', '⏰', '📅', '📢', '🔔', '💀', '👑', '🎉', '🚨'],
};

// One shared popover, retargeted per call. Inside an open modal the popover is
// appended to that modal's box so it stays above the backdrop.
function toggleEmojiPicker(buttonEl, textareaId) {
  let picker = document.getElementById('emojiPicker');
  const sameTarget = picker && picker.classList.contains('open') && picker.dataset.targetTextarea === textareaId;
  if (!picker) {
    picker = document.createElement('div');
    picker.id = 'emojiPicker';
    picker.className = 'emoji-picker';
    picker.setAttribute('role', 'dialog');
    picker.setAttribute('aria-label', 'Emoji');
    document.body.appendChild(picker);
    picker.addEventListener('click', (e) => {
      const item = e.target.closest('.emoji-picker__item');
      if (!item) return;
      insertAtCursor(picker.dataset.targetTextarea, item.dataset.emoji);
      picker.classList.remove('open');
    });
    document.addEventListener('click', (e) => {
      if (!picker.contains(e.target) && !e.target.closest('[data-md-cmd="emoji"]')) picker.classList.remove('open');
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && picker.classList.contains('open')) {
        picker.classList.remove('open');
        e.stopPropagation();
      }
    }, true);
  }
  if (sameTarget) { picker.classList.remove('open'); return; }
  picker.dataset.targetTextarea = textareaId;
  picker.innerHTML = Object.entries(EMOJI_PICKER_LIST).map(([heading, emojis]) =>
    `<div class="emoji-picker__heading">${heading}</div><div class="emoji-picker__grid">`
    + emojis.map((e) => `<button type="button" class="emoji-picker__item" data-emoji="${e}" aria-label="Insert ${e}">${e}</button>`).join('')
    + '</div>'
  ).join('');
  const rect = buttonEl.getBoundingClientRect();
  const left = Math.max(8, Math.min(rect.left, window.innerWidth - 276));
  picker.style.top = (rect.bottom + 4) + 'px';
  picker.style.left = left + 'px';
  picker.classList.add('open');
  const first = picker.querySelector('.emoji-picker__item');
  if (first) first.focus();
}

// ── Placeholders and the markdown preview ────────────────────

function formatDiscordAbsolutePreview(date) {
  return date.toLocaleString(undefined, {
    weekday: 'long', year: 'numeric', month: 'long', day: 'numeric', hour: 'numeric', minute: '2-digit',
  }) + ' (preview)';
}

function formatDiscordRelativePreview(date) {
  return formatRelativeTime(date) + ' (preview)';
}

// Browser-side port of services/templates.py's render_placeholders(). It shows
// readable approximations rather than literal <t:UNIX:F> tags, since the
// preview cannot reproduce each Discord viewer's own locale.
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

// Covers the Discord markdown spec §27 documents, plus role/channel mention
// names for the preview only (the stored text is always the literal markdown).
// The raw input is HTML-escaped first so nothing typed can inject markup.
function renderDiscordMarkdownPreview(text, { roles, channels }) {
  let html = escapeHtml(text);

  // Code first, stashed behind tokens so markdown inside it stays literal.
  const codeStash = [];
  html = html.replace(/```([\s\S]*?)```/g, (m, code) => {
    codeStash.push('<pre class="preview-codeblock"><code>' + code.replace(/^\n/, '') + '</code></pre>');
    return `\uE000CODEBLOCK${codeStash.length - 1}\uE000`;
  });
  html = html.replace(/`([^`\n]+)`/g, (m, code) => {
    codeStash.push('<code class="preview-inline-code">' + code + '</code>');
    return `\uE000CODEBLOCK${codeStash.length - 1}\uE000`;
  });

  html = html.replace(/^(?:&gt; ?.*(?:\n|$))+/gm, (m) =>
    '<blockquote class="preview-quote">' + m.replace(/^&gt; ?/gm, '').trim() + '</blockquote>');

  // The trailing \n? eats the heading's own line break so the later
  // newline-to-<br> pass does not add a gap under it.
  html = html.replace(/^### (.*)\n?/gm, '<h3 class="preview-h">$1</h3>');
  html = html.replace(/^## (.*)\n?/gm, '<h2 class="preview-h">$1</h2>');
  html = html.replace(/^# (.*)\n?/gm, '<h1 class="preview-h">$1</h1>');

  html = html.replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>');
  html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/__([^_]+)__/g, '<u>$1</u>');
  html = html.replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g, '<em>$1</em>');
  html = html.replace(/~~([^~]+)~~/g, '<s>$1</s>');
  html = html.replace(/\|\|([^|]+)\|\|/g, '<span class="preview-spoiler" tabindex="0" role="button" aria-label="Spoiler, activate to reveal">$1</span>');

  // HTML escaping turned <@&123> into &lt;@&amp;123&gt; and <#123> into &lt;#123&gt;.
  html = html.replace(/&lt;@&amp;(\d+)&gt;/g, (m, id) => {
    const role = (roles || []).find((r) => String(r.id) === id);
    return '<span class="preview-mention">@' + (role ? escapeHtml(role.name) : 'role') + '</span>';
  });
  html = html.replace(/&lt;#(\d+)&gt;/g, (m, id) => {
    const channel = (channels || []).find((c) => String(c.id) === id);
    return '<span class="preview-mention">#' + (channel ? escapeHtml(channel.name) : 'channel') + '</span>';
  });

  html = html.replace(/^- (.*)$/gm, '&nbsp;&nbsp;&bull; $1');
  html = html.replace(/\n/g, '<br>');
  html = html.replace(/\uE000CODEBLOCK(\d+)\uE000/g, (m, i) => codeStash[Number(i)]);
  return html || '<span class="samaya-muted">(nothing to preview yet)</span>';
}

// ── The composer component ───────────────────────────────────

// opts:
//   idPrefix        unique id prefix for the generated elements
//   label           visible label for the message field
//   value           initial text
//   rows            textarea rows (default 6)
//   helper          optional helper text under the field
//   previewSlug     () => alliance slug the preview speaks as
//   eventStart      () => Date | null, the occurrence start used for {event_time}
//   emptyNote       text shown when no start time is known yet
// Returns { value(), setValue(v), refresh(), setPreviewSlug(slug), textareaId }.
function createComposer(host, opts) {
  const p = opts.idPrefix;
  const bodyId = p + 'Body';
  const buttons = MD_TOOLBAR_BUTTONS.map(([cmd, html, title]) =>
    `<button type="button" class="md-toolbar__btn" data-md-cmd="${cmd}" title="${title}" aria-label="${title}">${html}</button>`
  ).join('');
  host.innerHTML = `
    <div class="composer">
      <label class="pf-v6-c-form__label" for="${bodyId}"><span class="pf-v6-c-form__label-text">${escapeHtml(opts.label)}</span></label>
      <div class="md-toolbar" id="${p}Toolbar" role="toolbar" aria-label="Formatting for ${escapeHtml(opts.label)}">
        ${buttons}
        <button type="button" class="md-toolbar__btn" data-md-cmd="emoji" title="Emoji" aria-label="Emoji" aria-haspopup="dialog">&#128512;</button>
        <label class="sr-only" for="${p}RoleMention">Insert a role mention</label>
        <select class="pf-v6-c-form-control pf-m-small md-toolbar__select" id="${p}RoleMention"><option value="">Insert role mention...</option></select>
      </div>
      <textarea class="pf-v6-c-form-control" id="${bodyId}" rows="${opts.rows || 6}" maxlength="${COMPOSER_MAX_CHARS}"
                aria-describedby="${p}Count"></textarea>
      <p class="composer__meta"><span id="${p}Count">0</span> / ${COMPOSER_MAX_CHARS} characters.
        Placeholders: <code>{alliance_name}</code> <code>{kingdom_name}</code> <code>{event_time}</code> <code>{event_time_relative}</code> <code>{send_time}</code> <code>{send_time_relative}</code></p>
      ${opts.helper ? `<p class="composer__helper">${escapeHtml(opts.helper)}</p>` : ''}
      <div class="composer__preview-head">
        <span class="pf-v6-c-form__label-text">Preview</span>
        <label class="composer__preview-as" for="${p}PreviewTenant">as</label>
        <select class="pf-v6-c-form-control pf-m-small" id="${p}PreviewTenant"></select>
      </div>
      <div class="discord-preview-pane" id="${p}Preview" aria-live="polite"></div>
    </div>`;

  const ta = document.getElementById(bodyId);
  const countEl = document.getElementById(p + 'Count');
  const preview = document.getElementById(p + 'Preview');
  const tenantSelect = document.getElementById(p + 'PreviewTenant');
  const roleSelect = document.getElementById(p + 'RoleMention');
  const toolbar = document.getElementById(p + 'Toolbar');
  let timer = null;

  ta.value = opts.value || '';
  countEl.textContent = ta.value.length;
  tenantSelect.innerHTML = optionsHtml(TENANTS.map((t) => ({ value: t.slug, label: t.name })),
    (opts.previewSlug && opts.previewSlug()) || (TENANTS[0] && TENANTS[0].slug));

  toolbar.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-md-cmd]');
    if (!btn) return;
    if (btn.dataset.mdCmd === 'emoji') { toggleEmojiPicker(btn, bodyId); return; }
    const fn = MD_TOOLBAR_COMMANDS[btn.dataset.mdCmd];
    if (fn) fn(bodyId);
  });
  roleSelect.addEventListener('change', () => {
    if (!roleSelect.value) return;
    insertAtCursor(bodyId, '<@&' + roleSelect.value + '>');
    roleSelect.value = '';
  });
  preview.addEventListener('click', (e) => {
    const sp = e.target.closest('.preview-spoiler');
    if (sp) sp.classList.add('revealed');
  });
  preview.addEventListener('keydown', (e) => {
    const sp = e.target.closest('.preview-spoiler');
    if (sp && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); sp.classList.add('revealed'); }
  });

  async function render() {
    const slug = tenantSelect.value;
    const tenant = tenantBySlug(slug);
    const now = new Date();
    const start = opts.eventStart ? opts.eventStart() : null;
    const offset = start ? Math.round((start.getTime() - now.getTime()) / 60000) : 0;
    const [kingdomNames, rolesRes, channelsRes] = await Promise.all([
      ensureKingdomNamesLoaded(), loadDiscordList('role', slug), loadDiscordList('channel', slug),
    ]);
    // Keep the role dropdown in step with the alliance being previewed.
    const roleOptions = '<option value="">Insert role mention...</option>'
      + rolesRes.items.map((r) => `<option value="${escapeHtml(r.id)}">@${escapeHtml(r.name)}</option>`).join('');
    if (roleSelect.dataset.slug !== slug || roleSelect.dataset.count !== String(rolesRes.items.length)) {
      roleSelect.innerHTML = roleOptions;
      roleSelect.dataset.slug = slug;
      roleSelect.dataset.count = String(rolesRes.items.length);
    }
    roleSelect.classList.toggle('hidden', !!rolesRes.error);
    const resolved = clientRenderPlaceholders(ta.value, {
      allianceName: tenant ? tenant.name : slug,
      kingdomName: tenant ? kingdomNames[tenant.kingdom_id] : '',
      scheduledFor: now, eventOffsetMinutes: offset,
    });
    const html = renderDiscordMarkdownPreview(resolved, { roles: rolesRes.items, channels: channelsRes.items });
    preview.innerHTML = `
      <div class="discord-preview-msg">
        <div class="discord-preview-avatar" aria-hidden="true">S</div>
        <div class="discord-preview-body">
          <div class="discord-preview-header"><span class="discord-preview-name">Samaya</span><span class="discord-preview-bot-tag">BOT</span></div>
          <div class="discord-preview-text">${html}</div>
        </div>
      </div>
      ${start ? '' : `<p class="discord-preview-note">${escapeHtml(opts.emptyNote || 'No start time set yet, so event time placeholders use the current time.')}</p>`}`;
  }

  function schedule() {
    if (timer) clearTimeout(timer);
    timer = setTimeout(render, 120);
  }

  ta.addEventListener('input', () => {
    countEl.textContent = ta.value.length;
    schedule();
  });
  tenantSelect.addEventListener('change', render);
  render();

  return {
    textareaId: bodyId,
    value() { return ta.value; },
    setValue(v) { ta.value = v || ''; countEl.textContent = ta.value.length; schedule(); },
    refresh: schedule,
    setPreviewSlug(slug) {
      if (slug && Array.from(tenantSelect.options).some((o) => o.value === slug)) {
        tenantSelect.value = slug;
        schedule();
      }
    },
  };
}
