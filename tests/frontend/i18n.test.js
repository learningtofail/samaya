// Unit tests for app/static/i18n.js (spec §72.4): lookup, plurals, escaping,
// DOM application and locale-aware formatting. The real script is evaluated
// verbatim, as events-public.test.js does for events-public.js.
import { readFileSync } from "node:fs";
import { beforeAll, describe, expect, test } from "vitest";

let SamayaI18n;
let JSDOM;

beforeAll(async () => {
  ({ JSDOM } = await import("jsdom"));
  new Function(readFileSync(new URL("../../app/static/i18n.js", import.meta.url), "utf8"))();
  SamayaI18n = globalThis.SamayaI18n;
});

const sample = {
  locale: "en",
  dir: "ltr",
  strings: {
    "a.hello": "Hello {name}",
    "a.items": { one: "{count} item", other: "{count} items" },
    "a.boldHtml": "Last: <strong>{name}</strong>",
  },
};

describe("t", () => {
  test("fills placeholders and leaves unknown ones as written", () => {
    const i = SamayaI18n.create(sample);
    expect(i.t("a.hello", { name: "Ada" })).toBe("Hello Ada");
    expect(i.t("a.hello")).toBe("Hello {name}");
  });

  test("a missing key returns the key", () => {
    expect(SamayaI18n.create(sample).t("nope")).toBe("nope");
  });

  test("plural objects pick a category from count", () => {
    const i = SamayaI18n.create(sample);
    expect(i.t("a.items", { count: 1 })).toBe("1 item");
    expect(i.t("a.items", { count: 3 })).toBe("3 items");
  });

  test("Russian plurals use the language's own categories", () => {
    const i = SamayaI18n.create({ locale: "ru", strings: { k: { one: "{count} день", few: "{count} дня", many: "{count} дней", other: "{count} дня" } } });
    expect([1, 3, 5, 21].map((n) => i.t("k", { count: n }))).toEqual(["1 день", "3 дня", "5 дней", "21 день"]);
  });
});

describe("tHtml", () => {
  test("keeps the template's markup and escapes every parameter", () => {
    const i = SamayaI18n.create(sample);
    expect(i.tHtml("a.boldHtml", { name: "<img src=x onerror=1>" })).toBe("Last: <strong>&lt;img src=x onerror=1&gt;</strong>");
  });
});

describe("formatting", () => {
  test("durations and relative times follow the locale", () => {
    const en = SamayaI18n.create({ locale: "en" });
    expect(en.unit(30, "minute")).toBe("30 min");
    expect(en.relative(-2, "hour")).toBe("2 hours ago");
    const de = SamayaI18n.create({ locale: "de" });
    expect(de.relative(5, "minute")).toBe("in 5 Minuten");
  });

  test("digits stay Western in Arabic", () => {
    const ar = SamayaI18n.create({ locale: "ar" });
    expect(ar.number(2026)).toMatch(/^[0-9٬,.]+$/);
    expect(ar.unit(5, "minute", "narrow")).toMatch(/[0-9]/);
  });

  test("the pseudo-locale en-XA falls back to English formats", () => {
    expect(SamayaI18n.create({ locale: "en-XA" }).relative(5, "minute")).toBe("in 5 minutes");
  });

  test("list joins with the locale's conjunction", () => {
    expect(SamayaI18n.create({ locale: "en" }).list(["MOD", "NSR", "ABC"])).toBe("MOD, NSR, and ABC");
  });
});

describe("apply and fromDocument", () => {
  test("applies data-i18n text and data-i18n-attr attributes, skipping unknown keys", () => {
    const dom = new JSDOM(`<body>
      <p id="a" data-i18n="a.hello">Fallback</p>
      <p id="b" data-i18n="missing.key">Keep me</p>
      <input id="c" placeholder="old" data-i18n-attr="placeholder:a.hello;title:a.hello">
    </body>`);
    const i = SamayaI18n.create(sample);
    i.apply(dom.window.document);
    const d = dom.window.document;
    expect(d.getElementById("a").textContent).toBe("Hello {name}");
    expect(d.getElementById("b").textContent).toBe("Keep me");
    expect(d.getElementById("c").getAttribute("placeholder")).toBe("Hello {name}");
    expect(d.getElementById("c").getAttribute("title")).toBe("Hello {name}");
  });

  test("reads the page's i18n block; a broken block yields an empty, English instance", () => {
    const ok = new JSDOM(`<script type="application/json" id="i18n">${JSON.stringify(sample)}</script>`);
    expect(SamayaI18n.fromDocument(ok.window.document).t("a.hello", { name: "x" })).toBe("Hello x");
    const bad = new JSDOM('<script type="application/json" id="i18n">{not json</script>');
    const i = SamayaI18n.fromDocument(bad.window.document);
    expect(i.locale).toBe("en");
    expect(i.t("a.hello")).toBe("a.hello");
  });
});
