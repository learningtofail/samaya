// Unit tests for the pure, DOM-independent functions in
// app/static/events-public.js (audit remediation, Phase 4).
//
// events-public.js is a plain classic <script> (no build step, no ES
// modules — see CLAUDE.md's app/static/ section), and it wires up real
// page elements and fires fetches as soon as it runs. Rather than
// stubbing an entire fake events.html DOM just to import a handful of
// pure helper functions, the script itself carries a small, explicit test
// hook: setting globalThis.__SAMAYA_TEST__ = true before it's evaluated
// makes it stop right after its pure-function section (before any DOM
// wiring or fetch) and hand those functions to
// globalThis.__SAMAYA_TEST_EXPORTS__ instead. That flag is never set in
// the browser, so production behavior is unchanged — see the comments
// around both guards in events-public.js itself.
import { readFileSync } from "node:fs";
import { beforeAll, describe, expect, test } from "vitest";

let fns;

beforeAll(async () => {
  globalThis.__SAMAYA_TEST__ = true;
  // events-public.js references window.location/localStorage/Intl at
  // module-evaluation time (before it even reaches the test-export
  // guard), so a browser-like global environment is needed to load it at
  // all. jsdom is already a vitest dependency; construct a minimal one
  // here rather than adding `environment: 'jsdom'` project-wide, since
  // nothing else in this suite needs a DOM.
  const { JSDOM } = await import("jsdom");
  // The page embeds its strings as an i18n block (spec §72.4); use the real catalogue.
  const en = JSON.parse(readFileSync(new URL("../../app/i18n/en.json", import.meta.url), "utf8"));
  delete en._meta;
  const block = JSON.stringify({ locale: "en", dir: "ltr", strings: en });
  const dom = new JSDOM(`<!doctype html><html><body><script type="application/json" id="i18n">${block}</script></body></html>`, { url: "https://ks138.taraka.dev/events" });
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.localStorage = dom.window.localStorage;
  globalThis.Intl = Intl;

  new Function(readFileSync(new URL("../../app/static/i18n.js", import.meta.url), "utf8"))();
  const source = readFileSync(new URL("../../app/static/events-public.js", import.meta.url), "utf8");
  // eslint-disable-next-line no-new-func -- executing the real production
  // script verbatim is the point: these tests exercise events-public.js
  // itself, not a reimplementation of it.
  new Function(source)();

  fns = globalThis.__SAMAYA_TEST_EXPORTS__;
});

describe("displayStatus", () => {
  test("event: pending occurrence not yet started", () => {
    expect(fns.displayStatus({ kind: "event", post_status: "pending", _start: 2000, _end: 3000 }, 1000)).toBe("scheduled");
  });

  test("event: posted and currently within its window is live", () => {
    expect(fns.displayStatus({ kind: "event", post_status: "posted", _start: 1000, _end: 3000 }, 2000)).toBe("live");
  });

  test("event: posted and past its end is completed", () => {
    expect(fns.displayStatus({ kind: "event", post_status: "posted", _start: 1000, _end: 2000 }, 3000)).toBe("completed");
  });

  test("event: error post_status is failed", () => {
    expect(fns.displayStatus({ kind: "event", post_status: "error", _start: 1000, _end: 2000 }, 500)).toBe("failed");
  });

  test("announcement: draft maps to scheduled", () => {
    expect(fns.displayStatus({ kind: "announcement", post_status: "draft" }, 0)).toBe("scheduled");
  });

  test("announcement: posted maps to announced, never 'live' (no duration)", () => {
    expect(fns.displayStatus({ kind: "announcement", post_status: "posted" }, 0)).toBe("announced");
  });

  test("unrecognized post_status defaults to scheduled", () => {
    expect(fns.displayStatus({ kind: "event", post_status: "something-new", _start: 1000, _end: 2000 }, 0)).toBe("scheduled");
  });
});

describe("daySpan", () => {
  const tz = "UTC";

  test("a same-day event returns exactly one key", () => {
    const ev = { _start: Date.UTC(2026, 5, 15, 10, 0), _end: Date.UTC(2026, 5, 15, 12, 0) };
    expect(fns.daySpan(ev, tz)).toEqual(["2026-06-15"]);
  });

  test("a multi-day event returns one key per day it touches", () => {
    const ev = { _start: Date.UTC(2026, 5, 15, 23, 0), _end: Date.UTC(2026, 5, 17, 1, 0) };
    expect(fns.daySpan(ev, tz)).toEqual(["2026-06-15", "2026-06-16", "2026-06-17"]);
  });

  test("an announcement (_end === _start) returns just its one day", () => {
    const ev = { _start: Date.UTC(2026, 5, 15, 9, 0), _end: Date.UTC(2026, 5, 15, 9, 0) };
    expect(fns.daySpan(ev, tz)).toEqual(["2026-06-15"]);
  });
});

