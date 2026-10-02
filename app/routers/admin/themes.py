"""Public page themes and their schedule (spec §71.7, §71.8). Superadmin only.

Themes are validated data: colors are hex, fonts are catalogue keys, and the
contrast rules of services/theme_rules.py run on every save. Banner images and
text overrides arrive in later build steps.
"""
import base64
import hashlib
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import Kingdom, ScheduledTheme, Theme, ThemeAsset, User
from services.audit import log_change
from services.db_errors import raise_friendly_integrity_error
from services.fonts import catalogue_json
from services.images import THEME_BANNER, THEME_HEADER, THEME_HEADER_MOBILE, process_data_uri_async
from services.themes import asset_url
from services import theme_rules as rules
from services.theme_rules import OVERLAY_DEFAULT, validate_theme_values
from services.theme_templates import TEMPLATES
from services.time_utils import ensure_utc

from .deps import require_superadmin

router = APIRouter(prefix="/api")

_THEME_TAKEN = {
    "uq_theme_name": "This Kingdom already has a theme with that name",
    "themes_kingdom_id_key": "This Kingdom already has a theme with that name",
}
_THEME_FIELDS = (
    "name", "bg", "accent", "accent_text", "primary",
    "font_heading", "font_body", "font_numerals", "banner_overlay", "archived",
    "banner_asset_id", "header_asset_id", "header_mobile_asset_id", "hero_wash", "tint", "radius", "art_height",
)
# Fields where an explicit null (or "") in a PATCH clears the value; omitted keeps it.
_CLEARABLE = ("font_heading", "font_body", "font_numerals", "banner_asset_id", "header_asset_id",
              "header_mobile_asset_id", "tint", "radius", "art_height")
_SIMPLE = ("name", "bg", "accent", "accent_text", "primary", "banner_overlay", "archived", "hero_wash")
_ASSET_SLOTS = {
    "banner_asset_id": ("banner", THEME_BANNER),
    "header_asset_id": ("header", THEME_HEADER),
    "header_mobile_asset_id": ("header_mobile", THEME_HEADER_MOBILE),
}
_CREDIT_MAX = 300


class ThemeIn(BaseModel):
    kingdom_id:     int
    name:           str
    bg:             str
    accent:         str
    accent_text:    str
    primary:        str
    font_heading:   str | None = None
    font_body:      str | None = None
    font_numerals:  str | None = None
    banner_overlay: float = float(OVERLAY_DEFAULT)
    banner_asset_id:        int | None = None
    header_asset_id:        int | None = None
    header_mobile_asset_id: int | None = None
    hero_wash:      str = "#FFFFFF"
    tint:           str | None = None
    radius:         int | None = None
    art_height:     int | None = None


class AssetIn(BaseModel):
    kingdom_id: int
    slot:       str = Field(pattern="^(banner|header|header_mobile)$")
    image_data: str
    credit:     str = Field(min_length=3, max_length=_CREDIT_MAX)


class ThemePatch(BaseModel):
    name:           str | None = None
    bg:             str | None = None
    accent:         str | None = None
    accent_text:    str | None = None
    primary:        str | None = None
    # Explicit null or "" clears a font slot to the page default; omit to keep.
    font_heading:   str | None = None
    font_body:      str | None = None
    font_numerals:  str | None = None
    banner_overlay: float | None = None
    archived:       bool | None = None
    banner_asset_id:        int | None = None
    header_asset_id:        int | None = None
    header_mobile_asset_id: int | None = None
    hero_wash:      str | None = None
    tint:           str | None = None
    radius:         int | None = None
    art_height:     int | None = None


class ScheduleIn(BaseModel):
    kingdom_id:     int
    theme_id:       int
    start_utc:      datetime
    end_utc:        datetime
    priority_level: int = Field(default=50, ge=0, le=1000)

    @field_validator("start_utc", "end_utc")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        return ensure_utc(v)


