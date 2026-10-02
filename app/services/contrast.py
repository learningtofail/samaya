"""WCAG 2.x contrast math for brand colors (spec §63.1).

The platform never rejects a color for readability: every 6-digit hex color
reaches at least 4.5:1 against either pure black or pure white, so
`pick_ink` always has a readable answer. The public page mirrors this in
`brandInk()` (events-public.js); tests/frontend and tests/test_contrast.py
share one fixture table so the two cannot drift.
"""
from services.validators import parse_hex_color

WHITE = "#FFFFFF"
BLACK = "#000000"
# WCAG 1.4.11 minimum for a non-text UI element (a stripe or chip border).
MIN_UI_CONTRAST = 3.0


def relative_luminance(hex_color: str) -> float:
    h = parse_hex_color(hex_color)[1:]
    channels = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255
        channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: str, b: str) -> float:
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def pick_ink(background: str) -> str:
    """Black or white, whichever reads better on `background`."""
    return BLACK if contrast_ratio(background, BLACK) >= contrast_ratio(background, WHITE) else WHITE


def faint_on_white_note(color: str | None) -> str | None:
    """A readable hint, not a rejection: a very light brand color shows as a
    faint stripe on the white public page (docs/alliance-branding-guidelines.md)."""
    if not color:
        return None
    ratio = contrast_ratio(color, WHITE)
    if ratio >= MIN_UI_CONTRAST:
        return None
    return f"{color} is very light against the white page ({ratio:.1f}:1, 3:1 or more shows clearly). Pick a darker color if its stripe looks faint."
