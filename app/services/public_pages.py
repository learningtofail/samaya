"""Serve the public HTML pages in the visitor's language (spec §72.3, §72.4).

The static file keeps its English text, so it reads correctly on its own. This
renderer sets `<html lang dir>` and `<title>`, and embeds the page's strings as
`<script type="application/json" id="i18n">`; `static/i18n.js` applies them
before first paint, with no extra request.
"""
import html as html_lib
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import Kingdom, Theme, User
from services.sessions import SESSION_COOKIE_NAME, read_session_token

from services.i18n import (
    LANG_COOKIE, choose_locale, json_for_script_tag, kingdom_locale_settings, locale_choices,
    match_locale, script_font, strings_for, t, text_direction,
)
from services.static_assets import STATIC_ASSET_VERSION, bust_static_cache
from services.themes import active_theme, is_renderable, theme_head_html

_HTML_TAG_RE = re.compile(r"<html\b[^>]*>", re.IGNORECASE)
_TITLE_RE = re.compile(r"<title>.*?</title>", re.IGNORECASE | re.DOTALL)
THEME_MARKER = "<!--theme-head-->"
_ONE_YEAR = 365 * 24 * 3600
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


async def get_public_kingdom(db: AsyncSession):
    """This deployment is one Kingdom, so "the Kingdom" is the first row (as in
    /api/kingdom-branding); None before one exists."""
    return (await db.execute(select(Kingdom).order_by(Kingdom.id).limit(1))).scalar_one_or_none()


async def get_public_theme(db: AsyncSession, kingdom):
    """The theme in force now for the page, or None for the shipped look."""
    return await active_theme(db, kingdom, datetime.now(timezone.utc))


async def get_request_theme(request: Request, db: AsyncSession, kingdom):
    """(theme, is_preview). `?preview_theme={id}` shows that theme to a signed-in
    superadmin only (spec §71.6); anyone else gets the ordinary resolution."""
    raw = request.query_params.get("preview_theme")
    if raw and raw.isdigit() and kingdom is not None:
        user_id = read_session_token(request.cookies.get(SESSION_COOKIE_NAME, ""))
        user = await db.get(User, user_id) if user_id else None
        if user is not None and user.is_superadmin:
            theme = await db.get(Theme, int(raw))
            if theme is not None and theme.kingdom_id == kingdom.id and is_renderable(theme):
                return theme, True
    return await get_public_theme(db, kingdom), False


def mark_preview(response: HTMLResponse) -> HTMLResponse:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex"
    return response


def render_public_page(
    request: Request, filename: str, prefixes: tuple[str, ...], title_key: str | None = None,
    kingdom=None, theme=None,
) -> HTMLResponse:
    with open(STATIC_DIR / filename, encoding="utf-8") as fh:
        page = bust_static_cache(fh.read())

    default_locale, enabled = kingdom_locale_settings(kingdom)
    lang_param = request.query_params.get("lang")
    locale = choose_locale(
        enabled, default_locale, lang_param,
        request.cookies.get(LANG_COOKIE), request.headers.get("accept-language"),
    )
    direction = text_direction(locale)

    page = page.replace(THEME_MARKER, theme_head_html(theme, STATIC_ASSET_VERSION), 1)
    page = _HTML_TAG_RE.sub(f'<html lang="{locale}" dir="{direction}">', page, count=1)
    if title_key:
        title = html_lib.escape(t(locale, title_key), quote=False)
        page = _TITLE_RE.sub(lambda _m: f"<title>{title}</title>", page, count=1)
    font = script_font(locale)
    font_link = (f'<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family='
                 f'{font.replace(" ", "+")}:wght@400;500;600;700&display=swap">\n') if font else ""
    block = json_for_script_tag(
        {"locale": locale, "dir": direction, "strings": strings_for(locale, prefixes, default_locale),
         # The language select appears only when there is a choice (spec §72.3).
         "locales": locale_choices(enabled) if len(enabled) > 1 else []}
    )
    page = page.replace("</head>", f'{font_link}<script type="application/json" id="i18n">{block}</script>\n</head>', 1)

    response = HTMLResponse(page)
    response.headers["Vary"] = "Accept-Language, Cookie"
    if lang_param and match_locale(lang_param, enabled) == locale:
        response.set_cookie(LANG_COOKIE, locale, max_age=_ONE_YEAR, samesite="lax", path="/")
    return response
