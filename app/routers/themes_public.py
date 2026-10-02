"""Public, unauthenticated theme stylesheet (spec §71.6, §71.8).

`GET /theme/{id}.css` returns only `:root` declarations built from validated
values. The URL carries a version query, so it is cached for a year.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import Theme, ThemeAsset
from sqlalchemy import select
from services.themes import asset_hashes, is_renderable, theme_css

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/theme/{theme_id}.css")
async def theme_stylesheet(theme_id: int, db: AsyncSession = Depends(get_db)):
    theme = await db.get(Theme, theme_id)
    if theme is None:
        raise HTTPException(status_code=404, detail="Theme not found")
    if not is_renderable(theme):
        logger.error("Theme %s failed validation at render; serving an empty stylesheet", theme_id)
        return Response("", media_type="text/css", headers={"Cache-Control": "no-store"})
    return Response(
        theme_css(theme, await asset_hashes(db, theme)), media_type="text/css",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.get("/theme-assets/{sha256}.webp")
async def theme_asset(sha256: str, db: AsyncSession = Depends(get_db)):
    """A theme image by content hash. The hash is the URL, so it can be cached forever."""
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise HTTPException(status_code=404, detail="Not found")
    asset = (await db.execute(select(ThemeAsset).where(ThemeAsset.sha256 == sha256).limit(1))).scalar_one_or_none()
    if asset is None:
        raise HTTPException(status_code=404, detail="Not found")
    return Response(asset.content, media_type="image/webp", headers={
        "Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"})
