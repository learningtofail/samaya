// Notify me and I'm in setup (spec §87): the per-server role rows shared by the
// Events form and the Event types tab, and the attendance groups panel.
// Depends on common.js. Loaded before types.js, which opens the two modals.
// Counts shown here are advisory and for leaders only; no Discord IDs of
// players ever reach this file.

const SIGNUP_WINDOW_LABEL = { none: 'No limit', day: 'One event per day', week: 'One event per week (Monday start, UTC)' };
const SGF = { editing: null, saving: false, slug: '' };
let SIGNUP_GROUPS = [];
const SIGNUP_SERVERS = {};

async function loadSignupServers(slug) {
  if (!SIGNUP_SERVERS[slug]) SIGNUP_SERVERS[slug] = await api('GET', '/api/kingdom-servers', null, false, slug);
  return SIGNUP_SERVERS[slug];
}

async function loadSignupGroups(slug) {
  SIGNUP_GROUPS = await api('GET', '/api/signup-groups', null, false, slug);
  return SIGNUP_GROUPS;
}

function signupGroupRule(g) {
  const bits = [];
  if (g.exclusive_roles) bits.push('One Notify me role at a time');
  bits.push(SIGNUP_WINDOW_LABEL[g.attendance_window] || g.attendance_window);
  return bits.join(', ');
}

