"""Public, unauthenticated theme stylesheet (spec §71.6, §71.8).

`GET /theme/{id}.css` returns only `:root` declarations built from validated
values. The URL carries a version query, so it is cached for a year.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import Theme
from services.themes import is_renderable, theme_css

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
        theme_css(theme), media_type="text/css",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
