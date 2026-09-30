"""Unit tests for services/static_assets.py — the cache-busting helper
shared by routers/admin/ui.py (admin.html) and routers/events.py (the
public events.html, since spec §62's redesign gave it its own external
events.css/events-public.js). See tests/test_ui.py for the same behavior
exercised through admin/ui.py's re-exported names.
"""
from services.static_assets import STATIC_ASSET_VERSION, bust_static_cache


class TestBustStaticCache:
    def test_appends_version_to_script_src(self):
        html = '<script src="/static/events-public.js"></script>'
        result = bust_static_cache(html)
        assert result == f'<script src="/static/events-public.js?v={STATIC_ASSET_VERSION}"></script>'

    def test_appends_version_to_stylesheet_href(self):
        html = '<link rel="stylesheet" href="/static/events.css">'
        result = bust_static_cache(html)
        assert f'href="/static/events.css?v={STATIC_ASSET_VERSION}"' in result

    def test_busts_every_static_reference_in_a_full_page(self):
        html = (
            '<link rel="stylesheet" href="/static/events.css">\n'
            '<script src="/static/events-public.js"></script>\n'
        )
        result = bust_static_cache(html)
        assert result.count(f"?v={STATIC_ASSET_VERSION}") == 2

    def test_does_not_touch_external_font_cdn(self):
        # events.html's Google Fonts <link> isn't served by this app and
        # doesn't sit behind the same edge cache — only local /static/
        # paths need busting.
        html = '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Sans">'
        assert bust_static_cache(html) == html

    def test_idempotent_on_a_path_that_already_has_a_query_string(self):
        html = '<script src="/static/events-public.js?v=already-set"></script>'
        assert bust_static_cache(html) == html
