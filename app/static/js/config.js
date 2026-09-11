function renderThemeSwatches() {
  const grid = document.getElementById('themeSwatchGrid');
  if (!grid) return;
  const current = window.getSamayaThemeCookie('samaya_theme') || 'twilight-slate';
  grid.innerHTML = Object.entries(window.SAMAYA_THEMES).map(([key, p]) => {
    const active = key === current;
    return `<button type="button" class="theme-swatch${active ? ' active' : ''}" data-theme="${key}"
      style="background:${p.bg}" title="${p.name}" onclick="selectTheme('${key}')">
      <span style="background:${p.header}"></span>
    </button>`;
  }).join('');
}

function selectTheme(key) {
  window.applyTheme(key);
  renderThemeSwatches();
}

async function saveDiscordConfig() {
  const payload = {
    bot_token:  document.getElementById('cfgToken').value.trim(),
    guild_id:   document.getElementById('cfgGuild').value.trim(),
    public_key: document.getElementById('cfgPublicKey').value.trim(),
  };
  const result = document.getElementById('cfgResult');
  result.textContent = 'Verifying…';
  try {
    const res = await api('PUT', '/api/config/discord', payload);
    result.textContent = `✓ Saved. Bot: ${res.bot_username}`;
    result.style.color = '#166534';
    configDirty = false;
    toast('Discord config saved');
  } catch(e) {
    result.textContent = `✗ ${e.message}`;
    result.style.color = '#991b1b';
    toast(e.message, true);
  }
}

// ── Sync ────────────────────────────────────────────────

