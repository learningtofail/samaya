"""Serve the public HTML pages in the visitor's language (spec §72.3, §72.4).

The static file keeps its English text, so it reads correctly on its own. This
renderer sets `<html lang dir>` and `<title>`, and embeds the page's strings as
`<script type="application/json" id="i18n">`; `static/i18n.js` applies them
before first paint, with no extra request.
"""
import html as html_lib
import re
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse

from services.i18n import (
    LANG_COOKIE, choose_locale, enabled_locales, json_for_script_tag, match_locale,
    strings_for, t, text_direction,
)
from services.static_assets import bust_static_cache

_HTML_TAG_RE = re.compile(r"<html\b[^>]*>", re.IGNORECASE)
_TITLE_RE = re.compile(r"<title>.*?</title>", re.IGNORECASE | re.DOTALL)
_ONE_YEAR = 365 * 24 * 3600
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def render_public_page(
    request: Request, filename: str, prefixes: tuple[str, ...], title_key: str | None = None,
    default_locale: str = "en",
) -> HTMLResponse:
    with open(STATIC_DIR / filename, encoding="utf-8") as fh:
        page = bust_static_cache(fh.read())

    enabled = enabled_locales()
    lang_param = request.query_params.get("lang")
    locale = choose_locale(
        enabled, default_locale, lang_param,
        request.cookies.get(LANG_COOKIE), request.headers.get("accept-language"),
    )
    direction = text_direction(locale)

    page = _HTML_TAG_RE.sub(f'<html lang="{locale}" dir="{direction}">', page, count=1)
    if title_key:
        title = html_lib.escape(t(locale, title_key), quote=False)
        page = _TITLE_RE.sub(lambda _m: f"<title>{title}</title>", page, count=1)
    block = json_for_script_tag(
        {"locale": locale, "dir": direction, "strings": strings_for(locale, prefixes, default_locale)}
    )
    page = page.replace("</head>", f'<script type="application/json" id="i18n">{block}</script>\n</head>', 1)

    response = HTMLResponse(page)
    response.headers["Vary"] = "Accept-Language, Cookie"
    if lang_param and match_locale(lang_param, enabled) == locale:
        response.set_cookie(LANG_COOKIE, locale, max_age=_ONE_YEAR, samesite="lax", path="/")
    return response
