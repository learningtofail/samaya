/* Interface strings and locale-aware formatting for the public pages (spec §72.4).
   The server embeds {locale, dir, strings} as <script type="application/json" id="i18n">;
   this file reads it and applies data-i18n / data-i18n-attr before first paint.
   One global, SamayaI18n, because events-public.js and feedback.js are separate IIFEs. */
(function (root) {
'use strict';

function canonical(tag) {
  try { return Intl.getCanonicalLocales(tag)[0]; } catch (e) { return 'en'; }
}

function create(data) {
  const locale = (data && data.locale) || 'en';
  const dir = (data && data.dir) || 'ltr';
  const strings = (data && data.strings) || {};
  const locales = (data && data.locales) || [];
  // Intl needs a tag it knows; the pseudo-locale en-XA falls back to English formats.
  const intlLocale = new Intl.NumberFormat(canonical(locale)).resolvedOptions().locale;
  // Times and counts use Western digits in every language (spec §72.4).
  const latnLocale = `${intlLocale}${/-u-/.test(intlLocale) ? '' : '-u'}-nu-latn`;
  const plurals = new Intl.PluralRules(intlLocale);

  function fill(template, params) {
    return template.replace(/\{(\w+)\}/g, (m, k) => (params && params[k] !== undefined ? String(params[k]) : m));
  }
  function pick(key, params) {
    const v = strings[key];
    if (v === undefined) return undefined;
    if (typeof v !== 'object') return v;
    const n = params ? Number(params.count) : NaN;
    return v[plurals.select(n)] || v.other || '';
  }
  function escapeHtml(v) {
    return String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  /** Text for textContent or an escaped attribute. A missing key returns the key. */
  function t(key, params) {
    const v = pick(key, params);
    return v === undefined ? key : fill(v, params);
  }
  /** For catalogue strings that carry markup (keys ending in Html): the template is
      trusted, every parameter is escaped. */
  function tHtml(key, params) {
    const safe = {};
    Object.keys(params || {}).forEach((k) => { safe[k] = escapeHtml(params[k]); });
    const v = pick(key, params);
    return v === undefined ? escapeHtml(key) : fill(v, safe);
  }
  function has(key) { return strings[key] !== undefined; }

  function number(n, opts) { return new Intl.NumberFormat(latnLocale, opts).format(n); }
  /** "30 min", "3 hr", "2 days" in the page language. */
  function unit(n, name, display, opts) {
    return new Intl.NumberFormat(latnLocale, Object.assign({ style: 'unit', unit: name, unitDisplay: display || 'short', maximumFractionDigits: 1 }, opts)).format(n);
  }
  /** "in 5 minutes" / "5 minutes ago". `value` is signed, `name` is a RelativeTimeFormat unit. */
  function relative(value, name) {
    return new Intl.RelativeTimeFormat(intlLocale, { numeric: 'always', style: 'long' }).format(value, name);
  }
  function list(items) {
    return new Intl.ListFormat(intlLocale, { style: 'long', type: 'conjunction' }).format(items);
  }

  function apply(scope) {
    (scope || document).querySelectorAll('[data-i18n]').forEach((el) => {
      if (has(el.dataset.i18n)) el.textContent = t(el.dataset.i18n);
    });
    (scope || document).querySelectorAll('[data-i18n-attr]').forEach((el) => {
      el.dataset.i18nAttr.split(';').forEach((pair) => {
        const i = pair.indexOf(':');
        const attr = pair.slice(0, i).trim(), key = pair.slice(i + 1).trim();
        if (i > 0 && has(key)) el.setAttribute(attr, t(key));
      });
    });
  }

  /** First day of the week, 1 = Monday ... 7 = Sunday. English keeps the page's existing
      Monday start; other languages follow their region's convention when the browser knows it. */
  function firstWeekday() {
    if (intlLocale.split('-')[0] === 'en') return 1;
    try {
      const loc = new Intl.Locale(intlLocale).maximize();
      const info = typeof loc.getWeekInfo === 'function' ? loc.getWeekInfo() : loc.weekInfo;
      if (info && info.firstDay >= 1 && info.firstDay <= 7) return info.firstDay;
    } catch (e) { /* fall through to Monday */ }
    return 1;
  }
  /** Wrap a name in Unicode directional isolates so it keeps its own order inside a sentence
      of the opposite direction (spec §72.6). For text; use <bdi> in markup. */
  function iso(text) { return `\u2068${text}\u2069`; }

  /** Fill and show the language select when the page offers more than one language.
      Choosing one reloads the page with ?lang=, which also sets the cookie. */
  function bindLanguageSelect(select, locales) {
    if (!select || !locales || locales.length < 2) return;
    select.innerHTML = locales.map((l) => `<option value="${escapeHtml(l.tag)}" lang="${escapeHtml(l.tag)}"${l.tag === locale ? ' selected' : ''}>${escapeHtml(l.name)}</option>`).join('');
    select.hidden = false;
    select.addEventListener('change', () => {
      const url = new URL(window.location.href);
      url.searchParams.set('lang', select.value);
      window.location.assign(url.toString());
    });
  }

  return { locale, dir, intlLocale, latnLocale, locales, t, tHtml, has, escapeHtml, number, unit, relative, list, apply, bindLanguageSelect, firstWeekday, iso };
}

function fromDocument(doc) {
  let data = {};
  try {
    const el = doc.getElementById('i18n');
    if (el) data = JSON.parse(el.textContent);
  } catch (e) { console.warn('SamayaI18n: could not read the i18n block', e); }
  return create(data);
}

root.SamayaI18n = { create, fromDocument };
})(globalThis);
