// "Use the standard look" (spec §71.15): a per-visitor opt-out of the Kingdom's theme and art.
// The choice is a cookie the server reads, so the next load renders without the theme and
// there is no flash. The buttons stay hidden unless a theme exists (data-theme-state).
(function () {
  'use strict';
  const COOKIE = 'samaya_standard';
  const state = document.documentElement.dataset.themeState;
  if (!state) return;
  const box = document.getElementById('lookBox');
  const standard = document.getElementById('lookStandard');
  const themed = document.getElementById('lookThemed');
  box.hidden = false;
  (state === 'on' ? standard : themed).hidden = false;
  function setCookie(value) {
    const age = value ? 365 * 24 * 3600 : 0;
    document.cookie = COOKIE + '=' + (value || '') + '; max-age=' + age + '; path=/; samesite=lax';
    window.location.reload();
  }
  standard.addEventListener('click', () => setCookie('1'));
  themed.addEventListener('click', () => setCookie(''));
})();
