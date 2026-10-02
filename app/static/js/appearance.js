// Appearance tab (#v-appearance, superadmin only): public page themes and
// their schedule (spec §71.7). Depends on common.js and platform.js helpers.
// The contrast rules live on the server (services/theme_rules.py); this file
// only shows the ratios live. The server refuses a failing theme regardless.

const APPEARANCE = { kingdom: null, themes: [], windows: [], fonts: [], templates: [], rules: null };
const WHITE_HEX = '#FFFFFF';

function hexLuminance(hex) {
  const n = parseInt(hex.slice(1), 16);
  const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
  return 0.2126 * f(n >> 16) + 0.7152 * f((n >> 8) & 255) + 0.0722 * f(n & 255);
}
function hexContrast(a, b) {
  const la = hexLuminance(a), lb = hexLuminance(b);
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
}
function overlayWorstCase(overlay) {
  const v = Math.round(255 * overlay).toString(16).toUpperCase().padStart(2, '0');
  return '#' + v + v + v;
}

// Each rule: label, colors compared, minimum. Mirrors contrast_failures().
function contrastChecks(values, rules) {
  const hero = overlayWorstCase(values.overlay);
  return [
    ['Page background and body text', values.bg, rules.ink, rules.ink_min],
    ['Page background and secondary text', values.bg, rules.muted, rules.muted_min],
    ['Accent text on white', values.accentText, WHITE_HEX, rules.text_min],
    ['Accent text on the background', values.accentText, values.bg, rules.text_min],
    ['White text on primary buttons', values.primary, WHITE_HEX, rules.text_min],
    ['Body text over a black banner', hero, rules.ink, rules.ink_min],
    ['Secondary text over a black banner', hero, rules.muted, rules.muted_min],
  ].map(([label, a, b, min]) => ({ label, ratio: hexContrast(a, b), min }));
}

function themeFormValues() {
  return {
    bg: byId('thBg').value.toUpperCase(),
    accent: byId('thAccent').value.toUpperCase(),
    accentText: byId('thAccentText').value.toUpperCase(),
    primary: byId('thPrimary').value.toUpperCase(),
    overlay: parseFloat(byId('thOverlay').value),
  };
}

function renderThemeContrast() {
  const v = themeFormValues();
  const checks = contrastChecks(v, APPEARANCE.rules);
  byId('thContrast').innerHTML = checks.map((c) => {
    const ok = c.ratio >= c.min;
    return `<li class="contrast-list__item contrast-list__item--${ok ? 'pass' : 'fail'}">${ok ? 'Pass' : 'Fail'}: ${escapeHtml(c.label)}, ${c.ratio.toFixed(1)}:1 (needs ${c.min}:1)</li>`;
  }).join('');
  byId('btnSaveTheme').disabled = checks.some((c) => c.ratio < c.min);
  byId('thOverlayOut').textContent = v.overlay.toFixed(2);
  const s = byId('thSample');
  s.style.setProperty('--sample-bg', v.bg);
  s.style.setProperty('--sample-accent', v.accent);
  s.style.setProperty('--sample-accent-text', v.accentText);
  s.style.setProperty('--sample-primary', v.primary);
}

function fontOptions(role, selected) {
  const fonts = APPEARANCE.fonts.filter((f) => f.roles.includes(role));
  return '<option value="">Page default</option>' + fonts.map((f) =>
    `<option value="${escapeHtml(f.key)}"${f.key === selected ? ' selected' : ''}>${escapeHtml(f.name)}</option>`).join('');
}

function setThemeForm(t) {
  byId('thId').value = t.id || '';
  byId('thName').value = t.name || '';
  byId('thBg').value = t.bg;
  byId('thAccent').value = t.accent;
  byId('thAccentText').value = t.accent_text;
  byId('thPrimary').value = t.primary;
  byId('thOverlay').value = String(t.banner_overlay);
  byId('thFontHeading').innerHTML = fontOptions('heading', t.font_heading);
  byId('thFontBody').innerHTML = fontOptions('body', t.font_body);
  byId('thFontNumerals').innerHTML = fontOptions('numerals', t.font_numerals);
  renderThemeContrast();
}

