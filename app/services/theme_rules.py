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
SURFACE_2 = "#F8FAFD"  # shipped --surface-2 (day headers, table bands)
DEFAULT_TINT = "#DCE9FD"  # shipped --tint
RADIUS_RANGE = (4, 14)
ART_HEIGHT_RANGE = (240, 360)
INK_MIN = 7.0
MUTED_MIN = 4.5
TEXT_MIN = 4.5

OVERLAY_MIN = Decimal("0.90")
OVERLAY_MAX = Decimal("1.00")
OVERLAY_DEFAULT = Decimal("0.96")
NAME_MAX = 60
# `--bg` is a near-white page; the overlay is white at this strength over the banner.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def worst_case_overlay_background(overlay: float, wash: str = "#FFFFFF") -> str:
    """Hero background when the banner is pure black and the wash color covers it
    at the given strength: the lowest contrast any image can produce."""
    h = parse_hex_color(wash)[1:]
    ch = [round(int(h[i:i + 2], 16) * float(overlay)) for i in (0, 2, 4)]
    return "#{:02X}{:02X}{:02X}".format(*ch)


def contrast_failures(
    bg: str, accent_text: str, primary: str, overlay: float = 0.96,
    hero_wash: str = "#FFFFFF", tint: str | None = None,
) -> list[str]:
    """Human-readable failures, empty when the palette passes every rule."""
    out: list[str] = []

    def check(label: str, a: str, b: str, floor: float, fix: str) -> None:
        ratio = contrast_ratio(a, b)
        if ratio < floor:
            out.append(f"{label}: {ratio:.1f}:1, needs {floor:g}:1. {fix}")

    hero = worst_case_overlay_background(overlay, hero_wash)
    check("Background and body text", bg, INK, INK_MIN, "Use a lighter background.")
    check("Background and secondary text", bg, MUTED, MUTED_MIN, "Use a lighter background.")
    check("Accent text on white", accent_text, WHITE, TEXT_MIN, "Use a darker accent text color.")
    check("Accent text on the background", accent_text, bg, TEXT_MIN, "Use a darker accent text color.")
    check("Accent text on table bands", accent_text, SURFACE_2, TEXT_MIN, "Use a darker accent text color.")
    check("White text on the primary color", primary, WHITE, TEXT_MIN, "Use a darker primary.")
    check(f"Body text over a black banner at overlay {overlay:g}", hero, INK, INK_MIN, "Raise the overlay or use a lighter wash.")
    check(f"Secondary text over a black banner at overlay {overlay:g}", hero, MUTED, MUTED_MIN, "Raise the overlay or use a lighter wash.")
    check("Accent text over a black banner", accent_text, hero, TEXT_MIN, "Raise the overlay, use a lighter wash or a darker accent text.")
    if tint:
        check("Hover tint and body text", tint, INK, INK_MIN, "Use a lighter tint.")
        check("Hover tint and secondary text", tint, MUTED, MUTED_MIN, "Use a lighter tint.")
    return out


def clean_name(value: str) -> str:
    v = _CONTROL_RE.sub("", str(value)).strip()
    if not v:
        raise ValueError("Name is required")
    if len(v) > NAME_MAX:
        raise ValueError(f"Name is at most {NAME_MAX} characters")
    return v


def clean_range(value, bounds: tuple[int, int], label: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a whole number") from None
    if not bounds[0] <= n <= bounds[1]:
        raise ValueError(f"{label} must be from {bounds[0]} to {bounds[1]}")
    return n


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
    out["hero_wash"] = parse_hex_color(values.get("hero_wash") or "#FFFFFF")
    out["tint"] = parse_hex_color(values["tint"]) if values.get("tint") else None
    out["radius"] = clean_range(values.get("radius"), RADIUS_RANGE, "Corner radius")
    out["art_height"] = clean_range(values.get("art_height"), ART_HEIGHT_RANGE, "Art height")
    failures = contrast_failures(
        out["bg"], out["accent_text"], out["primary"], float(out["banner_overlay"]), out["hero_wash"], out["tint"])
    if failures:
        raise ValueError(" ".join(failures))
    return out


def accent_ink(accent: str) -> str:
    """Text color on an accent fill (spec §63.1)."""
    return pick_ink(accent)
