"""Interface languages for the public pages (spec §72).

`app/i18n/<locale>.json` holds one catalogue per shipped locale: a `_meta`
object (`name`, `dir`, `script`, `reviewed`) plus flat keys grouped by prefix
(`public.*`). A value is a string or an object of CLDR plural categories.
`en.json` is canonical. `en-XA` is a pseudo-locale generated from it, for
development and tests only; it is enabled by `SAMAYA_PSEUDO_LOCALE=1`.

Lookup for one key: the locale, its language, the default locale, then `en`.
Plural selection happens in the browser (`Intl.PluralRules`); the server only
merges the chain and hands the page its strings.
"""
import json
import os
import re
from functools import lru_cache
from pathlib import Path

I18N_DIR = Path(__file__).resolve().parent.parent / "i18n"
BASE_LOCALE = "en"
PSEUDO_LOCALE = "en-XA"
LANG_COOKIE = "samaya_lang"

_ACCENTS = str.maketrans(
    "aeiouyAEIOUYcnsCNS",
    "àéîõüýÀÉÎÕÜÝçñšÇÑŠ",
)
_PLACEHOLDER_RE = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}|<[^>]+>")


@lru_cache(maxsize=None)
def _load_file(locale: str) -> dict:
    path = I18N_DIR / f"{locale}.json"
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def shipped_locales() -> list[str]:
    """Locale tags with a catalogue file, `en` first."""
    tags = sorted(p.stem for p in I18N_DIR.glob("*.json"))
    return sorted(tags, key=lambda t: (t != BASE_LOCALE, t))


def pseudo_enabled() -> bool:
    return os.environ.get("SAMAYA_PSEUDO_LOCALE") == "1"


def enabled_locales(kingdom_enabled: list[str] | None = None) -> list[str]:
    """Locales a visitor may be served. Until the Kingdom setting exists
    (spec §72.14 step 2) every shipped locale is enabled."""
    tags = [t for t in shipped_locales() if kingdom_enabled is None or t in kingdom_enabled]
    if pseudo_enabled():
        tags.append(PSEUDO_LOCALE)
    return tags


def pseudo_localize(text: str) -> str:
    """Accent the letters, stretch by about 40 percent, bracket the result.
    Placeholders and tags are left alone so substitution still works."""
    out, last = [], 0
    for m in _PLACEHOLDER_RE.finditer(text):
        out.append(text[last:m.start()].translate(_ACCENTS))
        out.append(m.group(0))
        last = m.end()
    out.append(text[last:].translate(_ACCENTS))
    body = "".join(out)
    # Padding is made of short words so a long pseudo string can still wrap, as real text does.
    words = max(1, round(len(text) * 0.4 / 3))
    return f"[{body}{' ~~' * words}]"


def _pseudo_value(value):
    if isinstance(value, dict):
        return {k: pseudo_localize(v) for k, v in value.items()}
    return pseudo_localize(value)


def catalogue(locale: str) -> dict:
    """The whole catalogue for one locale, `_meta` included."""
    if locale == PSEUDO_LOCALE:
        base = _load_file(BASE_LOCALE)
        data = {k: _pseudo_value(v) for k, v in base.items() if k != "_meta"}
        data["_meta"] = {"name": "Pseudo (accented)", "dir": "ltr", "script": "Latn", "reviewed": False}
        return data
    return _load_file(locale)


def fallback_chain(locale: str, default: str = BASE_LOCALE) -> list[str]:
    chain: list[str] = []
    for tag in (locale, locale.split("-")[0], default, BASE_LOCALE):
        if tag not in chain and (tag == PSEUDO_LOCALE or tag in shipped_locales()):
            chain.append(tag)
    return chain


def lookup(locale: str, key: str, default: str = BASE_LOCALE):
    """The string (or plural object) for `key`, or None when no locale has it."""
    for tag in fallback_chain(locale, default):
        value = catalogue(tag).get(key)
        if value is not None:
            return value
    return None


def strings_for(locale: str, prefixes: tuple[str, ...], default: str = BASE_LOCALE) -> dict:
    """Every key under `prefixes`, each resolved through the fallback chain."""
    keys = {k for k in catalogue(BASE_LOCALE) if k != "_meta" and k.startswith(prefixes)}
    return {k: lookup(locale, k, default) for k in sorted(keys)}


def text_direction(locale: str) -> str:
    return catalogue(locale)["_meta"].get("dir", "ltr")