function showThemePreview(id) {
  const frame = byId('thFrame');
  if (!id) {
    frame.classList.add('hidden');
    frame.removeAttribute('src');
    byId('thPreviewHelp').classList.remove('hidden');
    return;
  }
  byId('thPreviewHelp').classList.add('hidden');
  frame.classList.remove('hidden');
  frame.src = `/events?preview_theme=${id}&_=${Date.now()}`;
}

function openThemeModal(theme) {
  const t = theme || { ...APPEARANCE.templates[0], banner_overlay: APPEARANCE.rules.overlay_default, font_heading: null, font_body: null, font_numerals: null };
  byId('themeModalTitle').textContent = theme ? 'Edit theme' : 'New theme';
  byId('thTemplate').innerHTML = '<option value="">Choose a template</option>' + APPEARANCE.templates.map((x) =>
    `<option value="${escapeHtml(x.key)}">${escapeHtml(x.name)}</option>`).join('');
  setThemeForm(theme ? t : { ...t, name: '' });
  showThemePreview(theme ? theme.id : null);
  openModalById('themeModal');
}

function closeThemeModal() {
  showThemePreview(null);
  closeModalById('themeModal');
}

byId('thTemplate').addEventListener('change', (e) => {
  const tpl = APPEARANCE.templates.find((x) => x.key === e.target.value);
  if (!tpl) return;
  byId('thBg').value = tpl.bg;
  byId('thAccent').value = tpl.accent;
  byId('thAccentText').value = tpl.accent_text;
  byId('thPrimary').value = tpl.primary;
  if (!byId('thName').value.trim()) byId('thName').value = tpl.name;
  renderThemeContrast();
});
['thBg', 'thAccent', 'thAccentText', 'thPrimary', 'thOverlay'].forEach((id) => byId(id).addEventListener('input', renderThemeContrast));

async function saveTheme() {
  const v = themeFormValues();
  const id = byId('thId').value;
  const payload = {
    name: byId('thName').value.trim(), bg: v.bg, accent: v.accent, accent_text: v.accentText, primary: v.primary,
    font_heading: byId('thFontHeading').value || null, font_body: byId('thFontBody').value || null,
    font_numerals: byId('thFontNumerals').value || null, banner_overlay: v.overlay,
  };
  if (!payload.name) { toast('Name is required', true); return; }
  try {
    const saved = id
      ? await api('PATCH', `/api/themes/${id}`, payload, true)
      : await api('POST', '/api/themes', { ...payload, kingdom_id: APPEARANCE.kingdom.id }, true);
    toast('Theme saved');
    byId('thId').value = saved.id;
    byId('themeModalTitle').textContent = 'Edit theme';
    showThemePreview(saved.id);
    loadAppearance();
  } catch (e) { toast(e.message, true); }
}
byId('btnSaveTheme').addEventListener('click', saveTheme);
byId('btnNewTheme').addEventListener('click', () => openThemeModal(null));

function swatches(t) {
  return [t.bg, t.accent, t.accent_text, t.primary].map((c) => `<span class="swatch" data-color="${escapeHtml(c)}" title="${escapeHtml(c)}"></span>`).join('');
}

function fontNames(t) {
  const name = (k) => (APPEARANCE.fonts.find((f) => f.key === k) || {}).name;
  const parts = [name(t.font_heading) && 'Heading ' + name(t.font_heading), name(t.font_body) && 'Body ' + name(t.font_body), name(t.font_numerals) && 'Numbers ' + name(t.font_numerals)].filter(Boolean);
  return parts.length ? escapeHtml(parts.join(', ')) : '<span class="samaya-muted">Page defaults</span>';
}

