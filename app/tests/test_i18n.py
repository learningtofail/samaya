"""Interface languages (spec §72): catalogue integrity, locale choice, page rendering."""
import json
import re
from pathlib import Path

import pytest

from services import i18n

APP = Path(__file__).resolve().parent.parent
STATIC = APP / "static"
PAGES = {"events.html": "events-public.js", "feedback.html": "feedback.js"}

# CLDR plural categories per launch language (spec §72.2).
PLURAL_CATEGORIES = {
    "en": {"one", "other"}, "zh": {"other"}, "ar": {"zero", "one", "two", "few", "many", "other"},
    "fr": {"one", "many", "other"}, "es": {"one", "many", "other"}, "tr": {"one", "other"},
    "ru": {"one", "few", "many", "other"}, "de": {"one", "other"},
}
ALLOWED_TAGS = {"strong", "em", "b"}


def _en() -> dict:
    return json.loads((APP / "i18n" / "en.json").read_text(encoding="utf-8"))


def _placeholders(value) -> set[str]:
    values = value.values() if isinstance(value, dict) else [value]
    return {m for v in values for m in re.findall(r"\{(\w+)\}", v)}


def _keys_referenced() -> tuple[set[str], set[str], set[str]]:
    """(keys referenced directly, dynamic-family prefixes, every quoted literal's full key).

    Direct: data-i18n markup and `PREFIX_CONST + 'name'` in the scripts. Literals:
    any quoted word in a script, prefixed, so indirect uses (a ternary, a helper
    argument) still count a key as used. A typo in an indirect use is caught by
    the browser smoke test, not here."""
    exact, families, literals = set(), set(), set()
    for html in PAGES:
        text = (STATIC / html).read_text(encoding="utf-8")
        exact |= set(re.findall(r'data-i18n="([\w.]+)"', text))
        for attrs in re.findall(r'data-i18n-attr="([^"]+)"', text):
            exact |= {pair.split(":", 1)[1].strip() for pair in attrs.split(";") if ":" in pair}
    for js, const, prefix in (("events-public.js", "EVENTS_KEY", "public.events."), ("feedback.js", "FB", "public.feedback.")):
        text = (STATIC / js).read_text(encoding="utf-8")
        exact |= {prefix + k for k in re.findall(const + r" \+ '([\w.]+)'", text)}
        literals |= {prefix + k for k in re.findall(r"'([\w.]+)'", text)}
        families |= {f"{prefix}{k}." for k in re.findall(r"\$\{" + const + r"\}([\w.]+)\.\$\{", text)}
        families |= {f"{prefix}{k}." for k in re.findall(r"metaFor\('(\w+)'", text)}
    for py in (APP / "routers").glob("*.py"):
        exact |= set(re.findall(r'"(public\.[\w.]*\w)"', py.read_text(encoding="utf-8")))
    return exact, families, literals


