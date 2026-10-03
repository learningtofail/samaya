// @vitest-environment jsdom
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { beforeEach, describe, expect, it } from 'vitest';

const source = readFileSync(resolve(process.cwd(), 'app/static/js/reminders.js'), 'utf8');
const escapeHtml = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const toasts = [];
const load = new Function('describeReminder', 'escapeHtml', 'toast', 'COMPOSER_MAX_CHARS', 'document',
  `${source}; return { createReminderEditor };`);
const { createReminderEditor } = load((m) => (m === 0 ? 'at the start' : `${m} min before`), escapeHtml,
  (message) => toasts.push(message), 2000, document);

let host;
beforeEach(() => {
  toasts.length = 0;
  document.body.innerHTML = '<div id="host"></div>';
  host = document.getElementById('host');
});

const box = (m) => document.getElementById(`r${'Msg'}${m}`);
const type = (m, text) => { const b = box(m); b.value = text; b.dispatchEvent(new Event('input', { bubbles: true })); };

describe('reminder editor without messages (event type form)', () => {
  it('renders no message boxes and returns no messages', () => {
    const editor = createReminderEditor(host, { idPrefix: 'r', value: [60, 0] });
    expect(host.querySelector('textarea')).toBeNull();
    expect(editor.messages()).toEqual({});
    expect(editor.value()).toEqual([60, 0]);
  });
});

describe('reminder editor with messages (event form)', () => {
  it('shows one box per reminder, filled from the saved messages', () => {
    createReminderEditor(host, { idPrefix: 'r', value: [60, 0], withMessages: true, messages: { 60: 'One hour', 0: 'Now' } });
    expect(box(60).value).toBe('One hour');
    expect(box(0).value).toBe('Now');
    expect(host.querySelectorAll('textarea')).toHaveLength(2);
  });

  it('accepts string keys, as the API sends them', () => {
    const editor = createReminderEditor(host, { idPrefix: 'r', value: [60], withMessages: true, messages: { '60': 'x' } });
    expect(editor.messages()).toEqual({ 60: 'x' });
  });

  it('keeps typed text when another reminder is added', () => {
    const editor = createReminderEditor(host, { idPrefix: 'r', value: [60], withMessages: true });
    type(60, 'Typed text');
    host.querySelector('button[data-minutes="0"]').click();
    expect(box(60).value).toBe('Typed text');
    expect(editor.value()).toEqual([60, 0]);
    expect(editor.messages()).toEqual({ 60: 'Typed text' });
  });

  it('drops the text of a removed reminder, and it does not come back when re-added', () => {
    const editor = createReminderEditor(host, { idPrefix: 'r', value: [60, 0], withMessages: true, messages: { 0: 'Now' } });
    host.querySelector('button[data-remove="0"]').click();
    expect(editor.messages()).toEqual({});
    host.querySelector('button[data-minutes="0"]').click();
    expect(box(0).value).toBe('');
  });

  it('leaves blank and whitespace-only boxes out of messages()', () => {
    const editor = createReminderEditor(host, { idPrefix: 'r', value: [60, 0], withMessages: true });
    type(60, '   ');
    type(0, '  Now  ');
    expect(editor.messages()).toEqual({ 0: 'Now' });
  });

  it('calls onChange when a message is typed', () => {
    let calls = 0;
    createReminderEditor(host, { idPrefix: 'r', value: [60], withMessages: true, onChange: () => { calls += 1; } });
    type(60, 'a');
    expect(calls).toBe(1);
  });

  it('setValue prunes messages of reminders that are gone; setMessages replaces them', () => {
    const editor = createReminderEditor(host, { idPrefix: 'r', value: [60, 0], withMessages: true, messages: { 60: 'A', 0: 'B' } });
    editor.setValue([60]);
    expect(editor.messages()).toEqual({ 60: 'A' });
    editor.setMessages({ 60: 'Z', 15: 'ignored: no such reminder' });
    expect(editor.messages()).toEqual({ 60: 'Z' });
  });

  it('shows a message box with the text escaped, never as markup', () => {
    createReminderEditor(host, { idPrefix: 'r', value: [60], withMessages: true, messages: { 60: '</textarea><img src=x onerror=1>' } });
    expect(host.querySelector('img')).toBeNull();
    expect(box(60).value).toBe('</textarea><img src=x onerror=1>');
  });

  it('shows no message boxes when there are no reminders', () => {
    createReminderEditor(host, { idPrefix: 'r', value: [], withMessages: true });
    expect(host.querySelector('textarea')).toBeNull();
  });
});