def t(locale: str, key: str, **params) -> str:
    """One string, with `{name}` placeholders filled. A plural object uses
    the `other` form here; counted text is the browser's job (spec §72.4)."""
    value = lookup(locale, key)
    if value is None:
        return key
    if isinstance(value, dict):
        value = value.get("other", "")
    return value.format_map(_SafeParams(params)) if params else value


class _SafeParams(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _split_tag(tag: str) -> tuple[str, str, str]:
    """(language, script, region), lowercase language, title-case script, upper region."""
    parts = re.split(r"[-_]", tag.strip())
    lang = parts[0].lower()
    script = region = ""
    for p in parts[1:]:
        if len(p) == 4 and p.isalpha():
            script = p.title()
        elif len(p) in (2, 3) and (p.isalpha() or p.isdigit()):
            region = p.upper()
    return lang, script, region


_TRADITIONAL_REGIONS = {"TW", "HK", "MO"}


def match_locale(tag: str, enabled: list[str]) -> str | None:
    """Match a requested tag to an enabled locale: exact, then language only.
    Chinese is script-aware (spec §72.2): Traditional never falls to Simplified."""
    if not tag:
        return None
    by_lower = {e.lower(): e for e in enabled}
    if tag.lower() in by_lower:
        return by_lower[tag.lower()]
    lang, script, region = _split_tag(tag)
    if lang == "zh":
        traditional = script == "Hant" or (not script and region in _TRADITIONAL_REGIONS)
        return None if traditional else by_lower.get("zh-hans")
    return by_lower.get(lang)


def parse_accept_language(header: str) -> list[str]:
    """Tags from an Accept-Language header, best first, q=0 dropped."""
    ranked: list[tuple[float, int, str]] = []
    for i, part in enumerate(header.split(",")):
        piece = part.strip()
        if not piece:
            continue
        tag, _, params = piece.partition(";")
        q = 1.0
        m = re.search(r"q\s*=\s*([0-9.]+)", params)
        if m:
            try:
                q = float(m.group(1))
            except ValueError:
                q = 0.0
        if q > 0 and tag.strip() != "*":
            ranked.append((-q, i, tag.strip()))
    return [tag for _, _, tag in sorted(ranked)]


def choose_locale(
    enabled: list[str],
    default: str = BASE_LOCALE,
    lang_param: str | None = None,
    cookie: str | None = None,
    accept_language: str | None = None,
) -> str:
    """Spec §72.3, in order: `?lang=`, the cookie, Accept-Language, the default."""
    for candidate in (lang_param, cookie):
        found = match_locale(candidate or "", enabled)
        if found:
            return found
    for tag in parse_accept_language(accept_language or ""):
        found = match_locale(tag, enabled)
        if found:
            return found
    return match_locale(default, enabled) or BASE_LOCALE


def json_for_script_tag(data: dict) -> str:
    """JSON that is safe inside a <script type="application/json"> element."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return (text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
                .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def kingdom_locale_settings(kingdom) -> tuple[str, list[str]]:
    """(default locale, enabled locales) for a Kingdom row, or None for a
    deployment without one. Only shipped locales count, the default is always
    enabled, and a missing setting means English only (spec §72.2)."""
    shipped = shipped_locales()
    wanted = list(getattr(kingdom, "enabled_locales", None) or [BASE_LOCALE])
    enabled = [t for t in wanted if t in shipped]
    default = getattr(kingdom, "default_locale", None) or BASE_LOCALE
    if default not in shipped:
        default = BASE_LOCALE
    if default not in enabled:
        enabled.insert(0, default)
    return default, enabled_locales(enabled)


def locale_choices(enabled: list[str]) -> list[dict]:
    """[{tag, name}] for the language select, each name in its own language."""
    return [{"tag": tag, "name": catalogue(tag)["_meta"]["name"]} for tag in enabled]


def script_font(locale: str) -> str | None:
    """A Google Fonts family to add for a script the page font lacks, from the
    locale's `_meta.font` (Noto Sans covers Latin and Cyrillic, not Han or Arabic)."""
    return catalogue(locale)["_meta"].get("font")


def available_locales() -> list[dict]:
    """Every shipped locale with the facts the admin Languages panel shows."""
    out = []
    for tag in shipped_locales():
        meta = catalogue(tag)["_meta"]
        out.append({"tag": tag, "name": meta["name"], "dir": meta.get("dir", "ltr"),
                    "script": meta.get("script", ""), "reviewed": bool(meta.get("reviewed"))})
    return out
