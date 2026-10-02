"""Spec §71: theme rules, resolution, API, generated stylesheet and page head."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from services import theme_rules as rules
from services.fonts import BY_KEY, CATALOGUE, google_fonts_url, resolve_font, validate_font_key
from services.theme_templates import TEMPLATES
from services.themes import resolve_active_theme

UTC = timezone.utc
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
GOOD = {"name": "Autumn", "bg": "#F0F4F9", "accent": "#EBBD57", "accent_text": "#8A5700", "primary": "#151F32"}


class TestRules:

    @pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t["key"])
    def test_every_template_passes(self, template):
        assert rules.contrast_failures(template["bg"], template["accent_text"], template["primary"]) == []

    def test_dark_background_fails_body_text(self):
        failures = rules.contrast_failures("#333333", "#8A5700", "#151F32")
        assert any("body text" in f for f in failures)

    def test_light_accent_text_fails_on_white(self):
        assert any("Accent text on white" in f for f in rules.contrast_failures("#F0F4F9", "#EBBD57", "#151F32"))

    def test_light_primary_fails(self):
        assert any("primary" in f for f in rules.contrast_failures("#F0F4F9", "#8A5700", "#FFD54F"))

    def test_overlay_floor_keeps_text_readable_over_black(self):
        assert rules.contrast_failures("#F0F4F9", "#8A5700", "#151F32", 0.90) == []
        assert rules.contrast_failures("#F0F4F9", "#8A5700", "#151F32", 0.70) != []

    def test_overlay_bounds(self):
        assert str(rules.clean_overlay("0.9")) == "0.90"
        for bad in ("0.89", "1.01", "abc"):
            with pytest.raises(ValueError):
                rules.clean_overlay(bad)

    def test_name_rules(self):
        assert rules.clean_name("  Hi\x00 ") == "Hi"
        for bad in ("", "   ", "x" * 61):
            with pytest.raises(ValueError):
                rules.clean_name(bad)

    def test_values_are_normalised(self):
        out = rules.validate_theme_values({**GOOD, "bg": "#f0f4f9"})
        assert out["bg"] == "#F0F4F9" and str(out["banner_overlay"]) == "0.96"


class TestFonts:

    def test_catalogue_is_consistent(self):
        assert len(CATALOGUE) == len(BY_KEY)
        for f in CATALOGUE:
            assert len(f.weights) <= 3 and f.roles
            assert ("numerals" in f.roles) == f.tabular

    def test_role_rules(self):
        assert validate_font_key("cinzel", "heading") == "cinzel"
        assert validate_font_key("", "body") is None
        with pytest.raises(ValueError):
            validate_font_key("cinzel", "body")
        with pytest.raises(ValueError):
            validate_font_key("inter", "numerals")
        with pytest.raises(ValueError):
            validate_font_key("nope", "body")

    def test_stale_key_renders_as_default(self):
        assert resolve_font("removed-family", "body") is None

    def test_google_url(self):
        url = google_fonts_url([BY_KEY["inter"], BY_KEY["inter"], BY_KEY["cinzel"]])
        assert url.count("family=") == 2 and url.endswith("&display=swap") and "Inter:wght@400;600;700" in url


def row(i, theme_id, start, end, priority=50):
    return SimpleNamespace(id=i, theme_id=theme_id, start_utc=start, end_utc=end, priority_level=priority)


class TestResolution:
    d = timedelta(days=1)

    def test_none_falls_back_to_base_then_defaults(self):
        assert resolve_active_theme(7, [], NOW) == 7
        assert resolve_active_theme(None, [], NOW) is None

    def test_window_is_half_open(self):
        r = row(1, 3, NOW, NOW + self.d)
        assert resolve_active_theme(None, [r], NOW) == 3
        assert resolve_active_theme(None, [r], NOW + self.d) is None

    def test_priority_then_latest_start_then_id(self):
        low = row(1, 1, NOW - self.d, NOW + self.d, 10)
        high = row(2, 2, NOW - 3 * self.d, NOW + self.d, 100)
        assert resolve_active_theme(None, [low, high], NOW) == 2
        later = row(3, 3, NOW - self.d, NOW + self.d, 100)
        assert resolve_active_theme(None, [high, later], NOW) == 3
        tie = row(4, 4, NOW - self.d, NOW + self.d, 100)
        assert resolve_active_theme(None, [later, tie], NOW) == 4

    def test_naive_datetimes_are_treated_as_utc(self):
        r = row(1, 3, datetime(2026, 10, 2), datetime(2026, 10, 3))
        assert resolve_active_theme(None, [r], NOW) == 3


async def make_theme(client, kingdom_id, **over):
    r = await client.post("/admin/api/themes", json={"kingdom_id": kingdom_id, **GOOD, **over})
    assert r.status_code == 201, r.text
    return r.json()


class TestThemeApi:

    async def test_create_list_update(self, client, tenant):
        t = await make_theme(client, tenant["kingdom_id"], font_heading="cinzel")
        assert t["is_base"] is False and t["scheduled_count"] == 0
        r = await client.patch(f"/admin/api/themes/{t['id']}", json={"name": "Autumn 2", "font_heading": None})
        assert r.status_code == 200 and r.json()["name"] == "Autumn 2" and r.json()["font_heading"] is None
        listing = (await client.get("/admin/api/themes", params={"kingdom_id": tenant["kingdom_id"]})).json()
        assert [x["name"] for x in listing] == ["Autumn 2"]

    async def test_contrast_failure_is_422(self, client, tenant):
        r = await client.post("/admin/api/themes", json={"kingdom_id": tenant["kingdom_id"], **GOOD, "bg": "#222222"})
        assert r.status_code == 422 and "body text" in r.json()["detail"]

    async def test_patch_rechecks_contrast_against_stored_values(self, client, tenant):
        t = await make_theme(client, tenant["kingdom_id"])
        r = await client.patch(f"/admin/api/themes/{t['id']}", json={"accent_text": "#EBBD57"})
        assert r.status_code == 422

    async def test_font_role_and_unknown_font(self, client, tenant):
        for font in ({"font_body": "cinzel"}, {"font_numerals": "inter"}, {"font_heading": "nope"}):
            r = await client.post("/admin/api/themes", json={"kingdom_id": tenant["kingdom_id"], **GOOD, **font})
            assert r.status_code == 422

    async def test_duplicate_name(self, client, tenant):
        await make_theme(client, tenant["kingdom_id"])
        r = await client.post("/admin/api/themes", json={"kingdom_id": tenant["kingdom_id"], **GOOD})
        assert r.status_code == 422 and "already has a theme" in r.json()["detail"]

    async def test_delete_blocked_while_base_or_scheduled(self, client, tenant):
        kid = tenant["kingdom_id"]
        t = await make_theme(client, kid)
        assert (await client.patch(f"/admin/api/kingdoms/{kid}", json={"default_theme_id": t["id"]})).status_code == 200
        r = await client.delete(f"/admin/api/themes/{t['id']}")
        assert r.status_code == 409 and "base theme" in r.json()["detail"]
        assert (await client.patch(f"/admin/api/themes/{t['id']}", json={"archived": True})).status_code == 409
        assert (await client.patch(f"/admin/api/kingdoms/{kid}", json={"default_theme_id": None})).json()["default_theme_id"] is None
        s = await client.post("/admin/api/scheduled-themes", json={
            "kingdom_id": kid, "theme_id": t["id"], "start_utc": "2026-12-01T00:00:00Z", "end_utc": "2026-12-08T00:00:00Z"})
        assert s.status_code == 201
        assert (await client.delete(f"/admin/api/themes/{t['id']}")).status_code == 409
        assert (await client.delete(f"/admin/api/scheduled-themes/{s.json()['id']}")).status_code == 204
        assert (await client.delete(f"/admin/api/themes/{t['id']}")).status_code == 204

    async def test_base_theme_must_belong_and_not_be_archived(self, client, tenant):
        kid = tenant["kingdom_id"]
        t = await make_theme(client, kid)
        await client.patch(f"/admin/api/themes/{t['id']}", json={"archived": True})
        assert (await client.patch(f"/admin/api/kingdoms/{kid}", json={"default_theme_id": t["id"]})).status_code == 422
        assert (await client.patch(f"/admin/api/kingdoms/{kid}", json={"default_theme_id": 9999})).status_code == 422

    async def test_schedule_validation_and_overlap_flag(self, client, tenant):
        kid = tenant["kingdom_id"]
        t = await make_theme(client, kid)
        body = {"kingdom_id": kid, "theme_id": t["id"]}
        bad = await client.post("/admin/api/scheduled-themes", json={
            **body, "start_utc": "2026-12-08T00:00:00Z", "end_utc": "2026-12-01T00:00:00Z"})
        assert bad.status_code == 422
        a = (await client.post("/admin/api/scheduled-themes", json={
            **body, "start_utc": "2026-12-01T00:00:00Z", "end_utc": "2026-12-08T00:00:00Z"})).json()
        b = (await client.post("/admin/api/scheduled-themes", json={
            **body, "start_utc": "2026-12-05T00:00:00Z", "end_utc": "2026-12-12T00:00:00Z"})).json()
        assert b["overlaps_same_priority"] == [a["id"]]
        c = (await client.post("/admin/api/scheduled-themes", json={
            **body, "start_utc": "2026-12-05T00:00:00Z", "end_utc": "2026-12-12T00:00:00Z", "priority_level": 100})).json()
        assert c["overlaps_same_priority"] == []

    async def test_catalogue_and_templates(self, client):
        assert len((await client.get("/admin/api/fonts")).json()) == len(CATALOGUE)
        assert len((await client.get("/admin/api/theme-templates")).json()) == len(TEMPLATES)

    async def test_superadmin_only(self, tenant, make_user_and_client):
        kid = tenant["kingdom_id"]
        for role in ("owner", "coordinator", "viewer"):
            other, _ = await make_user_and_client(
                tenant_grants=[(tenant["id"], role)], kingdom_grants=[], discord_id=f"d-{role}")
            assert (await other.get("/admin/api/themes", params={"kingdom_id": kid})).status_code == 403
            assert (await other.post("/admin/api/themes", json={"kingdom_id": kid, **GOOD})).status_code == 403
            assert (await other.get("/admin/api/fonts")).status_code == 403
            assert (await other.get("/admin/api/scheduled-themes", params={"kingdom_id": kid})).status_code == 403

    async def test_writes_are_audited(self, client, tenant, db_engine):
        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
        from models.db import AuditLog
        t = await make_theme(client, tenant["kingdom_id"])
        await client.patch(f"/admin/api/themes/{t['id']}", json={"name": "Renamed"})
        async with async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)() as s:
            rows = (await s.execute(select(AuditLog).where(AuditLog.table_name == "themes"))).scalars().all()
        assert [r.action for r in rows] == ["create", "update"]
        assert '"name": "Renamed"' in rows[1].after and '"name": "Autumn"' in rows[1].before


class TestPublic:

    async def test_stylesheet(self, client, client_no_session, tenant):
        t = await make_theme(client, tenant["kingdom_id"], font_body="inter", font_heading="cinzel", font_numerals="jetbrains-mono")
        r = await client_no_session.get(f"/theme/{t['id']}.css")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/css")
        assert "immutable" in r.headers["cache-control"]
        css = r.text
        assert css.startswith(":root {") and "--bg: #F0F4F9;" in css and "--gold: #EBBD57;" in css
        assert "--font-body: 'Inter'" in css and "--font-heading: 'Cinzel'" in css and "--mono: 'JetBrains Mono'" in css
        assert css.count("{") == 1 and css.count("}") == 1
        assert (await client_no_session.get("/theme/9999.css")).status_code == 404

    async def test_stylesheet_with_stale_font_key_still_renders(self, client, client_no_session, tenant, db_engine):
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
        from models.db import Theme
        t = await make_theme(client, tenant["kingdom_id"], font_body="inter")
        async with async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)() as s:
            (await s.get(Theme, t["id"])).font_body = "removed"
            await s.commit()
        css = (await client_no_session.get(f"/theme/{t['id']}.css")).text
        assert "--font-body" not in css and "--gold:" in css

    async def test_invalid_row_serves_empty_stylesheet(self, client, client_no_session, tenant, db_engine):
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
        from models.db import Theme
        t = await make_theme(client, tenant["kingdom_id"])
        async with async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)() as s:
            (await s.get(Theme, t["id"])).bg = "#000000"
            await s.commit()
        r = await client_no_session.get(f"/theme/{t['id']}.css")
        assert r.status_code == 200 and r.text == "" and r.headers["cache-control"] == "no-store"

    async def test_page_without_theme_keeps_shipped_fonts(self, client_no_session, tenant):
        html = (await client_no_session.get("/events")).text
        assert "family=Noto+Sans" in html and "IBM+Plex+Mono" in html and "/theme/" not in html
        assert "<!--theme-head-->" not in html

    async def test_base_theme_is_linked_in_the_page(self, client, client_no_session, tenant):
        kid = tenant["kingdom_id"]
        t = await make_theme(client, kid, font_heading="cinzel")
        await client.patch(f"/admin/api/kingdoms/{kid}", json={"default_theme_id": t["id"]})
        html = (await client_no_session.get("/events")).text
        assert f'href="/theme/{t["id"]}.css?v=' in html and "family=Cinzel" in html
        assert html.index("/static/events.css") < html.index(f"/theme/{t['id']}.css")

    async def test_scheduled_window_overrides_base(self, client, client_no_session, tenant):
        kid = tenant["kingdom_id"]
        base = await make_theme(client, kid, name="Base")
        fest = await make_theme(client, kid, name="Festival")
        await client.patch(f"/admin/api/kingdoms/{kid}", json={"default_theme_id": base["id"]})
        now = datetime.now(UTC)
        await client.post("/admin/api/scheduled-themes", json={
            "kingdom_id": kid, "theme_id": fest["id"],
            "start_utc": (now - timedelta(hours=1)).isoformat(), "end_utc": (now + timedelta(hours=1)).isoformat()})
        html = (await client_no_session.get("/events")).text
        assert f'/theme/{fest["id"]}.css' in html and f'/theme/{base["id"]}.css' not in html
