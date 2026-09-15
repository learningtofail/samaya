// Runs once, after every other admin script has loaded: loads the tenant
// list (populating the picker and TENANT_COLORS), then paints the
// dashboard and the theme picker under whichever tenant was already
// selected (or the first one, on a fresh browser).
loadTenants().then(() => {
  loadDashboard();
  renderThemeSwatches();
});
