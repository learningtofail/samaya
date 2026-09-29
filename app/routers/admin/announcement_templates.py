"""Announcement templates (spec §27) — reusable named starting points for
creating an Announcement. Same tenant scoping and permission level as
Announcements themselves (any member of the current tenant, not
owner-only) — a template is just a prefill convenience for the same
create-announcement action every coordinator already has.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import AnnouncementTemplate, Tenant, User
from services.audit import log_change

from .deps import get_current_tenant, require_not_viewer, get_current_user
from .schemas import AnnouncementTemplateIn, AnnouncementTemplatePatch

router = APIRouter()


def _template_dict(t: AnnouncementTemplate) -> dict:
    return {
        "id":                    t.id,
        "owning_tenant_id":      t.owning_tenant_id,
        "name":                  t.name,
        "title_template":        t.title_template,
        "body_template":         t.body_template,
        "leadership_only":       t.leadership_only,
        "event_offset_minutes":  t.event_offset_minutes,
    }


@router.get("/api/announcement-templates")
async def list_announcement_templates(
    tenant: Tenant = Depends(get_current_tenant), db: AsyncSession = Depends(get_db)
):
    result = await db.execute(
        select(AnnouncementTemplate)
        .where(AnnouncementTemplate.owning_tenant_id == tenant.id)
        .order_by(AnnouncementTemplate.name)
    )
    return [_template_dict(t) for t in result.scalars().all()]


@router.post("/api/announcement-templates", status_code=201)
async def create_announcement_template(
    payload: AnnouncementTemplateIn,
    tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    template = AnnouncementTemplate(
        owning_tenant_id=tenant.id, name=payload.name,
        title_template=payload.title_template, body_template=payload.body_template,
        leadership_only=payload.leadership_only, event_offset_minutes=payload.event_offset_minutes,
    )
    db.add(template)
    try:
        await db.flush()
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=422, detail=f"Could not create template: {e}")

    await log_change(
        db, user_id=user.id, tenant_id=tenant.id,
        table_name="announcement_templates", row_id=template.id, action="create",
        after={"name": template.name},
    )
    await db.commit()
    await db.refresh(template)
    return _template_dict(template)


async def _get_owned_template(db: AsyncSession, tenant: Tenant, template_id: int) -> AnnouncementTemplate:
    result = await db.execute(
        select(AnnouncementTemplate).where(
            AnnouncementTemplate.id == template_id, AnnouncementTemplate.owning_tenant_id == tenant.id,
        )
    )
    template = result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Announcement template not found")
    return template


@router.patch("/api/announcement-templates/{template_id}")
async def update_announcement_template(
    template_id: int, payload: AnnouncementTemplatePatch,
    tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    template = await _get_owned_template(db, tenant, template_id)

    if payload.name is not None:                 template.name = payload.name
    if payload.title_template is not None:        template.title_template = payload.title_template
    if payload.body_template is not None:         template.body_template = payload.body_template
    if payload.leadership_only is not None:       template.leadership_only = payload.leadership_only
    if payload.event_offset_minutes is not None:  template.event_offset_minutes = payload.event_offset_minutes

    try:
        await log_change(
            db, user_id=user.id, tenant_id=tenant.id,
            table_name="announcement_templates", row_id=template.id, action="update",
            after={"name": template.name},
        )
        await db.commit()
        await db.refresh(template)
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=422, detail=f"Could not update template: {e}")
    return _template_dict(template)


@router.delete("/api/announcement-templates/{template_id}", status_code=200)
async def delete_announcement_template(
    template_id: int,
    tenant: Tenant = Depends(require_not_viewer),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    template = await _get_owned_template(db, tenant, template_id)
    await log_change(
        db, user_id=user.id, tenant_id=tenant.id,
        table_name="announcement_templates", row_id=template.id, action="delete",
        before={"name": template.name},
    )
    await db.delete(template)
    await db.commit()
    return {"status": "deleted"}
