"""Theme validation (spec §71.1): the contrast rules and value checks that run
on save. Pure functions, shared by the API and (as fixtures) the frontend."""
import re
from decimal import Decimal

from services.contrast import WHITE, contrast_ratio, pick_ink
from services.fonts import validate_font_key
from services.validators import parse_hex_color

# The shipped `--ink` and `--muted` tokens of events.css as sRGB hex. A theme
# never changes them, so its background is the only variable in the pairing.
INK = "#151B24"
MUTED = "#474D58"
INK_MIN = 7.0
MUTED_MIN = 4.5
TEXT_MIN = 4.5

OVERLAY_MIN = Decimal("0.90")
OVERLAY_MAX = Decimal("1.00")
OVERLAY_DEFAULT = Decimal("0.96")
NAME_MAX = 60
# `--bg` is a near-white page; the overlay is white at this strength over the banner.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def worst_case_overlay_background(overlay: float) -> str:
    """Hero background when the banner is pure black and the white overlay has
    the given strength: the lowest contrast any image can produce."""
    v = round(255 * float(overlay))
    return f"#{v:02X}{v:02X}{v:02X}"


def contrast_failures(bg: str, accent_text: str, primary: str, overlay: float = 0.96) -> list[str]:
    """Human-readable failures, empty when the palette passes every rule."""
    out: list[str] = []
    ratio = contrast_ratio(bg, INK)
    if ratio < INK_MIN:
        out.append(f"Background and body text: {ratio:.1f}:1, needs {INK_MIN:g}:1. Use a lighter background.")
    ratio = contrast_ratio(bg, MUTED)
    if ratio < MUTED_MIN:
        out.append(f"Background and secondary text: {ratio:.1f}:1, needs {MUTED_MIN:g}:1. Use a lighter background.")
    for label, against in (("white", WHITE), ("the background", bg)):
        ratio = contrast_ratio(accent_text, against)
        if ratio < TEXT_MIN:
            out.append(f"Accent text on {label}: {ratio:.1f}:1, needs {TEXT_MIN:g}:1. Use a darker accent text color.")
    ratio = contrast_ratio(primary, WHITE)
    if ratio < TEXT_MIN:
        out.append(f"White text on the primary color: {ratio:.1f}:1, needs {TEXT_MIN:g}:1. Use a darker primary.")
    hero = worst_case_overlay_background(overlay)
    for label, ink, floor in (("body", INK, INK_MIN), ("secondary", MUTED, MUTED_MIN)):
        ratio = contrast_ratio(hero, ink)
        if ratio < floor:
            out.append(f"Banner overlay {overlay:g}: {label} text over a black image is {ratio:.1f}:1, needs {floor:g}:1. Raise the overlay.")
    return out


def clean_name(value: str) -> str:
    v = _CONTROL_RE.sub("", str(value)).strip()
    if not v:
        raise ValueError("Name is required")
    if len(v) > NAME_MAX:
        raise ValueError(f"Name is at most {NAME_MAX} characters")
    return v


def clean_overlay(value) -> Decimal:
    try:
        d = Decimal(str(value)).quantize(Decimal("0.01"))
    except Exception as exc:
        raise ValueError("Overlay must be a number from 0.90 to 1.00") from exc
    if not OVERLAY_MIN <= d <= OVERLAY_MAX:
        raise ValueError("Overlay must be from 0.90 to 1.00")
    return d


def validate_theme_values(values: dict) -> dict:
    """Normalise and check one theme's fields; raise ValueError naming the
    first problem. Returns the cleaned dict (hex upper-cased)."""
    out = dict(values)
    out["name"] = clean_name(values["name"])
    for field in ("bg", "accent", "accent_text", "primary"):
        out[field] = parse_hex_color(values[field])
    for field, role in (("font_heading", "heading"), ("font_body", "body"), ("font_numerals", "numerals")):
        out[field] = validate_font_key(values.get(field), role)
    out["banner_overlay"] = clean_overlay(values.get("banner_overlay", OVERLAY_DEFAULT))
    failures = contrast_failures(out["bg"], out["accent_text"], out["primary"], float(out["banner_overlay"]))
    if failures:
        raise ValueError(" ".join(failures))
    return out


def accent_ink(accent: str) -> str:
    """Text color on an accent fill (spec §63.1)."""
    return pick_ink(accent)
