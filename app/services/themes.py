"""Theme resolution and stylesheet generation (spec §71.5, §71.6).

`resolve_active_theme` is pure with an injected clock. `theme_css` emits only
`:root` declarations built from validated values, so there is nothing to
escape beyond hex colors, catalogue font stacks and two numbers.
"""
import logging
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import Kingdom, ScheduledTheme, Theme
from services.fonts import Font, google_fonts_url, resolve_font
from services.theme_rules import contrast_failures
from services.time_utils import ensure_utc
from services.validators import parse_hex_color

logger = logging.getLogger(__name__)

DEFAULT_STYLESHEET = "default"


def resolve_active_theme(base_theme_id: int | None, rows: list, now: datetime) -> int | None:
    """Theme id in force at `now`, or None for the shipped defaults.

    `rows` are ScheduledTheme-like objects (theme_id, start_utc, end_utc,
    priority_level, id). Highest priority wins, then the latest start, then the
    highest id; no active window falls back to the base theme."""
    now = ensure_utc(now)
    active = [r for r in rows if ensure_utc(r.start_utc) <= now < ensure_utc(r.end_utc)]
    if not active:
        return base_theme_id
    active.sort(key=lambda r: (r.priority_level, ensure_utc(r.start_utc), r.id), reverse=True)
    return active[0].theme_id


async def active_theme(db: AsyncSession, kingdom: Kingdom | None, now: datetime) -> Theme | None:
    """The Theme to render for `kingdom` now. Never raises: a missing or
    invalid theme row logs and yields None (spec §71.10)."""
    if kingdom is None:
        return None
    rows = (await db.execute(
        select(ScheduledTheme).where(
            ScheduledTheme.kingdom_id == kingdom.id,
            ScheduledTheme.start_utc <= now, ScheduledTheme.end_utc > now,
        )
    )).scalars().all()
    theme_id = resolve_active_theme(kingdom.default_theme_id, list(rows), now)
    if theme_id is None:
        return None
    theme = await db.get(Theme, theme_id)
    if theme is None or not is_renderable(theme):
        logger.error("Theme %s is missing or invalid; using the shipped defaults", theme_id)
        return None
    return theme


def is_renderable(theme: Theme) -> bool:
    try:
        for field in (theme.bg, theme.accent, theme.accent_text, theme.primary):
            parse_hex_color(field)
        return not contrast_failures(theme.bg, theme.accent_text, theme.primary, float(theme.banner_overlay))
    except (ValueError, TypeError):
        return False


def theme_fonts(theme: Theme) -> dict[str, Font | None]:
    return {
        "heading": resolve_font(theme.font_heading, "heading"),
        "body": resolve_font(theme.font_body, "body"),
        "numerals": resolve_font(theme.font_numerals, "numerals"),
    }


def theme_css(theme: Theme) -> str:
    """The generated stylesheet for one theme."""
    fonts = theme_fonts(theme)
    lines = [
        f"  --bg: {parse_hex_color(theme.bg)};",
        "  --bg-wash: color-mix(in oklch, var(--gold) 12%, var(--bg));",
        f"  --gold: {parse_hex_color(theme.accent)};",
        f"  --gold-ink: {parse_hex_color(theme.accent_text)};",
        f"  --navy: {parse_hex_color(theme.primary)};",
        "  --gold-deep: color-mix(in oklch, var(--gold) 85%, black);",
        "  --navy-hover: color-mix(in oklch, var(--navy) 88%, white);",
        f"  --hero-overlay: {Decimal(str(theme.banner_overlay)).quantize(Decimal('0.01'))};",
    ]
    if fonts["body"]:
        lines.append(f"  --font-body: {fonts['body'].css_stack};")
    if fonts["heading"]:
        lines.append(f"  --font-heading: {fonts['heading'].css_stack};")
    if fonts["numerals"]:
        lines.append(f"  --mono: {fonts['numerals'].css_stack};")
    return ":root {\n" + "\n".join(lines) + "\n}\n"


def theme_head_html(theme: Theme | None, css_version: str) -> str:
    """The `<head>` fragment for the events page: the fonts request and the
    theme stylesheet link. With no theme, the shipped fonts load as before."""
    preconnect = ('<link rel="preconnect" href="https://fonts.googleapis.com">\n'
                  '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n')
    if theme is None:
        shipped = ("https://fonts.googleapis.com/css2?family=Noto+Sans:wght@400;500;600;700"
                   "&family=IBM+Plex+Mono:wght@400;500;600&display=swap")
        return f'{preconnect}<link rel="stylesheet" href="{shipped}">\n'
    fonts = theme_fonts(theme)
    # The page body and numbers fall back to the shipped families when a slot is empty.
    from services.fonts import BY_KEY, DEFAULT_BODY_KEY, DEFAULT_NUMERALS_KEY
    wanted = [fonts["body"] or BY_KEY[DEFAULT_BODY_KEY], fonts["numerals"] or BY_KEY[DEFAULT_NUMERALS_KEY]]
    if fonts["heading"]:
        wanted.append(fonts["heading"])
    url = google_fonts_url(wanted)
    stamp = int(ensure_utc(theme.updated_at).timestamp()) if theme.updated_at else 0
    return (f'{preconnect}<link rel="stylesheet" href="{url}">\n'
            f'<link rel="stylesheet" href="/theme/{theme.id}.css?v={stamp}.{css_version}">\n')
