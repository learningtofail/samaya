// Sortable and collapsible tables for the whole admin console (spec §88).
// Progressive enhancement: it finds every `table.pf-v6-c-table`, adds a collapse
// button above it and turns text headers into sort buttons. Tables are drawn by
// other scripts with innerHTML, so a MutationObserver re-applies the sort after
// each redraw. No other script depends on this file.
// Markers: `data-sort` on a cell sorts by that value instead of its text;
// `data-nosort` on a th (or `data-nosort` on the table) turns sorting off;
// `data-nocollapse` on a table turns collapsing off.

const TABLE_TOOLS = (() => {
  const SKIP_HEADS = /^(actions?|)$/i;
  const STORE_PREFIX = 'samaya_table_';
  const enhanced = new WeakSet();
  const states = new WeakMap();

  function store(key, value) {
    try {
      if (value === null) localStorage.removeItem(STORE_PREFIX + key);
      else localStorage.setItem(STORE_PREFIX + key, value);
    } catch (e) { /* storage blocked: the choice lasts until reload */ }
  }
  function recall(key) {
    try { return localStorage.getItem(STORE_PREFIX + key); } catch (e) { return null; }
  }

  // ── Pure helpers ───────────────────────────────────────────
  function cellValue(cell) {
    if (!cell) return '';
    return (cell.dataset.sort !== undefined ? cell.dataset.sort : cell.textContent).replace(/\s+/g, ' ').trim();
  }

  function asNumber(text) {
    const cleaned = text.replace(/[,\s%]/g, '');
    return cleaned !== '' && /^-?\d+(\.\d+)?$/.test(cleaned) ? Number(cleaned) : null;
  }

  // Numbers before text, numbers by value, text by locale with embedded numbers in order, empty cells last.
  function compareValues(a, b) {
    if (a === '' || b === '') return a === b ? 0 : (a === '' ? 1 : -1);
    const na = asNumber(a);
    const nb = asNumber(b);
    if (na !== null && nb !== null) return na - nb;
    return a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' });
  }

  function sortRows(rows, column, direction) {
    const sign = direction === 'desc' ? -1 : 1;
    return rows
      .map((row, index) => ({ row, index, value: cellValue(row.cells[column]) }))
      .sort((x, y) => {
        // Empty cells stay last in both directions.
        if (x.value === '' || y.value === '') return compareValues(x.value, y.value) || x.index - y.index;
        return sign * compareValues(x.value, y.value) || x.index - y.index;
      })
      .map((entry) => entry.row);
  }

  // A body made of one-cell placeholder rows ("Loading", "No events") has nothing to sort.
  function sortableRows(body) {
    const rows = Array.from(body.rows);
    if (rows.length < 2) return null;
    return rows.every((r) => r.cells.length > 1 && !r.querySelector('td[colspan]')) ? rows : null;
  }

  // ── Per-table behaviour ────────────────────────────────────
  function applySort(table) {
    const state = states.get(table);
    if (!state || state.column === null) return;
    state.applying = true;
    for (const body of table.tBodies) {
      const rows = sortableRows(body);
      if (!rows) continue;
      const ordered = sortRows(rows, state.column, state.direction);
      // Touch the DOM only when the order changes, so our own moves cannot trigger another pass.
      if (ordered.every((row, i) => row === rows[i])) continue;
      for (const row of ordered) body.appendChild(row);
    }
    state.applying = false;
  }

  function markHeaders(table) {
    const state = states.get(table);
    table.querySelectorAll('thead th').forEach((th, index) => {
      const button = th.querySelector('.sort-btn');
      if (!button) return;
      const active = state.column === index;
      th.setAttribute('aria-sort', active ? (state.direction === 'desc' ? 'descending' : 'ascending') : 'none');
      button.dataset.direction = active ? state.direction : '';
    });
  }

  function decorateHeaders(table) {
    if (table.hasAttribute('data-nosort')) return;
    const state = states.get(table);
    state.observer.disconnect();
    table.querySelectorAll('thead th').forEach((th, index) => {
      if (th.querySelector('.sort-btn') || th.hasAttribute('data-nosort') || SKIP_HEADS.test(th.textContent.trim())) return;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'sort-btn';
      button.dataset.column = String(index);
      button.append(...th.childNodes);
      th.appendChild(button);
    });
    markHeaders(table);
    observeHeader(table);
  }

  function observeHeader(table) {
    const state = states.get(table);
    const head = table.tHead;
    if (head) state.observer.observe(head, { childList: true, subtree: true });
    for (const body of table.tBodies) state.observer.observe(body, { childList: true });
  }

  function onSortClick(table, button) {
    const state = states.get(table);
    const column = Number(button.dataset.column);
    state.direction = state.column === column && state.direction === 'asc' ? 'desc' : 'asc';
    state.column = column;
    store(state.key + '_sort', `${column}:${state.direction}`);
    applySort(table);
    markHeaders(table);
  }

  function labelFor(table) {
    const caption = table.querySelector('caption');
    if (caption && caption.textContent.trim()) return caption.textContent.trim();
    const ref = table.getAttribute('aria-labelledby');
    const named = ref && document.getElementById(ref);
    if (named) return named.textContent.trim();
    const view = table.closest('section, [role="tabpanel"], .view');
    const heading = view && view.querySelector('h1, h2');
    return heading ? heading.textContent.trim() : 'table';
  }

  function keyFor(table) {
    if (table.id) return table.id;
    const view = table.closest('section[id], .view[id], [role="tabpanel"][id]');
    const scope = view ? view.id : 'page';
    const all = Array.from((view || document).querySelectorAll('table.pf-v6-c-table'));
    return `${scope}_${all.indexOf(table)}`;
  }

  function setCollapsed(table, collapsed) {
    const state = states.get(table);
    const target = state.region;
    target.classList.toggle('table--collapsed', collapsed);
    state.toggle.setAttribute('aria-expanded', String(!collapsed));
    state.toggle.textContent = collapsed ? 'Show table' : 'Hide table';
    state.toggle.setAttribute('aria-label', `${collapsed ? 'Show' : 'Hide'} ${state.label}`);
    store(state.key + '_collapsed', collapsed ? '1' : null);
  }

  function addToggle(table, state) {
    if (table.hasAttribute('data-nocollapse')) return;
    const region = table.closest('.table-wrap') || table;
    state.region = region;
    if (!region.id) region.id = `tableRegion_${state.key}`.replace(/[^A-Za-z0-9_-]/g, '_');
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'table-toggle';
    toggle.setAttribute('aria-controls', region.id);
    toggle.addEventListener('click', () => setCollapsed(table, !region.classList.contains('table--collapsed')));
    state.toggle = toggle;
    region.parentNode.insertBefore(toggle, region);
    setCollapsed(table, recall(state.key + '_collapsed') === '1');
  }

  function enhance(table) {
    if (enhanced.has(table) || table.closest('[data-no-table-tools]')) return;
    enhanced.add(table);
    const state = {
      key: keyFor(table), label: labelFor(table), column: null, direction: 'asc', applying: false, region: null, toggle: null,
      observer: new MutationObserver(() => {
        if (state.applying) return;
        decorateHeaders(table);
        state.observer.disconnect();
        applySort(table);
        observeHeader(table);
      }),
    };
    states.set(table, state);
    addToggle(table, state);
    const saved = (recall(state.key + '_sort') || '').match(/^(\d+):(asc|desc)$/);
    if (saved) { state.column = Number(saved[1]); state.direction = saved[2]; }
    table.addEventListener('click', (e) => {
      const button = e.target.closest('.sort-btn');
      if (button && table.contains(button)) onSortClick(table, button);
    });
    decorateHeaders(table);
    state.observer.disconnect();
    applySort(table);
    observeHeader(table);
  }

  function enhanceAll(root) {
    (root || document).querySelectorAll('table.pf-v6-c-table').forEach(enhance);
  }

  function start() {
    enhanceAll(document);
    // Tables built by other scripts after load (modals, tab content) get the same treatment.
    new MutationObserver((records) => {
      for (const record of records) {
        record.addedNodes.forEach((node) => {
          if (node.nodeType !== 1) return;
          if (node.matches && node.matches('table.pf-v6-c-table')) enhance(node);
          else enhanceAll(node);
        });
      }
    }).observe(document.body, { childList: true, subtree: true });
  }

  return { start, enhance, enhanceAll, compareValues, sortRows, cellValue };
})();

if (!globalThis.__SAMAYA_TEST__) TABLE_TOOLS.start();  // tests call start() themselves
