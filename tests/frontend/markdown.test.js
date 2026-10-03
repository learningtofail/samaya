// Unit tests for app/static/js/markdown.js, the escape-first Markdown subset
// used for internal ticket notes (spec §76.5). The script is a plain classic
// <script> that defines one global function, so it is loaded the same way as
// events-public.js: evaluated with new Function against the real source.
// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, test } from "vitest";

const source = readFileSync(resolve(process.cwd(), "app/static/js/markdown.js"), "utf8");
const render = new Function(`${source}\nreturn renderMarkdownSafe;`)();

const ALLOWED_TAGS = new Set(["P", "BR", "STRONG", "EM", "CODE", "PRE", "A", "UL", "OL", "LI", "H4", "H5", "H6"]);

/** Parse the output like a browser would and report anything outside the allowed set. */
function violations(html) {
  const host = document.createElement("div");
  host.innerHTML = html;
  const bad = [];
  host.querySelectorAll("*").forEach((el) => {
    if (!ALLOWED_TAGS.has(el.tagName)) bad.push(`tag ${el.tagName}`);
    for (const attr of el.attributes) {
      const okLink = el.tagName === "A" && (
        (attr.name === "href" && /^https?:\/\//.test(attr.value)) ||
        (attr.name === "target" && attr.value === "_blank") ||
        (attr.name === "rel" && attr.value === "noopener noreferrer"));
      if (!okLink) bad.push(`attr ${el.tagName}.${attr.name}=${attr.value}`);
    }
  });
  return bad;
}

describe("renderMarkdownSafe: supported syntax", () => {
  test("paragraphs and line breaks", () => {
    expect(render("one\ntwo\n\nthree")).toBe("<p>one<br>two</p><p>three</p>");
  });
  test("bold, italic and inline code", () => {
    expect(render("a **b** and *c* and `d`")).toBe("<p>a <strong>b</strong> and <em>c</em> and <code>d</code></p>");
  });
  test("code spans stay literal", () => {
    expect(render("`**not bold** <b>`")).toBe("<p><code>**not bold** &lt;b&gt;</code></p>");
  });
  test("headings render below the ticket's own headings", () => {
    expect(render("# A\n## B\n### C\n#### D")).toBe("<h4>A</h4><h5>B</h5><h6>C</h6><p>#### D</p>");
  });
  test("bullet and numbered lists", () => {
    expect(render("- a\n- b\n\n1. x\n2) y")).toBe("<ul><li>a</li><li>b</li></ul><ol><li>x</li><li>y</li></ol>");
  });
  test("switching list kind starts a new list", () => {
    expect(render("- a\n1. b")).toBe("<ul><li>a</li></ul><ol><li>b</li></ol>");
  });
  test("fenced code keeps its lines and escapes them", () => {
    expect(render("```\n<b>x</b>\n  y\n```")).toBe("<pre><code>&lt;b&gt;x&lt;/b&gt;\n  y</code></pre>");
  });
  test("an unclosed fence runs to the end", () => {
    expect(render("```\na")).toBe("<pre><code>a</code></pre>");
  });
  test("https links open safely in a new tab", () => {
    expect(render("[docs](https://example.com/a?b=1&c=2)")).toBe(
      '<p><a href="https://example.com/a?b=1&amp;c=2" target="_blank" rel="noopener noreferrer">docs</a></p>');
  });
  test("empty, null and undefined input", () => {
    expect(render("")).toBe("");
    expect(render(null)).toBe("");
    expect(render(undefined)).toBe("");
  });
  test("Windows line endings", () => {
    expect(render("a\r\nb")).toBe("<p>a<br>b</p>");
  });
});

describe("renderMarkdownSafe: hostile input", () => {
  const payloads = [
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "[x](javascript:alert(1))",
    "[x](JaVaScRiPt:alert(1))",
    "[x](data:text/html;base64,PHNjcmlwdD4=)",
    '[x](https://a.com" onmouseover="alert(1))',
    "[x](https://a.com' onmouseover='alert(1))",
    "[<img src=x onerror=1>](https://a.com)",
    "![img](https://a.com/x.png)",
    "**<b onclick=1>x</b>**",
    "&lt;script&gt;alert(1)&lt;/script&gt;",
    "`</code><script>alert(1)</script>`",
    "```\n</code></pre><script>alert(1)</script>\n```",
    "# <svg onload=alert(1)>",
    "- <iframe src=javascript:alert(1)>",
    "\u0000<script>alert(1)</script>",
    "<a href=\"javascript:alert(1)\">x</a>",
    "[a](https://x.com)<style>*{display:none}</style>",
  ];
  test.each(payloads)("only allowed tags and attributes: %s", (payload) => {
    expect(violations(render(payload))).toEqual([]);
  });
  test("a nul character in the input cannot forge a code placeholder", () => {
    expect(render("`a` \u00000\u0000")).toBe("<p><code>a</code> 0</p>");
  });
  test("javascript links stay plain text", () => {
    expect(render("[x](javascript:alert(1))")).not.toContain("<a");
  });
});
