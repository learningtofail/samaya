// Loaded in <head>, before <body>, so the saved theme applies before first
// paint (avoids a flash of the default palette). Self-contained: exposes
// window.SAMAYA_THEMES / window.applyTheme / window.getSamayaThemeCookie
// for config.js to use, rather than relying on shared globals from
// common.js (which loads later, at the end of <body>).
(function() {
  var THEME_PALETTES = {
    'autumn-harvest':    { name: 'Autumn Harvest',      text: '#2C1E11', header: '#A04000', bg: '#FAF6F0' },
    'terracotta-sunset': { name: 'Terracotta Sunset',   text: '#242120', header: '#9E3216', bg: '#FDFBF7' },
    'crimson-executive': { name: 'Crimson Executive',   text: '#2A0808', header: '#880808', bg: '#FFFFFF' },
    'golden-orchard':    { name: 'Golden Orchard',      text: '#32292F', header: '#6E260E', bg: '#FFFFFF' },
    'desert-sage-clay':  { name: 'Desert Sage & Clay',  text: '#1B1B1B', header: '#913E2B', bg: '#F9F6F0' },
    'vintage-rose':      { name: 'Vintage Rose',        text: '#2E1A25', header: '#7D2254', bg: '#FFFDF9' },
    'glacier-tech':      { name: 'Glacier Tech',        text: '#0B132B', header: '#1C2541', bg: '#FFFFFF' },
    'nordic-spruce':     { name: 'Nordic Spruce',       text: '#111827', header: '#0F4C3A', bg: '#F4F7F6' },
    'pacific-deep':      { name: 'Pacific Deep',        text: '#1A1A1A', header: '#0B4F93', bg: '#FFFFFF' },
    'twilight-slate':    { name: 'Twilight Slate',      text: '#1E293B', header: '#334155', bg: '#F8FAFC',
                            vars: { '--bg':'#F8FAFC', '--bg2':'#F1F5F9', '--bg3':'#E2E8F0', '--border':'#CBD5E1',
                                    '--banner':'#1E293B', '--accent':'#334155', '--text':'#1E293B', '--muted':'#475569' } },
    'emerald-city':      { name: 'Emerald City',        text: '#0A2F1D', header: '#145A32', bg: '#FFFFFF' },
    'royal-velvet':      { name: 'Royal Velvet',        text: '#1A0F2E', header: '#4A148C', bg: '#FAF9FC' },
  };

  function hexToRgb(hex) {
    hex = hex.replace('#', '');
    if (hex.length === 3) hex = hex.split('').map(function(c) { return c + c; }).join('');
    var n = parseInt(hex, 16);
    return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
  }
  function rgbToHex(r, g, b) {
    return '#' + [r, g, b].map(function(v) {
      return Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, '0');
    }).join('');
  }
  function mix(hexA, hexB, t) {
    var a = hexToRgb(hexA), b = hexToRgb(hexB);
    return rgbToHex(a.r + (b.r - a.r) * t, a.g + (b.g - a.g) * t, a.b + (b.b - a.b) * t);
  }
  // Only Twilight Slate has fully spec'd secondary shades (see .vars above); the other 11
  // palettes in the doc give just text/header/background, so bg2/bg3/border/muted are
  // derived by mixing background toward text at increasing strength.
  function deriveVars(p) {
    return {
      '--bg': p.bg, '--bg2': mix(p.bg, p.text, 0.04), '--bg3': mix(p.bg, p.text, 0.08),
      '--border': mix(p.bg, p.text, 0.18), '--banner': p.header, '--accent': p.header,
      '--text': p.text, '--muted': mix(p.text, p.bg, 0.35),
    };
  }
  function getCookie(name) {
    var m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : null;
  }

  window.SAMAYA_THEMES = THEME_PALETTES;
  window.getSamayaThemeCookie = getCookie;
  window.applyTheme = function(key) {
    var p = THEME_PALETTES[key];
    if (!p) return;
    var vars = p.vars || deriveVars(p);
    for (var k in vars) document.documentElement.style.setProperty(k, vars[k]);
    var d = new Date();
    d.setTime(d.getTime() + 30 * 24 * 60 * 60 * 1000);
    document.cookie = 'samaya_theme=' + encodeURIComponent(key) + ';expires=' + d.toUTCString() + ';path=/;SameSite=Lax';
  };

  var saved = getCookie('samaya_theme');
  if (saved && THEME_PALETTES[saved]) window.applyTheme(saved);
})();
