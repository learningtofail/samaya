// Event types tab (#v-types, spec §66.7): the kinds of event a Kingdom uses
// (Bear Hunt, Arena...), each with a color and defaults that pre-fill the
// event form. Types belong to a Kingdom, so this tab works on one alliance's
// Kingdom at a time. Depends on common.js, composer.js and reminders.js.

let EVENT_TYPES = [];
const TYPEF = { editing: null, composer: null, reminders: null, saving: false };

function typesSlug() {
  const sel = byId('typesKingdom');
  if (sel && sel.value) return sel.value;
  return singleSlugFor('types');
}

// One entry per Kingdom the caller can see; the value is an alliance in it,
// since the API picks the Kingdom from the X-Tenant-Slug alliance.
function renderTypesKingdomSelect() {
  const seen = new Map();
  TENANTS.forEach((t) => { if (!seen.has(t.kingdom_id)) seen.set(t.kingdom_id, t); });
  const group = byId('typesKingdomGroup');
  group.classList.toggle('hidden', seen.size < 2);
  const saved = getTabFilter('types');
  const current = Array.from(seen.values()).find((t) => t.slug === saved) || seen.values().next().value;
  const names = KINGDOM_NAMES_CACHE || {};
  byId('typesKingdom').innerHTML = optionsHtml(
    Array.from(seen.values()).map((t) => ({ value: t.slug, label: names[t.kingdom_id] || ('Kingdom of ' + t.name) })),
    current ? current.slug : '');
}

async function loadTypes() {
  await ensureKingdomNamesLoaded();
  renderTypesKingdomSelect();
  byId('btnNewType').classList.toggle('hidden', !canWriteAnywhere());
  byId('typesViewerNote').classList.toggle('hidden', canWriteAnywhere());
  try {
    EVENT_TYPES = await api('GET', '/api/v2/event-types', null, false, typesSlug());
    renderTypes();
  } catch (e) {
    toast(e.message, true);
    byId('typesBody').innerHTML = emptyRow(6, 'Could not load event types: ' + e.message);
  }
}

function typeDefaultsText(t) {
  const bits = [];
  bits.push(t.default_duration_hours ? `${t.default_duration_hours} h` : 'Message only');
  bits.push(t.default_interval_days ? `every ${t.default_interval_days} d` : 'one-off');
  return bits.join(', ');
}

function buildTypeRow(t) {
  const reminders = (t.default_reminder_minutes || []).length
    ? t.default_reminder_minutes.map((m) => escapeHtml(describeReminder(m))).join(', ')
    : '<span class="samaya-muted">None</span>';
  const actions = canWriteAnywhere()
    ? `<div class="row-actions">
        <button type="button" class="pf-v6-c-button pf-m-secondary pf-m-small" data-action="edit" data-id="${t.id}">Edit</button>
        <button type="button" class="pf-v6-c-button pf-m-danger pf-m-small" data-action="delete" data-id="${t.id}">Delete</button>
      </div>`
    : '<span class="samaya-muted">Read only</span>';
  return `<tr class="pf-v6-c-table__tr">
    <td class="pf-v6-c-table__td" data-label="Type">${typeChip(t)}</td>
    <td class="pf-v6-c-table__td" data-label="Defaults">${escapeHtml(typeDefaultsText(t))}</td>
    <td class="pf-v6-c-table__td" data-label="Reminders">${reminders}</td>
    <td class="pf-v6-c-table__td" data-label="Mentions role">${t.default_mention_role ? 'Yes' : 'No'}</td>
    <td class="pf-v6-c-table__td" data-label="Message">${t.default_message ? escapeHtml(t.default_message.length > 80 ? t.default_message.slice(0, 80) + '...' : t.default_message) : '<span class="samaya-muted">None</span>'}</td>
    <td class="pf-v6-c-table__td" data-label="Actions">${actions}</td>
  </tr>`;
}

function renderTypes() {
  const body = byId('typesBody');
  body.innerHTML = EVENT_TYPES.length ? EVENT_TYPES.map(buildTypeRow).join('') : emptyRow(6, 'No event types yet. Create one to start adding events.');
  applyTypeColors(body);
}

function showTypeErrors(messages) {
  const box = byId('tpErrors');
  box.innerHTML = messages.length ? '<ul>' + messages.map((m) => `<li>${escapeHtml(m)}</li>`).join('') + '</ul>' : '';
  box.classList.toggle('hidden', !messages.length);
}

