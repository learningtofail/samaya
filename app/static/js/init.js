// Runs once, after every other admin script has loaded: confirms who's
// logged in, loads the tenant list (populating the header's alliance link
// and TENANT_COLORS), then paints the dashboard under whichever tenant
// was already selected (or the first one, on a fresh browser). loadMe()'s
// own api() call redirects to /auth/login on a 401, so there's nothing
// else to handle for "not logged in" here.
// The time zone clock (spec §24) needs no login or tenant context, so
// it starts immediately rather than waiting on the loadMe() chain below —
// a logged-out visitor briefly seeing the login redirect still gets a
// correctly-running clock if the redirect is ever delayed.
startHeaderClock();

loadMe().then(() => loadTenants()).then(() => {
  if (!TENANTS.length) {
    document.querySelector('main').innerHTML =
      '<div style="max-width:32rem;margin:4rem auto;text-align:center">'
      + '<h2>No alliance access yet</h2>'
      + '<p style="color:var(--muted)">You\'re logged in as ' + escapeHtml(ME.discord_username) + ', '
      + 'but don\'t have access to any alliance. Ask your coordinator for an invite link.</p>'
      + '</div>';
    document.getElementById('tenantLink').style.display = 'none';
    return;
  }
  applyRoleVisibility();
  loadDashboard();
});