class SchedulePatch(BaseModel):
    theme_id:       int | None = None
    start_utc:      datetime | None = None
    end_utc:        datetime | None = None
    priority_level: int | None = Field(default=None, ge=0, le=1000)


def _theme_dict(t: Theme, used_by: dict | None = None) -> dict:
    d = {
        "id": t.id, "kingdom_id": t.kingdom_id, "name": t.name,
        "bg": t.bg, "accent": t.accent, "accent_text": t.accent_text, "primary": t.primary,
        "font_heading": t.font_heading, "font_body": t.font_body, "font_numerals": t.font_numerals,
        "banner_overlay": float(t.banner_overlay), "archived": bool(t.archived),
        "banner_asset_id": t.banner_asset_id, "header_asset_id": t.header_asset_id,
        "header_mobile_asset_id": t.header_mobile_asset_id,
        "hero_wash": t.hero_wash, "tint": t.tint, "radius": t.radius, "art_height": t.art_height,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }
    if used_by is not None:
        d["is_base"] = used_by["base"]
        d["scheduled_count"] = used_by["scheduled"]
    return d


def _audit_theme(t: Theme) -> dict:
    return {f: (float(getattr(t, f)) if f == "banner_overlay" else getattr(t, f)) for f in _THEME_FIELDS}


def _schedule_dict(r: ScheduledTheme, overlaps: list[int]) -> dict:
    return {
        "id": r.id, "kingdom_id": r.kingdom_id, "theme_id": r.theme_id,
        "start_utc": ensure_utc(r.start_utc).isoformat(), "end_utc": ensure_utc(r.end_utc).isoformat(),
        "priority_level": r.priority_level, "overlaps_same_priority": overlaps,
    }


def _overlapping_same_priority(row: ScheduledTheme, rows: list[ScheduledTheme]) -> list[int]:
    s, e = ensure_utc(row.start_utc), ensure_utc(row.end_utc)
    return sorted(
        o.id for o in rows
        if o.id != row.id and o.priority_level == row.priority_level
        and ensure_utc(o.start_utc) < e and s < ensure_utc(o.end_utc)
    )


async def _kingdom_or_404(db: AsyncSession, kingdom_id: int) -> Kingdom:
    kingdom = await db.get(Kingdom, kingdom_id)
    if kingdom is None:
        raise HTTPException(status_code=404, detail="Kingdom not found")
    return kingdom


async def _theme_or_404(db: AsyncSession, theme_id: int) -> Theme:
    theme = await db.get(Theme, theme_id)
    if theme is None:
        raise HTTPException(status_code=404, detail="Theme not found")
    return theme


async def _usage(db: AsyncSession, theme: Theme) -> dict:
    kingdom = await db.get(Kingdom, theme.kingdom_id)
    scheduled = (await db.execute(select(ScheduledTheme).where(ScheduledTheme.theme_id == theme.id))).scalars().all()
    return {"base": kingdom is not None and kingdom.default_theme_id == theme.id, "scheduled": len(scheduled)}


def _in_use_message(usage: dict, action: str) -> str | None:
    places = []
    if usage["base"]:
        places.append("the Kingdom's base theme")
    if usage["scheduled"]:
        places.append(f"{usage['scheduled']} scheduled window(s)")
    return f"Cannot {action} a theme used as " + " and in ".join(places) + ". Change the schedule first." if places else None


def _validated(values: dict) -> dict:
    try:
        return validate_theme_values(values)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/fonts")
async def list_fonts(_: User = Depends(require_superadmin)):
    return catalogue_json()


@router.get("/theme-rules")
async def theme_rules(_: User = Depends(require_superadmin)):
    """The numbers the editor needs to show live contrast; the server still enforces."""
    return {
        "ink": rules.INK, "muted": rules.MUTED, "ink_min": rules.INK_MIN, "muted_min": rules.MUTED_MIN,
        "text_min": rules.TEXT_MIN, "overlay_min": float(rules.OVERLAY_MIN), "overlay_max": float(rules.OVERLAY_MAX),
        "overlay_default": float(rules.OVERLAY_DEFAULT), "surface_2": rules.SURFACE_2,
        "default_tint": rules.DEFAULT_TINT, "radius_range": list(rules.RADIUS_RANGE),
        "art_height_range": list(rules.ART_HEIGHT_RANGE),
    }


