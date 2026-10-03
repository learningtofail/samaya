// Safe Markdown subset for internal ticket notes (spec §76.5). Plain script,
// loaded before tickets.js.
//
// Safe by construction: every line is HTML-escaped first, and only then are a
// fixed set of rules applied, so the output can contain nothing but the tags
// written in this file. Links must start with http:// or https:// (an escaped
// quote cannot leave the href), and a link opens in a new tab with
// rel="noopener noreferrer". Supported: headings (# to ###, shown as h4 to h6),
// paragraphs, **bold**, *italic*, `code`, fenced code, bullet and numbered
// lists, and [label](https://url). Everything else shows as plain text.

function renderMarkdownSafe(source) {
  const esc = (s) => s.replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));

  // `text` is already escaped. Code spans are lifted out first so their content stays literal.
  const inline = (text) => {
    const codes = [];
    let t = text.replace(/`([^`\n]+)`/g, (_, code) => { codes.push(code); return `\u0000${codes.length - 1}\u0000`; });
    t = t.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
      (_, label, url) => `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`);
    t = t.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    t = t.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
    return t.replace(/\u0000(\d+)\u0000/g, (_, n) => `<code>${codes[Number(n)]}</code>`);
  };

  const lines = String(source == null ? '' : source).replace(/\r\n?/g, '\n').replace(/\u0000/g, '').split('\n');
  const out = [];
  let para = [];
  let list = null;

  const flushPara = () => {
    if (para.length) out.push('<p>' + para.map(inline).join('<br>') + '</p>');
    para = [];
  };
  const flushList = () => {
    if (list) out.push(`<${list.tag}>` + list.items.map((item) => `<li>${inline(item)}</li>`).join('') + `</${list.tag}>`);
    list = null;
  };
  const addItem = (tag, text) => {
    flushPara();
    if (list && list.tag !== tag) flushList();
    if (!list) list = { tag, items: [] };
    list.items.push(text);
  };

  for (let i = 0; i < lines.length; i += 1) {
    if (/^```/.test(lines[i])) {
      flushPara();
      flushList();
      const code = [];
      i += 1;
      while (i < lines.length && !/^```/.test(lines[i])) { code.push(esc(lines[i])); i += 1; }
      out.push(`<pre><code>${code.join('\n')}</code></pre>`);
      continue;
    }
    const line = esc(lines[i]);
    let m;
    if (!line.trim()) { flushPara(); flushList(); }
    else if ((m = line.match(/^(#{1,3})\s+(.+)$/))) {
      flushPara();
      flushList();
      const level = m[1].length + 3;
      out.push(`<h${level}>${inline(m[2])}</h${level}>`);
    } else if ((m = line.match(/^\s*[-*]\s+(.+)$/))) addItem('ul', m[1]);
    else if ((m = line.match(/^\s*\d+[.)]\s+(.+)$/))) addItem('ol', m[1]);
    else { flushList(); para.push(line); }
  }
  flushPara();
  flushList();
  return out.join('');
}
