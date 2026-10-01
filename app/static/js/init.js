// Runs once, after every other admin script has loaded: starts the clock,
// applies the Kingdom's branding title, confirms who is logged in, loads the
// alliances, then opens the tab named in the URL hash (default Events).
// loadMe()'s own api() call redirects to /auth/login on a 401.

startHeaderClock();

// Public, unauthenticated, mounted at the site root (not under /admin), so a
// plain fetch rather than api().
fetch('/api/kingdom-branding').then((r) => r.json()).then((b) => {
  if (!b.admin_console_title) return;
  document.title = b.admin_console_title;
  const el = byId('headerTitle');
  if (el) el.textContent = b.admin_console_title;
}).catch(() => { /* the default title stays */ });

function initialView() {
  const hash = (window.location.hash || '').replace('#', '');
  const btn = document.querySelector('.tabs__link[data-view="' + hash + '"]');
  if (btn && !btn.closest('.hidden')) return hash;
  return 'events';
}

loadMe().then(() => loadTenants()).then(() => {
  if (!TENANTS.length) {
    byId('appMain').classList.add('hidden');
    byId('noAccess').classList.remove('hidden');
    byId('noAccessUser').textContent = ME.discord_username;
    return;
  }
  applyRoleVisibility();
  showView(initialView());
  maybeShowFirstVisitTzModal();
});