function buildThemeRow(t) {
  const use = [t.is_base ? 'Base theme' : '', t.scheduled_count ? `${t.scheduled_count} window(s)` : '', t.archived ? 'Archived' : ''].filter(Boolean).join(', ');
  return `<tr class="pf-v6-c-table__tr">
    ${platformCell('Theme', escapeHtml(t.name))}
    ${platformCell('Colors', swatches(t))}
    ${platformCell('Fonts', fontNames(t))}
    ${platformCell('In use', use ? escapeHtml(use) : '<span class="samaya-muted">Not used</span>')}
    ${platformCell('Actions', actionButton('Edit', 'edit', { id: t.id }) + ' ' + actionButton(t.archived ? 'Restore' : 'Archive', 'archive', { id: t.id, archived: t.archived ? '' : '1' }) + ' ' + actionButton('Delete', 'delete', { id: t.id, name: t.name }, 'danger'))}
  </tr>`;
}

function paintSwatches(root) {
  root.querySelectorAll('.swatch[data-color]').forEach((el) => el.style.setProperty('--swatch', el.dataset.color));
}

bindActions(byId('themesBody'), {
  edit(btn) {
    const t = APPEARANCE.themes.find((x) => x.id === parseInt(btn.dataset.id, 10));
    if (t) openThemeModal(t);
  },
  async archive(btn) {
    try {
      await api('PATCH', `/api/themes/${btn.dataset.id}`, { archived: !!btn.dataset.archived }, true);
      loadAppearance();
    } catch (e) { toast(e.message, true); }
  },
  async delete(btn) {
    if (!confirm(`Delete the theme "${btn.dataset.name}"? This cannot be undone.`)) return;
    try {
      await api('DELETE', `/api/themes/${btn.dataset.id}`, null, true);
      toast('Theme deleted');
      loadAppearance();
    } catch (e) { toast(e.message, true); }
  },
});

// ── Schedule ────────────────────────────────────────────────
function themeName(id) {
  const t = APPEARANCE.themes.find((x) => x.id === id);
  return t ? t.name : '#' + id;
}

function renderBaseTheme() {
  const k = APPEARANCE.kingdom;
  const usable = APPEARANCE.themes.filter((t) => !t.archived || t.id === k.default_theme_id);
  byId('baseTheme').innerHTML = '<option value="">Shipped defaults</option>' + usable.map((t) =>
    `<option value="${t.id}"${t.id === k.default_theme_id ? ' selected' : ''}>${escapeHtml(t.name)}</option>`).join('');
}

byId('baseTheme').addEventListener('change', async (e) => {
  const value = e.target.value ? parseInt(e.target.value, 10) : null;
  try {
    APPEARANCE.kingdom = await api('PATCH', `/api/kingdoms/${APPEARANCE.kingdom.id}`, { default_theme_id: value }, true);
    toast('Base theme updated');
    loadAppearance();
  } catch (err) { toast(err.message, true); renderBaseTheme(); }
});

function utcInput(iso) { return iso ? iso.slice(0, 16) : ''; }

function buildWindowRow(w) {
  const warn = w.overlaps_same_priority.length ? ` <span class="samaya-muted">Overlaps another window at this priority</span>` : '';
  return `<tr class="pf-v6-c-table__tr">
    ${platformCell('Theme', escapeHtml(themeName(w.theme_id)))}
    ${platformCell('Starts (UTC)', escapeHtml(w.start_utc.slice(0, 16).replace('T', ' ')))}
    ${platformCell('Ends (UTC)', escapeHtml(w.end_utc.slice(0, 16).replace('T', ' ')))}
    ${platformCell('Priority', escapeHtml(String(w.priority_level)) + warn)}
    ${platformCell('Actions', actionButton('Edit', 'edit', { id: w.id }) + ' ' + actionButton('Remove', 'remove', { id: w.id }, 'danger'))}
  </tr>`;
}

