"""Unit tests for routers/admin/ui.py's static-asset cache-busting.

Not an HTTP-level test of GET / itself: admin_home() reads a hardcoded
/app/static/admin.html path that only exists inside the built container,
not in a bare pytest checkout — so this exercises _bust_static_cache()
directly on representative HTML fragments instead.

Spec §62 moved the actual implementation to services/static_assets.py
(shared with routers/events.py, once the public events page also started
loading external /static/*.css and /static/*.js of its own) — this module
now just re-exports both names under their original spelling so this file
keeps testing them the way anything importing from routers.admin.ui would
still see them. See tests/test_static_assets.py for the implementation's
own tests.
"""
from routers.admin.ui import STATIC_ASSET_VERSION, _bust_static_cache


class TestBustStaticCache:
    def test_appends_version_to_script_src(self):
        html = '<script src="/static/js/announcements.js"></script>'
        result = _bust_static_cache(html)
        assert result == f'<script src="/static/js/announcements.js?v={STATIC_ASSET_VERSION}"></script>'

    def test_appends_version_to_stylesheet_href(self):
        html = '<link rel="stylesheet" href="/static/admin.css">'
        result = _bust_static_cache(html)
        assert f'href="/static/admin.css?v={STATIC_ASSET_VERSION}"' in result

    def test_busts_every_static_reference_in_a_full_page(self):
        html = (
            '<link rel="stylesheet" href="/static/vendor/patternfly/patternfly.min.css">\n'
            '<script src="/static/js/common.js"></script>\n'
            '<script src="/static/js/events.js"></script>\n'
        )
        result = _bust_static_cache(html)
        assert result.count(f"?v={STATIC_ASSET_VERSION}") == 3

    def test_does_not_touch_non_static_references(self):
        # External vendor CDNs, anchors, and API endpoints aren't served by
        # this app and don't sit behind the same edge cache — only local
        # /static/ paths need busting.
        html = (
            '<a href="/admin/api/announcements">link</a>\n'
            '<script src="https://cdn.example.com/lib.js"></script>\n'
        )
        assert _bust_static_cache(html) == html

    def test_idempotent_on_a_path_that_already_has_a_query_string(self):
        # Guards against ever double-appending if this is called twice, or
        # if a future asset reference is hand-written with its own ?v=.
        html = '<script src="/static/js/common.js?v=already-set"></script>'
        assert _bust_static_cache(html) == html