@router.get("/theme-templates")
async def list_templates(_: User = Depends(require_superadmin)):
    return list(TEMPLATES)


@router.get("/themes")
async def list_themes(kingdom_id: int, _: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)):
    kingdom = await _kingdom_or_404(db, kingdom_id)
    themes = (await db.execute(select(Theme).where(Theme.kingdom_id == kingdom_id).order_by(Theme.name))).scalars().all()
    scheduled = (await db.execute(select(ScheduledTheme).where(ScheduledTheme.kingdom_id == kingdom_id))).scalars().all()
    counts: dict[int, int] = {}
    for r in scheduled:
        counts[r.theme_id] = counts.get(r.theme_id, 0) + 1
    return [
        _theme_dict(t, {"base": kingdom.default_theme_id == t.id, "scheduled": counts.get(t.id, 0)})
        for t in themes
    ]


async def _check_assets(db: AsyncSession, kingdom_id: int, values: dict) -> None:
    """Each referenced image must exist, belong to the Kingdom and have its slot's exact size."""
    for field, (slot, profile) in _ASSET_SLOTS.items():
        asset_id = values.get(field)
        if asset_id is None:
            continue
        asset = await db.get(ThemeAsset, asset_id)
        if asset is None or asset.kingdom_id != kingdom_id:
            raise HTTPException(status_code=422, detail=f"That {slot.replace('_', ' ')} image does not belong to this Kingdom")
        if (asset.width, asset.height) != profile.crop:
            raise HTTPException(status_code=422, detail=f"That image is {asset.width} by {asset.height}; the {slot.replace('_', ' ')} slot needs {profile.crop[0]} by {profile.crop[1]}")


async def _prune_assets(db: AsyncSession, asset_ids: set[int | None]) -> None:
    """Delete images no theme references any more, in the caller's transaction."""
    for asset_id in {i for i in asset_ids if i}:
        used = (await db.execute(select(Theme.id).where(or_(
            Theme.banner_asset_id == asset_id, Theme.header_asset_id == asset_id,
            Theme.header_mobile_asset_id == asset_id)).limit(1))).first()
        if used is None:
            asset = await db.get(ThemeAsset, asset_id)
            if asset is not None:
                await db.delete(asset)


def _asset_ids(t: Theme) -> set[int | None]:
    return {t.banner_asset_id, t.header_asset_id, t.header_mobile_asset_id}


@router.post("/theme-assets", status_code=201)
async def upload_asset(payload: AssetIn, user: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)):
    """Normalise an uploaded image for its slot and store it by content hash. A
    re-upload of the same result returns the existing row (and updates its credit)."""
    await _kingdom_or_404(db, payload.kingdom_id)
    slot_field = {"banner": "banner_asset_id", "header": "header_asset_id", "header_mobile": "header_mobile_asset_id"}[payload.slot]
    label = {"banner": "Banner image", "header": "Header image", "header_mobile": "Mobile header image"}[payload.slot]
    processed = await process_data_uri_async(payload.image_data, _ASSET_SLOTS[slot_field][1], label)
    content = base64.b64decode(processed.split(",", 1)[1])
    digest = hashlib.sha256(content).hexdigest()
    asset = (await db.execute(select(ThemeAsset).where(
        ThemeAsset.kingdom_id == payload.kingdom_id, ThemeAsset.sha256 == digest))).scalar_one_or_none()
    created = asset is None
    width, height = _ASSET_SLOTS[slot_field][1].crop
    if created:
        asset = ThemeAsset(kingdom_id=payload.kingdom_id, sha256=digest, content=content, width=width, height=height,
                           credit=payload.credit.strip())
        db.add(asset)
        await db.flush()
    else:
        asset.credit = payload.credit.strip()
    # Metadata only: audit rows never hold image bytes (spec §71.7).
    await log_change(db, user_id=user.id, tenant_id=None, table_name="theme_assets", row_id=asset.id,
                     action="create" if created else "update",
                     after={"sha256": digest, "width": width, "height": height, "bytes": len(content), "credit": asset.credit})
    await db.commit()
    return _asset_dict(asset)