describe("buildDayMap", () => {
  const tz = "UTC";

  test("groups events under their day key(s), earliest start first within a day", () => {
    const early = { _start: Date.UTC(2026, 5, 15, 8, 0), _end: Date.UTC(2026, 5, 15, 9, 0), _k: "e:1" };
    const late = { _start: Date.UTC(2026, 5, 15, 18, 0), _end: Date.UTC(2026, 5, 15, 19, 0), _k: "e:2" };
    const map = fns.buildDayMap([late, early], tz);
    const day = map.get("2026-06-15");
    expect(day.map((x) => x.ev._k)).toEqual(["e:1", "e:2"]);
    expect(day.every((x) => x.cont === false)).toBe(true);
  });

  test("a multi-day event's continuation rows are marked cont:true, last row marked last:true", () => {
    const ev = { _start: Date.UTC(2026, 5, 15, 20, 0), _end: Date.UTC(2026, 5, 17, 2, 0), _k: "e:multi" };
    const map = fns.buildDayMap([ev], tz);
    expect(map.get("2026-06-15")[0]).toMatchObject({ cont: false, last: false });
    expect(map.get("2026-06-16")[0]).toMatchObject({ cont: true, last: false });
    expect(map.get("2026-06-17")[0]).toMatchObject({ cont: true, last: true });
  });
});

describe("groupCombinedFanoutRows", () => {
  test("collapses one row per (kind, id, target) back into one row per item, with a targets array", () => {
    const rows = [
      { kind: "event", id: 1, tenant_slug: "mod", tenant_name: "MOD", tenant_color: "#111" },
      { kind: "event", id: 1, tenant_slug: "nsr", tenant_name: "NSR", tenant_color: "#222" },
      { kind: "announcement", id: 5, tenant_slug: "mod", tenant_name: "MOD", tenant_color: "#111" },
    ];
    const grouped = fns.groupCombinedFanoutRows(rows);
    expect(grouped).toHaveLength(2);
    const eventRow = grouped.find((g) => g.kind === "event");
    expect(eventRow.targets.map((t) => t.tenant_slug).sort()).toEqual(["mod", "nsr"]);
  });

  test("a kingdom-wide row is never fanned out and passes through untouched", () => {
    const rows = [{ kind: "event", id: 9, scope: "kingdom-wide" }];
    expect(fns.groupCombinedFanoutRows(rows)).toEqual(rows);
  });

  test("targets on a grouped row are sorted alphabetically by tenant_name", () => {
    const rows = [
      { kind: "event", id: 1, tenant_slug: "z", tenant_name: "Zeta" },
      { kind: "event", id: 1, tenant_slug: "a", tenant_name: "Alpha" },
    ];
    const [grouped] = fns.groupCombinedFanoutRows(rows);
    expect(grouped.targets.map((t) => t.tenant_name)).toEqual(["Alpha", "Zeta"]);
  });
});

describe("formatDuration", () => {
  test("falsy hours renders nothing (announcements have no duration)", () => {
    expect(fns.formatDuration(0)).toBe("");
    expect(fns.formatDuration(undefined)).toBe("");
  });

  test("exactly 24 hours is called out as all day", () => {
    expect(fns.formatDuration(24)).toBe("24 hr (all day)");
  });

  test("sub-hour durations render in minutes", () => {
    expect(fns.formatDuration(0.5)).toBe("30 min");
  });

  test("multi-day durations render in days", () => {
    expect(fns.formatDuration(48)).toBe("2 days");
  });

  test("ordinary durations render in hours", () => {
    expect(fns.formatDuration(3)).toBe("3 hr");
  });
});

describe("rel", () => {
  const MIN = 60000;
  test("under an hour is in minutes", () => {
    expect(fns.rel(5 * MIN)).toBe("in 5 minutes");
  });
  test("under a day is in hours", () => {
    expect(fns.rel(3 * 60 * MIN)).toBe("in 3 hours");
  });
  test("a day or more is in days", () => {
    expect(fns.rel(3 * 24 * 60 * MIN)).toBe("in 3 days");
  });
});

describe("cd", () => {
  test("under 48 hours counts down as hh:mm:ss with Western digits", () => {
    expect(fns.cd(((3 * 60 + 5) * 60 + 9) * 1000)).toBe("03:05:09");
  });
  test("48 hours or more shows days and hours", () => {
    expect(fns.cd((2 * 24 + 5) * 3600 * 1000)).toBe("2d 05h");
  });
  test("a negative remainder clamps to zero", () => {
    expect(fns.cd(-5000)).toBe("00:00:00");
  });
});

describe("evAlliances", () => {
  test("kingdom-wide scope always returns the single Kingdom-wide pseudo-alliance", () => {
    const result = fns.evAlliances({ scope: "kingdom-wide", targets: [{ tenant_slug: "mod", tenant_name: "MOD" }] });
    expect(result).toEqual([{ name: "Kingdom", icon: "", color: expect.any(String), slug: "" }]);
  });

  test("an explicit targets array is mapped to alliance info per target", () => {
    const result = fns.evAlliances({ targets: [{ tenant_slug: "mod", tenant_name: "MOD" }] });
    expect(result).toHaveLength(1);
    expect(result[0].name).toBe("MOD");
  });
});