function openTypeModal(t) {
  TYPEF.editing = t || null;
  byId('typeModalTitle').textContent = t ? 'Edit event type' : 'New event type';
  byId('tpName').value = t ? t.name : '';
  byId('tpColor').value = t ? t.color : '#475569';
  byId('tpDuration').value = t && t.default_duration_hours ? t.default_duration_hours : '';
  byId('tpInterval').value = t && t.default_interval_days ? t.default_interval_days : '';
  byId('tpMentionRole').checked = !!(t && t.default_mention_role);
  byId('tpSort').value = t ? t.sort_order : 0;
  TYPEF.composer = createComposer(byId('tpMessageHost'), {
    idPrefix: 'tpMsg', label: 'Default message', value: t ? t.default_message : '', rows: 4,
    helper: 'Pre-fills the message of new events of this type. Events can change it.',
    previewSlug: typesSlug,
  });
  TYPEF.reminders = createReminderEditor(byId('tpReminderHost'), {
    idPrefix: 'tpRem', value: t ? t.default_reminder_minutes : [],
  });
  showTypeErrors([]);
  openModalById('typeModal');
}

function closeTypeModal() {
  closeModalById('typeModal');
}

async function saveType() {
  if (TYPEF.saving) return;
  const errors = [];
  const name = byId('tpName').value.trim();
  if (!name) errors.push('Name is required.');
  const durationRaw = byId('tpDuration').value.trim();
  const intervalRaw = byId('tpInterval').value.trim();
  const duration = durationRaw === '' ? null : parseFloat(durationRaw);
  const interval = intervalRaw === '' ? null : parseInt(intervalRaw, 10);
  if (duration !== null && !(duration > 0)) errors.push('Default duration must be more than 0 hours, or empty for message-only events.');
  if (interval !== null && !(interval >= 1)) errors.push('Default interval must be at least 1 day, or empty for one-off events.');
  const message = TYPEF.composer.value();
  if (message.length > COMPOSER_MAX_CHARS) errors.push(`The message is over ${COMPOSER_MAX_CHARS} characters.`);
  showTypeErrors(errors);
  if (errors.length) return;
  const payload = {
    name,
    color: byId('tpColor').value,
    default_duration_hours: duration,
    default_interval_days: interval,
    default_message: message,
    default_reminder_minutes: TYPEF.reminders.value(),
    default_mention_role: byId('tpMentionRole').checked,
    sort_order: parseInt(byId('tpSort').value, 10) || 0,
  };
  TYPEF.saving = true;
  byId('btnSaveType').disabled = true;
  try {
    if (TYPEF.editing) await api('PATCH', `/api/v2/event-types/${TYPEF.editing.id}`, payload, false, typesSlug());
    else await api('POST', '/api/v2/event-types', payload, false, typesSlug());
    invalidateEventTypes();
    closeTypeModal();
    toast(TYPEF.editing ? 'Event type saved.' : 'Event type created.');
    loadTypes();
  } catch (e) {
    showTypeErrors([e.message]);
  } finally {
    TYPEF.saving = false;
    byId('btnSaveType').disabled = false;
  }
}

bindActions(byId('typesBody'), {
  edit(btn) {
    const t = EVENT_TYPES.find((x) => x.id === parseInt(btn.dataset.id, 10));
    if (t) openTypeModal(t);
  },
  async delete(btn) {
    const t = EVENT_TYPES.find((x) => x.id === parseInt(btn.dataset.id, 10));
    if (!t || !confirm(`Delete the event type "${t.name}"? This only works when no event uses it.`)) return;
    try {
      await api('DELETE', `/api/v2/event-types/${t.id}`, null, false, typesSlug());
      invalidateEventTypes();
      toast('Event type deleted.');
      loadTypes();
    } catch (e) { toast(e.message, true); }
  },
});
byId('btnNewType').addEventListener('click', () => openTypeModal(null));
byId('btnSaveType').addEventListener('click', saveType);
byId('typesKingdom').addEventListener('change', (e) => { setTabFilter('types', e.target.value); loadTypes(); });
byId('typeForm').addEventListener('submit', (e) => { e.preventDefault(); saveType(); });

VIEW_LOADERS.types = loadTypes;
