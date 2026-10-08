// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { beforeEach, describe, expect, it } from 'vitest';

const read = (name) => readFileSync(resolve(process.cwd(), 'app/static/js', name), 'utf8');
const common = read('common.js');
const bindSource = common.slice(common.indexOf('function bindActions'), common.indexOf('// ── API'));
const full = read('signup.js');
const source = full.slice(full.indexOf('function createSignupRoles'), full.indexOf('// Event form helper'));

let calls;
const flush = () => new Promise((r) => setTimeout(r, 0));
function load() {
  const api = async (method, path) => { calls.push(`${method} ${path}`); return []; };
  const names = ['api', 'toast', 'escapeHtml', 'pfLabel', 'byId', 'optionsHtml', 'document'];
  const stubs = [api, () => {}, (s) => String(s), (s) => s, (id) => document.getElementById(id), () => '', document];
  return new Function(...names, `${bindSource}\n${source}\nreturn { createSignupRoles };`)(...stubs);
}

function open(createSignupRoles, host, id) {
  return createSignupRoles(host, {
    scope: 'event', id, slug: 'mod', canWrite: true,
    async load() { return { rows: [], warnings: [], servers: [{ id: 1, name: 'Main' }] }; },
  });
}

beforeEach(() => {
  calls = [];
  document.body.innerHTML = '<div id="host"></div>';
});

describe('Notify me role rows on a reused host', () => {
  it('one click acts on the event that is open, not on events opened before', async () => {
    const { createSignupRoles } = load();
    const host = document.getElementById('host');
    open(createSignupRoles, host, 11);
    await flush();
    host.innerHTML = '';
    open(createSignupRoles, host, 22);
    await flush();
    host.querySelector('[data-action="create"]').click();
    await flush();
    const puts = calls.filter((c) => c.startsWith('PUT'));
    expect(puts).toEqual(['PUT /api/events/22/signup-roles']);
  });

  it('a form that was replaced does not draw over the new one', async () => {
    const { createSignupRoles } = load();
    const host = document.getElementById('host');
    let release;
    const slow = createSignupRoles(host, {
      scope: 'event', id: 1, slug: 'mod', canWrite: true,
      load: () => new Promise((resolve) => { release = () => resolve({ rows: [], warnings: [], servers: [{ id: 1, name: 'OLD' }] }); }),
    });
    open(createSignupRoles, host, 2);
    await flush();
    release();
    await flush();
    expect(slow).toBeTruthy();
    expect(host.textContent).toContain('Main');
    expect(host.textContent).not.toContain('OLD');
  });
});
