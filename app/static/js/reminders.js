// Reminder editor: "minutes before the start" as removable chips, with presets
// and a custom value. Used by the event form and the event type form.
// Depends on common.js.
//
// The server accepts at most REMINDER_MAX_COUNT reminders per event, each from
// 0 (at the start) to four weeks ahead (services/validators.py).

const REMINDER_MAX_COUNT = 10;
const REMINDER_MAX_MINUTES = 28 * 24 * 60;

const REMINDER_PRESETS = [0, 5, 15, 30, 60, 120, 1440];

// host gets the whole editor. opts: { idPrefix, value: [minutes], onChange,
// withMessages, messages: {minutes: text} }. With withMessages each reminder also
// gets its own message box (spec §77); the type form leaves that off.
// Returns { value(), setValue(minutes[]), messages(), setMessages({}) };
// value() is sorted largest first and messages() holds only the non-blank texts.
function createReminderEditor(host, opts) {
  const p = opts.idPrefix;
  let minutes = [];
  let texts = {};

  host.innerHTML = `
    <div class="reminders">
      <ul class="chips" id="${p}Chips" aria-label="Reminders"></ul>
      <div class="reminders__presets" id="${p}Presets" role="group" aria-label="Add a reminder">
        ${REMINDER_PRESETS.map((m) => `<button type="button" class="pf-v6-c-button pf-m-secondary pf-m-small" data-minutes="${m}">+ ${escapeHtml(describeReminder(m))}</button>`).join('')}
      </div>
      <div class="reminders__custom">
        <label class="pf-v6-c-form__label" for="${p}Custom"><span class="pf-v6-c-form__label-text">Custom reminder</span></label>
        <div class="reminders__custom-row">
          <input class="pf-v6-c-form-control" type="number" min="0" step="1" id="${p}Custom" placeholder="e.g. 45">
          <label class="sr-only" for="${p}Unit">Unit</label>
          <select class="pf-v6-c-form-control" id="${p}Unit">
            <option value="1">minutes before</option>
            <option value="60">hours before</option>
            <option value="1440">days before</option>
          </select>
          <button type="button" class="pf-v6-c-button pf-m-secondary" id="${p}AddCustom">Add</button>
        </div>
      </div>
      <p class="reminders__help">Each reminder posts to each selected destination that many minutes before the start. 0 means at the start. Up to ${REMINDER_MAX_COUNT}, at most 4 weeks ahead.</p>
      ${opts.withMessages ? `<div class="reminders__messages" id="${p}Messages"></div>` : ''}
    </div>`;

  const chips = document.getElementById(p + 'Chips');
  const messagesHost = opts.withMessages ? document.getElementById(p + 'Messages') : null;

  function renderMessages() {
    if (!messagesHost) return;
    messagesHost.innerHTML = minutes.length
      ? `<p class="reminders__help">Give a reminder its own text, for example "Starting now!" at 0. A reminder left empty posts the event message, or "1 hour until Event Name" if that is empty too. A reminder's own text is used for every alliance, in place of an alliance's own message.</p>`
        + minutes.map((m) => `<div class="reminders__message">
          <label class="pf-v6-c-form__label" for="${p}Msg${m}"><span class="pf-v6-c-form__label-text">Message: ${escapeHtml(describeReminder(m))}</span></label>
          <textarea class="pf-v6-c-form-control" id="${p}Msg${m}" rows="2" maxlength="${COMPOSER_MAX_CHARS}" data-minutes="${m}" placeholder="Uses the event message">${escapeHtml(texts[m] || '')}</textarea>
        </div>`).join('')
      : '';
  }
  if (messagesHost) {
    messagesHost.addEventListener('input', (e) => {
      const box = e.target.closest('textarea[data-minutes]');
      if (!box) return;
      texts[parseInt(box.dataset.minutes, 10)] = box.value;
      if (opts.onChange) opts.onChange();
    });
  }

  function render() {
    chips.innerHTML = minutes.length
      ? minutes.map((m) => {
        const label = describeReminder(m);
        return `<li class="chips__item"><span>${escapeHtml(label)}</span>`
          + `<button type="button" class="chips__remove" data-remove="${m}" aria-label="Remove reminder: ${escapeHtml(label)}">&times;</button></li>`;
      }).join('')
      : '<li class="chips__empty">No reminders. Nothing is posted to the channel for this event.</li>';
    renderMessages();
  }

  function pruneTexts() {
    texts = Object.fromEntries(Object.entries(texts).filter(([m]) => minutes.includes(parseInt(m, 10))));
  }

  function add(m) {
    if (!Number.isInteger(m) || m < 0) { toast('A reminder must be a whole number of minutes, 0 or more', true); return false; }
    if (m > REMINDER_MAX_MINUTES) { toast('A reminder cannot be more than 4 weeks ahead', true); return false; }
    if (minutes.includes(m)) { toast('That reminder is already in the list', true); return false; }
    if (minutes.length >= REMINDER_MAX_COUNT) { toast(`At most ${REMINDER_MAX_COUNT} reminders`, true); return false; }
    minutes = minutes.concat([m]).sort((a, b) => b - a);
    render();
    if (opts.onChange) opts.onChange();
    return true;
  }

  document.getElementById(p + 'Presets').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-minutes]');
    if (btn) add(parseInt(btn.dataset.minutes, 10));
  });
  chips.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-remove]');
    if (!btn) return;
    const m = parseInt(btn.dataset.remove, 10);
    minutes = minutes.filter((x) => x !== m);
    pruneTexts();
    render();
    if (opts.onChange) opts.onChange();
    document.getElementById(p + 'Custom').focus();
  });
  const custom = document.getElementById(p + 'Custom');
  function addCustom() {
    const n = parseInt(custom.value, 10);
    const unit = parseInt(document.getElementById(p + 'Unit').value, 10);
    if (Number.isNaN(n)) { toast('Enter a number for the custom reminder', true); custom.focus(); return; }
    if (add(n * unit)) custom.value = '';
  }
  document.getElementById(p + 'AddCustom').addEventListener('click', addCustom);
  custom.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); addCustom(); }
  });

  minutes = (opts.value || []).slice().sort((a, b) => b - a);
  texts = Object.fromEntries(Object.entries(opts.messages || {}).map(([m, t]) => [parseInt(m, 10), t]));
  pruneTexts();
  render();

  return {
    value() { return minutes.slice(); },
    setValue(list) { minutes = (list || []).slice().sort((a, b) => b - a); pruneTexts(); render(); },
    messages() {
      const out = {};
      minutes.forEach((m) => { const t = (texts[m] || '').trim(); if (t) out[m] = t; });
      return out;
    },
    setMessages(map) {
      texts = Object.fromEntries(Object.entries(map || {}).map(([m, t]) => [parseInt(m, 10), t]));
      pruneTexts();
      render();
    },
  };
}
