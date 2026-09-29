"""Read-only view over AuditLog (spec §31) — services/audit.py's
log_change() already writes every row from the call sites across
announcements.py, announcement_templates.py, events.py, invites.py,
tenants.py, and users.py; this router only adds a way to see them.

Owner-only (require_tenant_owner), same as /api/members — a coordinator
manages day-to-day content but an audit trail of who-changed-what is an
owner-level concern, same reasoning as member management itself. A
superadmin-only global action (e.g. removing a kingdom coordinator grant,
tenant_id=None) is invisible here by design: this endpoint is
tenant-scoped, and those rows belong to no tenant to scope by.
"""
import json

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import get_db
from models.db import AuditLog, Tenant, User

from .deps import require_tenant_owner

router = APIRouter()


def _audit_dict(entry: AuditLog, user: User | None) -> dict:
    return {
        "id": entry.id,
        "timestamp": entry.timestamp.isoformat() if entry.timestamp else None,
        "user": user.discord_username if user else None,
        "table_name": entry.table_name,
        "row_id": entry.row_id,
        "action": entry.action,
        "before": json.loads(entry.before) if entry.before else None,
        "after": json.loads(entry.after) if entry.after else None,
    }


@router.get("/api/audit-log")
async def get_audit_log(
    limit: int = 100, offset: int = 0,
    tenant: Tenant = Depends(require_tenant_owner), db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(AuditLog, User)
        .outerjoin(User, User.id == AuditLog.user_id)
        .where(AuditLog.tenant_id == tenant.id)
        .order_by(AuditLog.timestamp.desc())
        .limit(limit).offset(offset)
    )
    return [_audit_dict(entry, user) for entry, user in result.all()]
