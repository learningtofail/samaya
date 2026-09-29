// Runs once, after every other admin script has loaded: confirms who's
// logged in, loads the tenant list (populating TENANT_COLORS/TENANT_ICONS),
// then paints the dashboard. loadMe()'s own api() call redirects to
// /auth/login on a 401, so there's nothing else to handle for "not logged
// in" here.
// The time zone clock (spec §24) needs no login or tenant context, so
// it starts immediately rather than waiting on the loadMe() chain below —
// a logged-out visitor briefly seeing the login redirect still gets a
// correctly-running clock if the redirect is ever delayed.
startHeaderClock();

// Spec §38.7: the admin console's own masthead title/tab title are
// editable per-kingdom now (Access & Platform tab). Public, unauthenticated
// endpoint mounted at the site root, not under /admin — so a plain fetch()
// here, not api() (which always prepends /admin). Same trust level as the
// header clock, so it also doesn't wait on loadMe().
fetch('/api/kingdom-branding').then(r => r.json()).then(b => {
  if (b.admin_console_title) {
    document.title = b.admin_console_title;
    const el = document.getElementById('headerTitle');
    if (el) el.textContent = '⏱ ' + b.admin_console_title;
  }
}).catch(() => {});

loadMe().then(() => loadTenants()).then(() => {
  if (!TENANTS.length) {
    document.querySelector('main').innerHTML =
      '<div style="max-width:32rem;margin:4rem auto;text-align:center">'
      + '<h2>No alliance access yet</h2>'
      + '<p style="color:var(--muted)">You\'re logged in as ' + escapeHtml(ME.discord_username) + ', '
      + 'but don\'t have access to any alliance. Ask your coordinator for an invite link.</p>'
      + '</div>';
    return;
  }
  applyRoleVisibility();
  loadDashboard();
  // Only once real access is confirmed — not before loadMe()'s 401 redirect
  // has had a chance to fire, so a logged-out visitor never sees this modal
  // flash before being sent to /auth/login.
  maybeShowFirstVisitTzModal();
});