// ── Role rows ────────────────────────────────────────────────
// opts: { scope: 'event'|'type', id, slug, canWrite, load() -> {rows, warnings, servers} }
// rows are mapping views from the API: one per server, role_id null when unset.
function createSignupRoles(host, opts) {
  const state = { rows: [], warnings: [], servers: [], picking: null, busy: false, message: '' };
  const base = opts.scope === 'event' ? `/api/events/${opts.id}/signup-roles` : `/api/event-types/${opts.id}/signup-roles`;

  function rowHtml(srv) {
    const row = state.rows.find((r) => r.server_id === srv.id) || { server_id: srv.id, server_name: srv.name, role_id: null };
    const inherited = opts.scope === 'event' && row.role_id && row.scope === 'type';
    const status = row.role_id
      ? `<strong>${escapeHtml(row.role_name)}</strong> ${row.created_by_samaya ? pfLabel('Created by Samaya', 'pf-m-blue') : pfLabel('Linked', 'pf-m-gray')}${inherited ? ' ' + pfLabel('From the event type', 'pf-m-purple') : ''}
         <div class="samaya-muted">${row.subscribers} subscriber${row.subscribers === 1 ? '' : 's'} (advisory, counted from Notify me taps)</div>`
      : '<span class="samaya-muted">No Notify me role</span>';
    const error = row.last_error ? `<div class="form-errors" role="alert">Last Discord error: ${escapeHtml(row.last_error)}</div>` : '';
    let buttons = '';
    if (opts.canWrite) {
      const ds = `data-server="${srv.id}"`;
      const own = !inherited;
      buttons = `<div class="row-actions">
        <button type="button" class="pf-v6-c-button pf-m-secondary pf-m-small" data-action="pick" ${ds}>${row.role_id ? 'Use a different role' : 'Choose a role'}</button>
        <button type="button" class="pf-v6-c-button pf-m-secondary pf-m-small" data-action="create" ${ds}>Create role</button>
        ${opts.scope === 'event' ? `<button type="button" class="pf-v6-c-button pf-m-secondary pf-m-small" data-action="find" ${ds}>Find roles named like the event</button>` : ''}
        ${opts.scope === 'event' && row.role_id && row.created_by_samaya && own ? `<button type="button" class="pf-v6-c-button pf-m-secondary pf-m-small" data-action="sync" ${ds}>Sync name</button>` : ''}
        ${row.role_id && own ? `<button type="button" class="pf-v6-c-button pf-m-link pf-m-danger pf-m-small" data-action="remove" ${ds}>Remove</button>` : ''}
      </div>`;
    }
    const picker = state.picking && state.picking.serverId === srv.id ? pickerHtml() : '';
    return `<li class="audience__item">
      <div class="audience__name">${escapeHtml(srv.name)}</div>
      ${status}${error}${buttons}${picker}
    </li>`;
  }

  function pickerHtml() {
    const p = state.picking;
    if (p.loading) return '<p class="samaya-muted" role="status">Loading roles from Discord&hellip;</p>';
    if (!p.roles.length) return `<p class="samaya-muted">${escapeHtml(p.empty || 'No roles to choose from.')}</p>`;
    const options = p.roles.map((r) =>
      `<option value="${escapeHtml(r.id)}"${r.unsafe_reason ? ' disabled' : ''}>${escapeHtml(r.name)}${r.unsafe_reason ? ' (unavailable: ' + escapeHtml(r.unsafe_reason) + ')' : ''}</option>`).join('');
    return `<div class="signup-picker">
      <label class="pf-v6-c-form__label" for="sgPick${p.serverId}"><span class="pf-v6-c-form__label-text">Role</span></label>
      <select class="pf-v6-c-form-control" id="sgPick${p.serverId}"><option value="">Select a role</option>${options}</select>
      <div class="row-actions">
        <button type="button" class="pf-v6-c-button pf-m-primary pf-m-small" data-action="link" data-server="${p.serverId}">Use this role</button>
        <button type="button" class="pf-v6-c-button pf-m-link pf-m-small" data-action="cancel-pick">Cancel</button>
      </div>
    </div>`;
  }

  function render() {
    const warnings = state.warnings.length
      ? `<ul class="form-errors" role="status">${state.warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join('')}</ul>` : '';
    const msg = state.message ? `<p class="samaya-muted" role="status">${escapeHtml(state.message)}</p>` : '';
    host.innerHTML = state.servers.length
      ? `${warnings}${msg}<ul class="audience__list">${state.servers.map(rowHtml).join('')}</ul>`
      : '<p class="samaya-muted">This Kingdom has no Discord servers yet.</p>';
  }

  async function refresh() {
    try {
      const data = await opts.load();
      state.rows = data.rows;
      state.warnings = data.warnings || [];
      state.servers = data.servers;
    } catch (e) {
      state.message = e.message;
      toast(e.message, true);
    }
    render();
  }

  async function put(body, done) {
    if (state.busy) return;
    state.busy = true;
    try {
      await api('PUT', base, body, false, opts.slug);
      state.picking = null;
      state.message = done;
      await refresh();
    } catch (e) {
      state.message = e.message;
      toast(e.message, true);
      render();
    } finally {
      state.busy = false;
    }
  }

  async function openPicker(serverId, loader, empty) {
    state.picking = { serverId, loading: true, roles: [] };
    render();
    try {
      const res = await loader();
      state.picking = { serverId, loading: false, roles: res.roles, empty };
    } catch (e) {
      state.picking = null;
      state.message = e.message;
      toast(e.message, true);
    }
    render();
  }

  bindActions(host, {
    pick(el) {
      const serverId = parseInt(el.dataset.server, 10);
      openPicker(serverId, () => api('GET', `/api/signup-roles/server-roles?server_id=${serverId}`, null, false, opts.slug), 'No roles to choose from.');
    },
    find(el) {
      const serverId = parseInt(el.dataset.server, 10);
      openPicker(serverId, () => api('POST', `/api/events/${opts.id}/signup-roles/find`, { server_id: serverId }, false, opts.slug),
        'No unused role has exactly the event name in that server.');
    },
    link(el) {
      const serverId = parseInt(el.dataset.server, 10);
      const select = byId('sgPick' + serverId);
      if (!select || !select.value) { state.message = 'Select a role first.'; render(); return; }
      put({ server_id: serverId, role_id: select.value }, 'Role linked.');
    },
    create(el) {
      const serverId = parseInt(el.dataset.server, 10);
      put({ server_id: serverId, create: true }, 'Role created, named after the event.');
    },
    async sync(el) {
      const serverId = parseInt(el.dataset.server, 10);
      try {
        await api('POST', `/api/events/${opts.id}/signup-roles/sync-name`, { server_id: serverId }, false, opts.slug);
        state.message = 'Role renamed to match the event.';
        await refresh();
      } catch (e) { toast(e.message, true); }
    },
    remove(el) {
      const serverId = parseInt(el.dataset.server, 10);
      if (!confirm('Remove the Notify me role from Samaya? The Discord role itself is not deleted, and its subscriber list here is cleared.')) return;
      put({ server_id: serverId, remove: true }, 'Role removed from Samaya. The Discord role still exists.');
    },
    'cancel-pick'() { state.picking = null; render(); },
  });

  refresh();
  return { refresh };
}

// Event form helper: an edited event's own role rows.
function createEventSignupRoles(host, event, canWrite) {
  const slug = writeSlugForEvent(event);
  return createSignupRoles(host, {
    scope: 'event', id: event.id, slug, canWrite,
    async load() {
      const [body, servers] = await Promise.all([api('GET', `/api/events/${event.id}`, null, false, slug), loadSignupServers(slug)]);
      return { rows: body.signup_roles || [], warnings: body.signup_warnings || [], servers };
    },
  });
}

// Event type helper: the type's own roles on every Kingdom server.
function createTypeSignupRoles(host, type, slug, canWrite) {
  return createSignupRoles(host, {
    scope: 'type', id: type.id, slug, canWrite,
    async load() {
      const [types, servers] = await Promise.all([api('GET', '/api/event-types', null, false, slug), loadSignupServers(slug)]);
      const fresh = types.find((t) => t.id === type.id) || type;
      return { rows: fresh.signup_roles || [], warnings: [], servers };
    },
  });
}

