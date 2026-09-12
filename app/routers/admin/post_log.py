"""Read-only views over PostLog: the paginated list shown in the admin UI
and the full CSV export.
"""
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import EventDefinition, PostLog
from services.auth import require_admin_key

from .serializers import _log_dict

router = APIRouter(dependencies=[Depends(require_admin_key)])


@router.get("/api/post-log")
async def get_post_log(limit: int = 50, offset: int = 0, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(PostLog, EventDefinition)
        .outerjoin(EventDefinition, PostLog.event_id == EventDefinition.id)
        .order_by(PostLog.occurrence_date.desc(), PostLog.posted_at_utc.desc())
        .limit(limit).offset(offset)
    )
    return [_log_dict(l, ev) for l, ev in result.all()]


@router.get("/api/post-log/export.csv")
async def export_post_log(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PostLog).order_by(PostLog.occurrence_date.desc()))
    logs   = result.scalars().all()
    def generate():
        yield "Event Name,Date,Discord Event ID,Posted At (UTC),Posted By,Status\n"
        for l in logs:
            yield f'"{l.event_name}","{l.occurrence_date}","{l.discord_event_id or ""}","{l.posted_at_utc or ""}","{l.posted_by}","{l.status}"\n'
    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=PostLog_Export.csv"}
    )

