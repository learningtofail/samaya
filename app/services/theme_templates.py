"""Starter palettes for the theme editor (spec §71.1): the five seasonal looks
that used to live in events.css, as hex values that pass the contrast rules.
The editor copies one into a new, unsaved theme. No database rows."""

TEMPLATES: tuple[dict, ...] = (
    {"key": "kingdom-gold", "name": "Kingdom gold", "bg": "#F0F4F9", "accent": "#EBBD57", "accent_text": "#8A5700", "primary": "#151F32"},
    {"key": "nine-nights", "name": "Nine nights", "bg": "#FDF1F8", "accent": "#F45FB0", "accent_text": "#970D63", "primary": "#151F32"},
    {"key": "hallowed-ancestors", "name": "Hallowed ancestors", "bg": "#F3F0F9", "accent": "#6B359B", "accent_text": "#571F84", "primary": "#151F32"},
    {"key": "illuminated-keep", "name": "Illuminated keep", "bg": "#F0F4F9", "accent": "#FC9F30", "accent_text": "#934400", "primary": "#151F32"},
    {"key": "crimson-festival", "name": "Crimson festival", "bg": "#FDF2F0", "accent": "#CC2827", "accent_text": "#A20711", "primary": "#151F32"},
    {"key": "frost-festival", "name": "Frost festival", "bg": "#EDF7FC", "accent": "#84C9E5", "accent_text": "#00547B", "primary": "#151F32"},
)