// ── Attendance groups ────────────────────────────────────────
function groupSelectHtml(selectedId) {
  const options = [{ value: '', label: 'No group' }].concat(SIGNUP_GROUPS.map((g) => ({ value: g.id, label: g.name })));
  return optionsHtml(options, selectedId == null ? '' : selectedId);
}

function buildGroupRow(g, canWrite) {
  const events = g.events.length
    ? g.events.map((e) => escapeHtml(e.name) + (e.active ? '' : ' (inactive)')).join(', ')
    : '<span class="samaya-muted">No events yet</span>';
  const actions = canWrite
    ? `<div class="row-actions">
        <button type="button" class="pf-v6-c-button pf-m-secondary pf-m-small" data-action="edit" data-id="${g.id}">Edit</button>
        <details class="menu"><summary class="menu__btn" aria-label="More actions for ${escapeHtml(g.name)}">&#8943;</summary><div class="menu__panel"><button type="button" class="menu__item menu__item--danger" data-action="delete" data-id="${g.id}">Delete&hellip;</button></div></details>
      </div>`
    : '<span class="samaya-muted">Read only</span>';
  return `<tr class="pf-v6-c-table__tr">
    <td class="pf-v6-c-table__td" data-label="Group"><strong>${escapeHtml(g.name)}</strong></td>
    <td class="pf-v6-c-table__td" data-label="Rule">${escapeHtml(signupGroupRule(g))}</td>
    <td class="pf-v6-c-table__td" data-label="Events">${events}</td>
    <td class="pf-v6-c-table__td" data-label="Actions">${actions}</td>
  </tr>`;
}

function renderSignupGroups(slug, canWrite) {
  SGF.slug = slug;
  byId('btnNewGroup').classList.toggle('hidden', !canWrite);
  byId('groupsBody').innerHTML = SIGNUP_GROUPS.length
    ? SIGNUP_GROUPS.map((g) => buildGroupRow(g, canWrite)).join('')
    : emptyRow(4, 'No attendance groups. Add one when events exclude each other.');
}

function showGroupErrors(messages) {
  const box = byId('sgErrors');
  box.innerHTML = messages.length ? '<ul>' + messages.map((m) => `<li>${escapeHtml(m)}</li>`).join('') + '</ul>' : '';
  box.classList.toggle('hidden', !messages.length);
}

function openGroupModal(g) {
  SGF.editing = g || null;
  byId('groupModalTitle').textContent = g ? 'Edit attendance group' : 'New attendance group';
  byId('sgName').value = g ? g.name : '';
  byId('sgExclusive').checked = g ? g.exclusive_roles : true;
  byId('sgWindow').value = g ? g.attendance_window : 'none';
  showGroupErrors([]);
  openModalById('groupModal');
}

function closeGroupModal() {
  closeModalById('groupModal');
}

async function saveGroup() {
  if (SGF.saving) return;
  const name = byId('sgName').value.trim();
  if (!name) { showGroupErrors(['Name is required.']); return; }
  const payload = { name, exclusive_roles: byId('sgExclusive').checked, attendance_window: byId('sgWindow').value };
  SGF.saving = true;
  byId('btnSaveGroup').disabled = true;
  try {
    if (SGF.editing) await api('PATCH', `/api/signup-groups/${SGF.editing.id}`, payload, false, SGF.slug);
    else await api('POST', '/api/signup-groups', payload, false, SGF.slug);
    closeGroupModal();
    toast(SGF.editing ? 'Group saved.' : 'Group created.');
    await loadSignupGroups(SGF.slug);
    renderSignupGroups(SGF.slug, canWriteAnywhere());
  } catch (e) {
    showGroupErrors([e.message]);
  } finally {
    SGF.saving = false;
    byId('btnSaveGroup').disabled = false;
  }
}

byId('btnNewGroup').addEventListener('click', () => openGroupModal(null));
byId('btnSaveGroup').addEventListener('click', saveGroup);
byId('groupForm').addEventListener('submit', (e) => { e.preventDefault(); saveGroup(); });

bindActions(byId('groupsBody'), {
  edit(btn) {
    const g = SIGNUP_GROUPS.find((x) => x.id === parseInt(btn.dataset.id, 10));
    if (g) openGroupModal(g);
  },
  async delete(btn) {
    const g = SIGNUP_GROUPS.find((x) => x.id === parseInt(btn.dataset.id, 10));
    if (!g || !confirm(`Delete the group "${g.name}"? Its events stay, and stop excluding each other.`)) return;
    try {
      await api('DELETE', `/api/signup-groups/${g.id}`, null, false, SGF.slug);
      toast('Group deleted.');
      await loadSignupGroups(SGF.slug);
      renderSignupGroups(SGF.slug, canWriteAnywhere());
    } catch (e) { toast(e.message, true); }
  },
});
