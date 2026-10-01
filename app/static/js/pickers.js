// Discord channel and role pickers, used by the destination form on the Setup
// tab (spec §67). Depends on common.js.
//
// Each picker is a <select> filled from GET /api/discord/channels or
// /api/discord/roles for one alliance and one of its servers (the primary, or
// a secondary when `serverId` is given). If Discord cannot be reached (no bot
// token, network, permissions) the select is replaced by a plain text input
// for the numeric ID, so a value can always be typed in.

const _DISCORD_LIST_CACHE = { channel: {}, role: {} };
const _DISCORD_OK_TTL_MS = 5 * 60 * 1000;
const _DISCORD_FAIL_TTL_MS = 60 * 1000;

// Resolves to { items: [{id, name, ...}], error: string|null }. Never rejects.
function loadDiscordList(kind, slug, serverId) {
  const cache = _DISCORD_LIST_CACHE[kind];
  const cacheKey = serverId ? `${slug}#${serverId}` : slug;
  const hit = cache[cacheKey];
  if (hit && Date.now() - hit.at < (hit.failed ? _DISCORD_FAIL_TTL_MS : _DISCORD_OK_TTL_MS)) return hit.promise;
  const base = kind === 'channel' ? '/api/discord/channels' : '/api/discord/roles';
  const path = serverId ? `${base}?server_id=${encodeURIComponent(serverId)}` : base;
  const entry = { at: Date.now(), failed: false, promise: null };
  entry.promise = api('GET', path, null, false, slug)
    .then((items) => ({ items, error: null }))
    .catch((e) => { entry.failed = true; return { items: [], error: e.message }; });
  cache[cacheKey] = entry;
  return entry.promise;
}

function clearDiscordListCache() {
  _DISCORD_LIST_CACHE.channel = {};
  _DISCORD_LIST_CACHE.role = {};
}

function _channelLabel(c) {
  return '#' + c.name;
}

function _roleLabel(r) {
  return '@' + r.name;
}

// Renders a labelled picker into `host` and returns a controller:
//   value()    the chosen ID ('' for "none")
//   setValue(v)
// opts: { kind: 'channel'|'role', slug, serverId, id, label, value, emptyLabel, helper }
function mountDiscordPicker(host, opts) {
  const kind = opts.kind;
  const id = opts.id;
  const noun = kind === 'channel' ? 'channel' : 'role';
  host.innerHTML = `
    <div class="picker">
      <label class="pf-v6-c-form__label" for="${escapeHtml(id)}"><span class="pf-v6-c-form__label-text">${escapeHtml(opts.label)}</span></label>
      <select class="pf-v6-c-form-control" id="${escapeHtml(id)}" disabled>
        <option value="">Loading ${noun}s...</option>
      </select>
      <input class="pf-v6-c-form-control hidden" type="text" inputmode="numeric" pattern="[0-9]*" autocomplete="off"
             id="${escapeHtml(id)}Text" placeholder="Discord ${noun} ID (digits only)" aria-label="${escapeHtml(opts.label)} (ID)">
      <p class="picker__note hidden" id="${escapeHtml(id)}Note"></p>
      ${opts.helper ? `<p class="picker__helper">${escapeHtml(opts.helper)}</p>` : ''}
    </div>`;
  const select = host.querySelector('select');
  const text = host.querySelector('input');
  const note = host.querySelector('.picker__note');
  let current = opts.value || '';
  let usingText = false;
  let loaded = false;

  function useText(reason) {
    usingText = true;
    select.classList.add('hidden');
    text.classList.remove('hidden');
    text.value = current;
    note.textContent = `Could not load the ${noun} list from Discord (${reason}). Enter the ${noun} ID instead.`;
    note.classList.remove('hidden');
  }

  select.addEventListener('change', () => { current = select.value; });
  text.addEventListener('input', () => { current = text.value.trim(); });

  loadDiscordList(kind, opts.slug, opts.serverId).then(({ items, error }) => {
    loaded = true;
    if (error) { useText(error); return; }
    const options = [{ value: '', label: opts.emptyLabel || 'None' }]
      .concat(items.map((x) => ({ value: x.id, label: kind === 'channel' ? _channelLabel(x) : _roleLabel(x) })));
    if (current && !items.some((x) => String(x.id) === String(current))) {
      options.push({ value: current, label: `${current} (not found, kept as is)` });
    }
    select.innerHTML = optionsHtml(options, current);
    select.disabled = false;
  });

  return {
    value() { return usingText ? text.value.trim() : current; },
    setValue(v) {
      current = v || '';
      if (usingText) text.value = current;
      else if (loaded) {
        if (current && !Array.from(select.options).some((o) => o.value === current)) {
          select.insertAdjacentHTML('beforeend', `<option value="${escapeHtml(current)}">${escapeHtml(current)} (not found, kept as is)</option>`);
        }
        select.value = current;
      }
    },
  };
}