class TestCatalogue:
    def test_en_meta_and_flat_string_values(self):
        data = _en()
        assert data["_meta"]["dir"] == "ltr" and data["_meta"]["reviewed"] is True
        for key, value in data.items():
            if key == "_meta":
                continue
            assert key.startswith("public."), key
            assert isinstance(value, (str, dict)), key

    def test_markup_only_in_html_keys(self):
        for key, value in _en().items():
            if key == "_meta":
                continue
            for text in (value.values() if isinstance(value, dict) else [value]):
                tags = set(re.findall(r"</?(\w+)", text))
                if key.endswith("Html"):
                    assert tags <= ALLOWED_TAGS, key
                else:
                    assert not tags, f"{key} contains markup but does not end in Html"

    def test_every_key_used_in_code_exists(self):
        exact, _, _ = _keys_referenced()
        missing = sorted(k for k in exact if k not in _en())
        assert not missing, missing

    def test_no_unused_keys(self):
        exact, families, literals = _keys_referenced()
        unused = sorted(k for k in _en() if k != "_meta" and k not in exact and k not in literals
                        and not k.startswith(tuple(families)))
        assert not unused, unused

    def test_dynamic_families_are_found(self):
        _, families, _ = _keys_referenced()
        assert {"public.events.status.", "public.events.kind.", "public.feedback.kind.", "public.feedback.status."} <= families

    @pytest.mark.parametrize("page", sorted(PAGES))
    def test_markup_english_matches_catalogue(self, page):
        """The static markup is the fallback text, so it must say what en.json says."""
        from html.parser import HTMLParser
        en = _en()
        found: list[tuple[str, str]] = []

        class P(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack = []

            def handle_starttag(self, tag, attrs):
                a = dict(attrs)
                self.stack.append([a.get("data-i18n"), ""])
                for pair in (a.get("data-i18n-attr") or "").split(";"):
                    if ":" in pair:
                        attr, key = pair.split(":", 1)
                        if attr.strip() in a:
                            found.append((key.strip(), a[attr.strip()]))

            def handle_data(self, data):
                for frame in self.stack:
                    if frame[0]:
                        frame[1] += data

            def handle_endtag(self, tag):
                key, text = self.stack.pop() if self.stack else (None, "")
                if key:
                    found.append((key, text))

        P().feed((STATIC / page).read_text(encoding="utf-8"))
        assert found
        for key, text in found:
            assert " ".join(text.split()) == en[key], key


LOCALE_FILES = [p.stem for p in (APP / "i18n").glob("*.json")]


@pytest.mark.parametrize("locale", LOCALE_FILES)
def test_locale_file_matches_english(locale):
    data = json.loads((APP / "i18n" / f"{locale}.json").read_text(encoding="utf-8"))
    en = _en()
    assert {"name", "dir", "script", "reviewed"} <= set(data["_meta"])
    categories = PLURAL_CATEGORIES.get(locale.split("-")[0])
    assert categories is not None, f"add {locale} to PLURAL_CATEGORIES"
    for key, value in data.items():
        if key == "_meta":
            continue
        assert key in en, f"unknown key {key}"
        assert _placeholders(value) == _placeholders(en[key]), key
        if isinstance(value, dict):
            assert set(value) <= categories, key


class TestChoosingLocale:
    ENABLED = ["en", "fr", "zh-Hans", "ar"]

    def test_param_beats_cookie_beats_header(self):
        assert i18n.choose_locale(self.ENABLED, "en", "fr", "ar", "zh") == "fr"
        assert i18n.choose_locale(self.ENABLED, "en", None, "ar", "fr") == "ar"
        assert i18n.choose_locale(self.ENABLED, "en", None, None, "fr;q=0.8, ar;q=0.9") == "ar"

    def test_default_when_nothing_matches(self):
        assert i18n.choose_locale(self.ENABLED, "en", "xx", "yy", "ja, ko") == "en"

    def test_language_only_fallback(self):
        assert i18n.match_locale("fr-CA", self.ENABLED) == "fr"
        assert i18n.match_locale("ar-EG", self.ENABLED) == "ar"

    @pytest.mark.parametrize("tag", ["zh", "zh-CN", "zh-SG", "zh-Hans", "zh-Hans-CN"])
    def test_simplified_chinese_tags(self, tag):
        assert i18n.match_locale(tag, self.ENABLED) == "zh-Hans"

    @pytest.mark.parametrize("tag", ["zh-TW", "zh-HK", "zh-Hant", "zh-Hant-TW"])
    def test_traditional_chinese_never_falls_to_simplified(self, tag):
        assert i18n.match_locale(tag, self.ENABLED) is None

    def test_disabled_locale_is_not_matched(self):
        assert i18n.match_locale("de", self.ENABLED) is None

    def test_accept_language_parsing(self):
        assert i18n.parse_accept_language("fr-CA,fr;q=0.9,en;q=0.5,*;q=0.1,de;q=0") == ["fr-CA", "fr", "en"]
        assert i18n.parse_accept_language("") == []


class TestRuntime:
    def test_t_fills_placeholders_and_falls_back_to_key(self):
        assert i18n.t("en", "public.events.kickerAlliance", alliance="MOD") == "Kingshot · MOD"
        assert i18n.t("en", "no.such.key") == "no.such.key"

    def test_unknown_placeholder_left_as_written(self):
        assert i18n.t("en", "public.events.kickerAlliance") == "Kingshot · {alliance}"

    def test_fallback_chain_ends_in_english(self):
        assert i18n.fallback_chain("fr-CA")[-1] == "en"

    def test_strings_for_prefix(self):
        strings = i18n.strings_for("en", ("public.feedback.",))
        assert strings and all(k.startswith("public.feedback.") for k in strings)

    def test_pseudo_locale_keeps_placeholders_and_tags(self):
        out = i18n.pseudo_localize("Last: <strong>{name}</strong> at {time}")
        assert "{name}" in out and "{time}" in out and "<strong>" in out
        assert out.startswith("[") and out.endswith("]") and len(out) > 40 * 0.9
        assert "Last" not in out

    def test_pseudo_catalogue_mirrors_english_keys(self):
        pseudo = i18n.catalogue(i18n.PSEUDO_LOCALE)
        assert set(pseudo) == set(_en())
        assert isinstance(pseudo["public.events.calItems"], dict)

    def test_json_for_script_tag_escapes(self):
        out = i18n.json_for_script_tag({"a": "</script><b>& "})
        assert "</script>" not in out and "<" not in out and "&" not in out and " " not in out
        assert json.loads(out) == {"a": "</script><b>& "}


class TestPages:
    @staticmethod
    def _block(html: str) -> dict:
        m = re.search(r'<script type="application/json" id="i18n">(.*?)</script>', html, re.S)
        assert m, "no i18n block"
        return json.loads(m.group(1))

    async def test_events_page_has_lang_dir_title_and_strings(self, client_no_session):
        r = await client_no_session.get("/events")
        assert r.status_code == 200
        assert '<html lang="en" dir="ltr">' in r.text
        assert "<title>Kingshot Event Schedule</title>" in r.text
        block = self._block(r.text)
        assert block["locale"] == "en" and block["dir"] == "ltr"
        assert block["strings"]["public.events.today"] == "Today"
        assert not any(k.startswith("public.feedback.") for k in block["strings"])
        assert r.headers["vary"] == "Accept-Language, Cookie"

    async def test_feedback_page_gets_its_own_strings(self, client_no_session):
        r = await client_no_session.get("/feedback")
        block = self._block(r.text)
        assert "public.feedback.intro" in block["strings"]
        assert "public.common.close" in block["strings"]
        assert "<title>Feedback &amp; Requests — Kingshot</title>" in r.text

    async def test_tenant_events_page(self, client_no_session, tenant):
        r = await client_no_session.get(f"/t/{tenant['slug']}/events")
        assert r.status_code == 200 and '<html lang="en"' in r.text

    async def test_lang_param_sets_cookie_only_for_enabled_locale(self, client_no_session):
        r = await client_no_session.get("/events?lang=en")
        assert "samaya_lang=en" in r.headers["set-cookie"]
        r = await client_no_session.get("/events?lang=xx")
        assert "set-cookie" not in r.headers and '<html lang="en"' in r.text

    async def test_pseudo_locale_only_when_enabled(self, client_no_session, monkeypatch):
        assert '<html lang="en"' in (await client_no_session.get("/events?lang=en-XA")).text
        monkeypatch.setenv("SAMAYA_PSEUDO_LOCALE", "1")
        r = await client_no_session.get("/events?lang=en-XA")
        assert '<html lang="en-XA" dir="ltr">' in r.text
        assert self._block(r.text)["strings"]["public.events.today"].startswith("[")

    async def test_static_assets_are_versioned(self, client_no_session):
        r = await client_no_session.get("/events")
        assert re.search(r'/static/i18n\.js\?v=', r.text)


class TestKingdomLanguages:
    @staticmethod
    async def _patch(client, tenant, **body):
        return await client.patch(f"/admin/api/kingdoms/{tenant['kingdom_id']}", json=body)

    async def test_defaults_to_english_only_and_lists_every_shipped_language(self, client, tenant):
        k = next(k for k in (await client.get("/admin/api/kingdoms")).json() if k["id"] == tenant["kingdom_id"])
        assert k["default_locale"] == "en" and k["enabled_locales"] == ["en"]
        assert {"en", "zh-Hans", "ar", "fr", "es", "tr", "ru", "de"} <= {loc["tag"] for loc in k["available_locales"]}
        assert next(loc for loc in k["available_locales"] if loc["tag"] == "ar")["dir"] == "rtl"

    async def test_enable_languages_and_choose_default(self, client, tenant, client_no_session):
        r = await self._patch(client, tenant, enabled_locales=["en", "fr", "de"], default_locale="fr")
        assert r.status_code == 200, r.text
        assert r.json()["enabled_locales"] == ["en", "de", "fr"] and r.json()["default_locale"] == "fr"
        page = await client_no_session.get("/events")
        assert '<html lang="fr" dir="ltr">' in page.text
        block = TestPages._block(page.text)
        assert [x["tag"] for x in block["locales"]] == ["en", "de", "fr"]  # English first, the rest alphabetical
        assert {x["tag"]: x["name"] for x in block["locales"]}["fr"] == "Français"
        assert block["strings"]["public.events.today"] != "Today"

    async def test_visitor_choice_order_with_enabled_languages(self, client, tenant, client_no_session):
        await self._patch(client, tenant, enabled_locales=["en", "fr", "de"], default_locale="en")
        get = client_no_session.get
        assert '<html lang="de"' in (await get("/events", headers={"Accept-Language": "de-DE,de;q=0.9"})).text
        assert '<html lang="en"' in (await get("/events", headers={"Accept-Language": "ja"})).text
        assert '<html lang="fr"' in (await get("/events?lang=fr", headers={"Accept-Language": "de"})).text
        client_no_session.cookies.clear()  # the ?lang=fr visit above set the language cookie
        assert '<html lang="en"' in (await get("/events?lang=es")).text  # es is shipped but not enabled

    async def test_cookie_keeps_the_language_on_the_next_visit(self, client, tenant, client_no_session):
        await self._patch(client, tenant, enabled_locales=["en", "fr"])
        first = await client_no_session.get("/events?lang=fr")
        assert "samaya_lang=fr" in first.headers["set-cookie"]
        second = await client_no_session.get("/events")  # the client keeps the cookie, like a browser
        assert '<html lang="fr"' in second.text

    async def test_single_language_hides_the_select(self, client_no_session):
        assert TestPages._block((await client_no_session.get("/events")).text)["locales"] == []

    async def test_arabic_is_right_to_left_and_zh_adds_its_font(self, client, tenant, client_no_session):
        await self._patch(client, tenant, enabled_locales=["en", "ar", "zh-Hans"], default_locale="ar")
        ar = (await client_no_session.get("/events")).text
        assert '<html lang="ar" dir="rtl">' in ar and "Noto+Sans+Arabic" in ar
        zh = (await client_no_session.get("/events?lang=zh-Hans")).text
        assert '<html lang="zh-Hans" dir="ltr">' in zh and "Noto+Sans+SC" in zh
        assert "Noto+Sans+SC" not in (await client_no_session.get("/events?lang=en")).text

    async def test_traditional_chinese_visitor_is_not_given_simplified(self, client, tenant, client_no_session):
        await self._patch(client, tenant, enabled_locales=["en", "zh-Hans"])
        r = await client_no_session.get("/events", headers={"Accept-Language": "zh-TW,zh;q=0.8"})
        assert '<html lang="zh-Hans"' in r.text  # zh;q=0.8 is plain Chinese, which is Simplified here
        r = await client_no_session.get("/events", headers={"Accept-Language": "zh-TW"})
        assert '<html lang="en"' in r.text

    async def test_validation(self, client, tenant):
        assert (await self._patch(client, tenant, enabled_locales=["en", "xx"])).status_code == 422
        assert (await self._patch(client, tenant, enabled_locales=[])).status_code == 422
        r = await self._patch(client, tenant, enabled_locales=["fr"])
        assert r.status_code == 422 and "default" in r.json()["detail"].lower()
        assert (await self._patch(client, tenant, default_locale="de")).status_code == 422

    async def test_back_to_english_only_stores_nothing(self, client, tenant, db_session):
        from models.db import Kingdom
        await self._patch(client, tenant, enabled_locales=["en", "fr"], default_locale="fr")
        r = await self._patch(client, tenant, enabled_locales=["en"], default_locale="en")
        assert r.status_code == 200
        k = await db_session.get(Kingdom, tenant["kingdom_id"])
        await db_session.refresh(k)
        assert k.default_locale is None and k.enabled_locales is None

    async def test_change_is_audited(self, client, tenant, db_session):
        from sqlalchemy import select

        from models.db import AuditLog
        await self._patch(client, tenant, enabled_locales=["en", "de"])
        row = (await db_session.execute(select(AuditLog).where(AuditLog.table_name == "kingdoms")
                                         .order_by(AuditLog.id.desc()))).scalars().first()
        assert "de" in row.after


class TestFontsAndRtlMeta:
    def test_script_fonts(self):
        assert i18n.script_font("zh-Hans") == "Noto Sans SC" and i18n.script_font("ar") == "Noto Sans Arabic"
        assert i18n.script_font("en") is None

    def test_arabic_is_rtl_everything_else_ltr(self):
        assert i18n.text_direction("ar") == "rtl"
        assert all(i18n.text_direction(t) == "ltr" for t in ("en", "fr", "de", "es", "tr", "ru", "zh-Hans"))