function openWindowModal(w) {
  const usable = APPEARANCE.themes.filter((t) => !t.archived || (w && t.id === w.theme_id));
  if (!usable.length) { toast('Create a theme first', true); return; }
  byId('windowModalTitle').textContent = w ? 'Edit scheduled theme' : 'Schedule a theme';
  byId('wnId').value = w ? w.id : '';
  byId('wnTheme').innerHTML = usable.map((t) => `<option value="${t.id}"${w && t.id === w.theme_id ? ' selected' : ''}>${escapeHtml(t.name)}</option>`).join('');
  byId('wnStart').value = w ? utcInput(w.start_utc) : '';
  byId('wnEnd').value = w ? utcInput(w.end_utc) : '';
  byId('wnPriority').value = w ? w.priority_level : 50;
  openModalById('windowModal');
}

function closeWindowModal() { closeModalById('windowModal'); }

async function saveWindow() {
  const id = byId('wnId').value;
  if (!byId('wnStart').value || !byId('wnEnd').value) { toast('Start and end are required', true); return; }
  const payload = {
    theme_id: parseInt(byId('wnTheme').value, 10),
    start_utc: byId('wnStart').value + ':00Z', end_utc: byId('wnEnd').value + ':00Z',
    priority_level: parseInt(byId('wnPriority').value, 10) || 0,
  };
  try {
    if (id) await api('PATCH', `/api/scheduled-themes/${id}`, payload, true);
    else await api('POST', '/api/scheduled-themes', { ...payload, kingdom_id: APPEARANCE.kingdom.id }, true);
    toast('Schedule saved');
    closeWindowModal();
    loadAppearance();
  } catch (e) { toast(e.message, true); }
}
byId('btnSaveWindow').addEventListener('click', saveWindow);
byId('btnNewWindow').addEventListener('click', () => openWindowModal(null));

bindActions(byId('windowsBody'), {
  edit(btn) {
    const w = APPEARANCE.windows.find((x) => x.id === parseInt(btn.dataset.id, 10));
    if (w) openWindowModal(w);
  },
  async remove(btn) {
    if (!confirm('Remove this scheduled window?')) return;
    try {
      await api('DELETE', `/api/scheduled-themes/${btn.dataset.id}`, null, true);
      loadAppearance();
    } catch (e) { toast(e.message, true); }
  },
});

async function loadAppearance() {
  if (!isSuperadmin()) return;
  try {
    const [kingdoms, fonts, templates, rules] = APPEARANCE.rules
      ? [await api('GET', '/api/kingdoms', null, true), APPEARANCE.fonts, APPEARANCE.templates, APPEARANCE.rules]
      : await Promise.all([api('GET', '/api/kingdoms', null, true), api('GET', '/api/fonts', null, true),
        api('GET', '/api/theme-templates', null, true), api('GET', '/api/theme-rules', null, true)]);
    Object.assign(APPEARANCE, { kingdom: kingdoms[0], fonts, templates, rules });
    if (!APPEARANCE.kingdom) { byId('themesBody').innerHTML = emptyRow(5, 'Create a Kingdom first.'); return; }
    const id = APPEARANCE.kingdom.id;
    [APPEARANCE.themes, APPEARANCE.windows] = await Promise.all([
      api('GET', `/api/themes?kingdom_id=${id}`, null, true), api('GET', `/api/scheduled-themes?kingdom_id=${id}`, null, true)]);
    byId('themesBody').innerHTML = APPEARANCE.themes.length ? APPEARANCE.themes.map(buildThemeRow).join('') : emptyRow(5, 'No themes yet. The public page uses the shipped look.');
    byId('windowsBody').innerHTML = APPEARANCE.windows.length ? APPEARANCE.windows.map(buildWindowRow).join('') : emptyRow(5, 'Nothing scheduled.');
    paintSwatches(byId('themesBody'));
    renderBaseTheme();
  } catch (e) {
    toast(e.message, true);
    byId('themesBody').innerHTML = emptyRow(5, 'Could not load themes: ' + e.message);
  }
}

VIEW_LOADERS.appearance = loadAppearance;
