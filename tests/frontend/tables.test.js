// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { beforeEach, describe, expect, it } from 'vitest';

globalThis.__SAMAYA_TEST__ = true;
const source = readFileSync(resolve(process.cwd(), 'app/static/js/tables.js'), 'utf8');
const load = () => new Function('document', 'localStorage', `${source}; return TABLE_TOOLS;`)(document, window.localStorage);

const tick = () => new Promise((r) => setTimeout(r, 0));
const HTML = `<section id="v-test"><h2>Test</h2><div class="table-wrap">
  <table class="pf-v6-c-table" id="t1"><thead><tr><th>Name</th><th>Count</th><th>When</th><th><span class="sr-only">Actions</span></th></tr></thead>
  <tbody id="body"><tr><td>b</td><td>10</td><td data-sort="2026-10-02">x</td><td></td></tr>
  <tr><td>a</td><td>9</td><td data-sort="2026-10-03">y</td><td></td></tr>
  <tr><td>c</td><td></td><td data-sort="2026-10-01">z</td><td></td></tr></tbody></table></div></section>`;

const names = () => Array.from(document.querySelectorAll('#body tr')).map((r) => r.cells[0].textContent);
let tools;
beforeEach(() => {
  localStorage.clear();
  document.body.innerHTML = HTML;
  tools = load();
  tools.enhanceAll(document);
});

describe('comparison', () => {
  it('sorts numbers by value, text naturally and empty cells last', () => {
    expect(['10', '9', '', '100'].sort(tools.compareValues)).toEqual(['9', '10', '100', '']);
    expect(['Bear #10', 'Bear #2'].sort(tools.compareValues)).toEqual(['Bear #2', 'Bear #10']);
  });
});

describe('sorting', () => {
  it('adds a sort button to text headers only', () => {
    expect(document.querySelectorAll('.sort-btn').length).toBe(3);
  });

  it('sorts ascending then descending and sets aria-sort', () => {
    const [name] = document.querySelectorAll('.sort-btn');
    name.click();
    expect(names()).toEqual(['a', 'b', 'c']);
    expect(name.parentElement.getAttribute('aria-sort')).toBe('ascending');
    name.click();
    expect(names()).toEqual(['c', 'b', 'a']);
    expect(name.parentElement.getAttribute('aria-sort')).toBe('descending');
  });

  it('keeps empty cells last in both directions', async () => {
    const count = document.querySelectorAll('.sort-btn')[1];
    count.click();
    expect(names()).toEqual(['a', 'b', 'c']);
    count.click();
    expect(names()).toEqual(['b', 'a', 'c']);
    await tick();
  });

  it('uses data-sort over the visible text', () => {
    document.querySelectorAll('.sort-btn')[2].click();
    expect(names()).toEqual(['c', 'b', 'a']);
  });

  it('sorts again after the body is redrawn', async () => {
    document.querySelectorAll('.sort-btn')[0].click();
    document.getElementById('body').innerHTML = '<tr><td>z</td><td>1</td><td></td><td></td></tr><tr><td>m</td><td>2</td><td></td><td></td></tr>';
    await tick();
    expect(names()).toEqual(['m', 'z']);
  });

  it('leaves a placeholder row alone', async () => {
    document.querySelectorAll('.sort-btn')[0].click();
    document.getElementById('body').innerHTML = '<tr><td colspan="4">No events</td></tr>';
    await tick();
    expect(document.querySelectorAll('#body tr').length).toBe(1);
  });

  it('remembers the sort', () => {
    document.querySelectorAll('.sort-btn')[0].click();
    document.body.innerHTML = HTML;
    load().enhanceAll(document);
    expect(names()).toEqual(['a', 'b', 'c']);
  });

  it('does not decorate a table marked data-nosort', () => {
    document.body.innerHTML = HTML.replace('id="t1"', 'id="t2" data-nosort');
    load().enhanceAll(document);
    expect(document.querySelectorAll('.sort-btn').length).toBe(0);
  });
});

describe('collapsing', () => {
  it('adds one toggle that hides and shows the table and remembers it', () => {
    const toggle = document.querySelector('.table-toggle');
    const region = document.querySelector('.table-wrap');
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    toggle.click();
    expect(region.classList.contains('table--collapsed')).toBe(true);
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(toggle.getAttribute('aria-controls')).toBe(region.id);
    document.body.innerHTML = HTML;
    load().enhanceAll(document);
    expect(document.querySelector('.table-wrap').classList.contains('table--collapsed')).toBe(true);
    document.querySelector('.table-toggle').click();
    expect(document.querySelector('.table-wrap').classList.contains('table--collapsed')).toBe(false);
  });

  it('does not enhance a table twice', () => {
    tools.enhanceAll(document);
    expect(document.querySelectorAll('.table-toggle').length).toBe(1);
    expect(document.querySelectorAll('.sort-btn').length).toBe(3);
  });

  it('enhances a table added later', async () => {
    tools.start();
    document.body.insertAdjacentHTML('beforeend', '<div><table class="pf-v6-c-table" id="late"><thead><tr><th>X</th></tr></thead><tbody></tbody></table></div>');
    await tick();
    expect(document.querySelector('#late').previousElementSibling.classList.contains('table-toggle')).toBe(true);
  });
});
