"""Cache-busting for `/static/*` references inside a server-rendered HTML
shell — extracted out of `routers/admin/ui.py` (spec §23) once the public
events page (spec §62 redesign) also started loading its own external
`/static/*.css`/`/static/*.js` files instead of carrying everything inline.
Before that, `events.html` had nothing under `/static/` to go stale, so this
was purely an admin.html concern; now both the admin shell and the public
events shell need the exact same fix for the exact same reason, so it lives
here instead of one router reaching into another's module.

Cloudflare (in front of this deployment) caches `/static/*.js` and
`/static/*.css` at the edge by file extension for hours, independent of how
fresh the origin's copy is — a plain redeploy of a static file is not enough
to bust it, and even a client-side `cache: "no-store"` fetch still hits the
stale edge copy. Bump STATIC_ASSET_VERSION whenever any file under
/static actually changes, so every reference gets a new query string and
Cloudflare (and browsers) treat it as a fresh URL instead of serving a stale
one. Track this independently of the spec/API version — it only needs to
move when a static asset actually changes.
"""
import re

STATIC_ASSET_VERSION = "1.67.1"

_STATIC_ASSET_REF = re.compile(r'(src|href)="(/static/[^"?]+)"')


def bust_static_cache(html: str) -> str:
    return _STATIC_ASSET_REF.sub(
        lambda m: f'{m.group(1)}="{m.group(2)}?v={STATIC_ASSET_VERSION}"', html
    )
