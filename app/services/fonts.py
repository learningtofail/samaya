"""Font catalogue for public-page themes (spec §71.2).

An allow-list of open-source Google Fonts families. Themes store catalogue
keys, never free text, so a theme can neither load an arbitrary URL nor name a
face the page cannot render. Adding a family is a reviewed code change.

`numerals` accepts monospace families only. A proportional family would need
its tabular figures verified first, and no family here has been; the time
column therefore cannot shift. Decorative faces fill `heading` only.
"""
import logging
import re
from dataclasses import dataclass
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

ROLES = ("heading", "body", "numerals")
_SANS = "system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif"
_SERIF = "Georgia, 'Times New Roman', serif"
_MONO = "ui-monospace, Menlo, Consolas, monospace"


@dataclass(frozen=True)
class Font:
    key: str
    name: str
    family: str            # Google Fonts family name
    weights: tuple[int, ...]
    stack: str             # CSS fallback stack, appended after the family
    scripts: tuple[str, ...]
    roles: tuple[str, ...]
    kind: str              # sans, serif, display, mono
    tabular: bool = False  # safe for the numerals role

    @property
    def css_stack(self) -> str:
        return f"'{self.family}', {self.stack}"


_HB = ("heading", "body")
_ALL = ("heading", "body", "numerals")

CATALOGUE: tuple[Font, ...] = (
    Font("noto-sans", "Noto Sans", "Noto Sans", (400, 600, 700), _SANS, ("latin", "cyrillic", "greek"), _HB, "sans"),
    Font("inter", "Inter", "Inter", (400, 600, 700), _SANS, ("latin", "cyrillic", "greek"), _HB, "sans"),
    Font("source-sans-3", "Source Sans 3", "Source Sans 3", (400, 600, 700), _SANS, ("latin", "cyrillic", "greek"), _HB, "sans"),
    Font("open-sans", "Open Sans", "Open Sans", (400, 600, 700), _SANS, ("latin", "cyrillic", "greek"), _HB, "sans"),
    Font("roboto", "Roboto", "Roboto", (400, 500, 700), _SANS, ("latin", "cyrillic", "greek"), _HB, "sans"),
    Font("merriweather", "Merriweather", "Merriweather", (400, 700), _SERIF, ("latin", "cyrillic"), _HB, "serif"),
    Font("lora", "Lora", "Lora", (400, 600, 700), _SERIF, ("latin", "cyrillic"), _HB, "serif"),
    Font("noto-serif", "Noto Serif", "Noto Serif", (400, 600, 700), _SERIF, ("latin", "cyrillic", "greek"), _HB, "serif"),
    Font("playfair-display", "Playfair Display", "Playfair Display", (400, 600, 700), _SERIF, ("latin", "cyrillic"), ("heading",), "display"),
    Font("cormorant-garamond", "Cormorant Garamond", "Cormorant Garamond", (500, 600, 700), _SERIF, ("latin", "cyrillic"), ("heading",), "display"),
    Font("cinzel", "Cinzel", "Cinzel", (500, 600, 700), _SERIF, ("latin",), ("heading",), "display"),
    Font("ibm-plex-mono", "IBM Plex Mono", "IBM Plex Mono", (400, 500, 600), _MONO, ("latin", "cyrillic"), _ALL, "mono", True),
    Font("jetbrains-mono", "JetBrains Mono", "JetBrains Mono", (400, 500, 600), _MONO, ("latin", "cyrillic", "greek"), _ALL, "mono", True),
    Font("roboto-mono", "Roboto Mono", "Roboto Mono", (400, 500, 600), _MONO, ("latin", "cyrillic", "greek"), _ALL, "mono", True),
    Font("space-mono", "Space Mono", "Space Mono", (400, 700), _MONO, ("latin",), _ALL, "mono", True),
)

BY_KEY: dict[str, Font] = {f.key: f for f in CATALOGUE}

# What the shipped page uses when a slot is empty (events.css).
DEFAULT_BODY_KEY = "noto-sans"
DEFAULT_NUMERALS_KEY = "ibm-plex-mono"
_KEY_RE = re.compile(r"^[a-z0-9-]{1,40}$")


def validate_font_key(key: str | None, role: str) -> str | None:
    """Return the key (or None for "page default"); raise ValueError when the
    key is unknown or the family may not fill `role`."""
    if key is None or key == "":
        return None
    font = BY_KEY.get(key) if _KEY_RE.match(key) else None
    if font is None:
        raise ValueError(f"Unknown font '{key}'")
    if role not in font.roles:
        raise ValueError(f"{font.name} cannot be used for {role}")
    return key


def resolve_font(key: str | None, role: str) -> Font | None:
    """Render-time lookup: a stale or invalid key logs and yields None, which
    means "use the page default". Never raises (spec §71.10)."""
    if not key:
        return None
    font = BY_KEY.get(key)
    if font is None or role not in font.roles:
        logger.warning("Theme font key %r is not valid for %s; using the default", key, role)
        return None
    return font


def google_fonts_url(fonts: list[Font]) -> str | None:
    """One CSS2 request for the distinct families, display=swap."""
    seen: dict[str, Font] = {}
    for f in fonts:
        seen.setdefault(f.key, f)
    if not seen:
        return None
    parts = [
        f"family={quote_plus(f.family)}:wght@{';'.join(str(w) for w in sorted(f.weights))}"
        for f in sorted(seen.values(), key=lambda f: f.key)
    ]
    return "https://fonts.googleapis.com/css2?" + "&".join(parts) + "&display=swap"


def catalogue_json() -> list[dict]:
    return [
        {"key": f.key, "name": f.name, "stack": f.css_stack, "kind": f.kind, "weights": list(f.weights),
         "scripts": list(f.scripts), "roles": list(f.roles)}
        for f in CATALOGUE
    ]