def _asset_dict(a: ThemeAsset) -> dict:
    return {"id": a.id, "sha256": a.sha256, "url": asset_url(a.sha256), "width": a.width, "height": a.height,
            "bytes": len(a.content), "credit": a.credit}


@router.get("/theme-assets")
async def list_assets(kingdom_id: int, _: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)):
    await _kingdom_or_404(db, kingdom_id)
    rows = (await db.execute(select(ThemeAsset).where(ThemeAsset.kingdom_id == kingdom_id).order_by(ThemeAsset.id))).scalars().all()
    return [_asset_dict(a) for a in rows]


@router.delete("/theme-assets/{asset_id}", status_code=204)
async def delete_asset(asset_id: int, user: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)):
    asset = await db.get(ThemeAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Image not found")
    used = (await db.execute(select(Theme.name).where(or_(
        Theme.banner_asset_id == asset_id, Theme.header_asset_id == asset_id,
        Theme.header_mobile_asset_id == asset_id)))).scalars().all()
    if used:
        raise HTTPException(status_code=409, detail="This image is used by: " + ", ".join(used))
    await log_change(db, user_id=user.id, tenant_id=None, table_name="theme_assets", row_id=asset.id,
                     action="delete", before={"sha256": asset.sha256, "credit": asset.credit})
    await db.delete(asset)
    await db.commit()


@router.post("/themes", status_code=201)
async def create_theme(payload: ThemeIn, user: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)):
    await _kingdom_or_404(db, payload.kingdom_id)
    values = _validated(payload.model_dump(exclude={"kingdom_id"}))
    await _check_assets(db, payload.kingdom_id, values)
    theme = Theme(kingdom_id=payload.kingdom_id, **values)
    db.add(theme)
    try:
        await db.flush()
        await log_change(db, user_id=user.id, tenant_id=None, table_name="themes", row_id=theme.id,
                         action="create", after=_audit_theme(theme))
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise_friendly_integrity_error(exc, _THEME_TAKEN, fallback="Could not save the theme")
    await db.refresh(theme)
    return _theme_dict(theme, await _usage(db, theme))


@router.patch("/themes/{theme_id}")
async def update_theme(
    theme_id: int, payload: ThemePatch, user: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db),
):
    theme = await _theme_or_404(db, theme_id)
    before = _audit_theme(theme)
    old_assets = _asset_ids(theme)
    sent = payload.model_fields_set
    merged = {f: getattr(theme, f) for f in _THEME_FIELDS}
    for f in _SIMPLE:
        if f in sent and getattr(payload, f) is not None:
            merged[f] = getattr(payload, f)
    for f in _CLEARABLE:
        if f in sent:
            merged[f] = getattr(payload, f)
    usage = await _usage(db, theme)
    if merged["archived"] and not theme.archived:
        message = _in_use_message(usage, "archive")
        if message:
            raise HTTPException(status_code=409, detail=message)
    values = _validated({**merged, "banner_overlay": float(merged["banner_overlay"])})
    await _check_assets(db, theme.kingdom_id, values)
    for f, v in values.items():
        setattr(theme, f, v)
    try:
        await db.flush()
        await _prune_assets(db, old_assets - _asset_ids(theme))
        await log_change(db, user_id=user.id, tenant_id=None, table_name="themes", row_id=theme.id,
                         action="update", before=before, after=_audit_theme(theme))
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise_friendly_integrity_error(exc, _THEME_TAKEN, fallback="Could not save the theme")
    await db.refresh(theme)
    return _theme_dict(theme, usage)


