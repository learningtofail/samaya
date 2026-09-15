"""log_change: called at the mutation point in each admin route that
touches something worth an audit trail — not a generic ORM-level hook
trying to auto-diff every model, which gets unreliable fast with async
SQLAlchemy (see routers/admin/deps.py's docstring for the same reasoning
applied to shared lookups).

before/after are plain dicts you build at the call site with whatever
fields are worth recording — not a dump of every column, since e.g. a
Tenant's bot_token has no business sitting in a log table donors might
eventually be able to read.
"""
import json

from models.db import AuditLog


async def log_change(
    db, *, user_id: int | None, tenant_id: int | None,
    table_name: str, row_id: int | None, action: str,
    before: dict | None = None, after: dict | None = None,
):
    entry = AuditLog(
        user_id    = user_id,
        tenant_id  = tenant_id,
        table_name = table_name,
        row_id     = row_id,
        action     = action,
        before     = json.dumps(before) if before is not None else None,
        after      = json.dumps(after) if after is not None else None,
    )
    db.add(entry)
    # Caller's own commit (for the mutation itself) covers this row too —
    # log_change doesn't commit on its own, so it never accidentally
    # commits a mutation the caller hasn't finished validating yet.