// Shared with app/tests/test_contrast.py: the backend's pick_ink() and the
// page's brandInk() must agree on every color in this table (spec §63.1).
describe("brandInk matches the backend", () => {
  const fixtures = JSON.parse(readFileSync(new URL("./ink-fixtures.json", import.meta.url), "utf8"));
  test.each(fixtures)("%s -> %s", (hex, ink) => {
    expect(fns.brandInk(hex).toLowerCase()).toBe(ink.toLowerCase());
  });
});

describe("calendarLinks (vendor subscribe URL formats)", () => {
  const abs = "https://ks138.taraka.dev/events/mod.ics";
  const web = "webcal://ks138.taraka.dev/events/mod.ics";
  let links;
  beforeAll(() => { links = fns.calendarLinks(abs, web, "Events & More"); });

  test("Google gets the webcal:// form inside cid; https:// there is rejected by Google", () => {
    expect(links.google).toBe("https://calendar.google.com/calendar/r?cid=webcal%3A%2F%2Fks138.taraka.dev%2Fevents%2Fmod.ics");
  });
  test("Apple opens the bare webcal:// address", () => {
    expect(links.apple).toBe(web);
  });
  test("Outlook.com takes the https feed and an encoded calendar name", () => {
    const u = new URL(links.outlook);
    expect(u.origin + u.pathname).toBe("https://outlook.live.com/calendar/0/addfromweb");
    expect(u.searchParams.get("url")).toBe(abs);
    expect(u.searchParams.get("name")).toBe("Events & More");
  });
  test("download is the plain https feed", () => {
    expect(links.download).toBe(abs);
  });
});

describe("calendar grid geometry", () => {
  const H = 3600000;
  const day = Date.UTC(2026, 9, 5); // Monday 2026-10-05 00:00 UTC
  const ev = (name, startH, hours) => ({ event_name: name, _start: day + startH * H, _end: day + (startH + hours) * H });

  test("weekStartOf honours the locale's first weekday", () => {
    expect(fns.weekStartOf("2026-10-08", 1)).toBe("2026-10-05"); // Thursday -> Monday
    expect(fns.weekStartOf("2026-10-08", 7)).toBe("2026-10-04"); // Sunday start
    expect(fns.weekStartOf("2026-10-05", 1)).toBe("2026-10-05");
    expect(fns.weekStartOf("2026-10-04", 1)).toBe("2026-09-28"); // Sunday belongs to the week before
  });

  test("addDays crosses month and year ends", () => {
    expect(fns.addDays("2026-12-31", 1)).toBe("2027-01-01");
    expect(fns.addDays("2026-03-01", -1)).toBe("2026-02-28");
  });

  test("a timed event gets its start and end minute", () => {
    const [b] = fns.dayBlocks([ev("A", 10, 2)], "2026-10-05", "UTC");
    expect([b.s, b.e, b.cs, b.ce, b.col, b.n]).toEqual([600, 720, false, false, 0, 1]);
  });

  test("overlapping events share lanes; a later one starts a new cluster", () => {
    const out = fns.dayBlocks([ev("A", 10, 3), ev("B", 11, 1), ev("C", 15, 1)], "2026-10-05", "UTC");
    const by = Object.fromEntries(out.map((b) => [b.ev.event_name, b]));
    expect([by.A.col, by.A.n, by.B.col, by.B.n]).toEqual([0, 2, 1, 2]);
    expect([by.C.col, by.C.n]).toEqual([0, 1]);
  });

  test("a multi-day event continues into the next day", () => {
    const e = ev("Long", 20, 8); // 20:00 -> 04:00 next day
    const d1 = fns.dayBlocks([e], "2026-10-05", "UTC")[0], d2 = fns.dayBlocks([e], "2026-10-06", "UTC")[0];
    expect([d1.s, d1.e, d1.ce]).toEqual([1200, 1440, true]);
    expect([d2.s, d2.e, d2.cs]).toEqual([0, 240, true]);
  });

  test("an event covering the whole day goes to the all-day strip", () => {
    const out = fns.dayBlocks([ev("Day", 0, 48)], "2026-10-06", "UTC");
    expect(out).toHaveLength(0);
    expect(out.full).toHaveLength(1);
  });

  test("an announcement with no duration gets a 30 minute block", () => {
    const [b] = fns.dayBlocks([{ event_name: "N", _start: day + 9 * H, _end: day + 9 * H }], "2026-10-05", "UTC");
    expect(b.e - b.s).toBe(30);
  });

  test("events on other days are left out", () => {
    expect(fns.dayBlocks([ev("A", 10, 1)], "2026-10-06", "UTC")).toHaveLength(0);
  });

  test("minsOf reads the display zone", () => {
    expect(fns.minsOf(day + 10 * H, "UTC")).toBe(600);
    expect(fns.minsOf(day + 10 * H, "America/Toronto")).toBe(360); // UTC-4 in October
  });
});