@router.delete("/themes/{theme_id}", status_code=204)
async def delete_theme(theme_id: int, user: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)):
    theme = await _theme_or_404(db, theme_id)
    message = _in_use_message(await _usage(db, theme), "delete")
    if message:
        raise HTTPException(status_code=409, detail=message)
    assets = _asset_ids(theme)
    await log_change(db, user_id=user.id, tenant_id=None, table_name="themes", row_id=theme.id,
                     action="delete", before=_audit_theme(theme))
    await db.delete(theme)
    await db.flush()
    await _prune_assets(db, assets)
    await db.commit()


async def _rows(db: AsyncSession, kingdom_id: int) -> list[ScheduledTheme]:
    return list((await db.execute(
        select(ScheduledTheme).where(ScheduledTheme.kingdom_id == kingdom_id)
        .order_by(ScheduledTheme.start_utc, ScheduledTheme.id)
    )).scalars().all())


@router.get("/scheduled-themes")
async def list_schedule(kingdom_id: int, _: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)):
    await _kingdom_or_404(db, kingdom_id)
    rows = await _rows(db, kingdom_id)
    return [_schedule_dict(r, _overlapping_same_priority(r, rows)) for r in rows]


async def _check_window(db: AsyncSession, kingdom_id: int, theme_id: int, start: datetime, end: datetime) -> None:
    theme = await db.get(Theme, theme_id)
    if theme is None or theme.kingdom_id != kingdom_id:
        raise HTTPException(status_code=422, detail="That theme does not belong to this Kingdom")
    if theme.archived:
        raise HTTPException(status_code=422, detail="An archived theme cannot be scheduled")
    if not start < end:
        raise HTTPException(status_code=422, detail="The end must be after the start")


@router.post("/scheduled-themes", status_code=201)
async def create_schedule(payload: ScheduleIn, user: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)):
    await _kingdom_or_404(db, payload.kingdom_id)
    await _check_window(db, payload.kingdom_id, payload.theme_id, payload.start_utc, payload.end_utc)
    row = ScheduledTheme(**payload.model_dump())
    db.add(row)
    await db.flush()
    await log_change(db, user_id=user.id, tenant_id=None, table_name="scheduled_themes", row_id=row.id,
                     action="create", after=_schedule_dict(row, []))
    await db.commit()
    rows = await _rows(db, row.kingdom_id)
    return _schedule_dict(row, _overlapping_same_priority(row, rows))


@router.patch("/scheduled-themes/{row_id}")
async def update_schedule(
    row_id: int, payload: SchedulePatch, user: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db),
):
    row = await db.get(ScheduledTheme, row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Scheduled theme not found")
    before = _schedule_dict(row, [])
    for f in ("theme_id", "priority_level"):
        if getattr(payload, f) is not None:
            setattr(row, f, getattr(payload, f))
    if payload.start_utc is not None:
        row.start_utc = ensure_utc(payload.start_utc)
    if payload.end_utc is not None:
        row.end_utc = ensure_utc(payload.end_utc)
    await _check_window(db, row.kingdom_id, row.theme_id, ensure_utc(row.start_utc), ensure_utc(row.end_utc))
    await log_change(db, user_id=user.id, tenant_id=None, table_name="scheduled_themes", row_id=row.id,
                     action="update", before=before, after=_schedule_dict(row, []))
    await db.commit()
    rows = await _rows(db, row.kingdom_id)
    return _schedule_dict(row, _overlapping_same_priority(row, rows))


@router.delete("/scheduled-themes/{row_id}", status_code=204)
async def delete_schedule(row_id: int, user: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)):
    row = await db.get(ScheduledTheme, row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Scheduled theme not found")
    await log_change(db, user_id=user.id, tenant_id=None, table_name="scheduled_themes", row_id=row.id,
                     action="delete", before=_schedule_dict(row, []))
    await db.delete(row)
    await db.commit()
