"""drop the tables the unified event model replaces (spec §66.8, closure)

Revision ID: a1f0c0de0003
Revises: a1f0c0de0002
Create Date: 2026-10-01 18:00:00.000000

DESTRUCTIVE. Drops event_definitions, occurrences, post_log, event_targets,
event_tenant_notifications, announcements, announcement_targets,
announcement_templates and scheduler_state. Setup data (kingdoms, Discord
servers, alliances, users, access, audit log, tickets) is untouched.

There is no data-preserving downgrade. The rollback is restoring the dump
taken by ops/backup.sh (docs/rollback-runbook.md).

Guard: the Discord Scheduled Events the old system created must be deleted
before this runs, because post_log is the only record of their IDs. Run
`python cutover_delete_old_discord_events.py --apply` first; it clears each
ID from post_log as it deletes the event, and this revision refuses to run
while any ID is left. Set SAMAYA_SKIP_CUTOVER_CHECK=1 to bypass the check
(for example on a database that never posted to a real Discord server).
"""
import os
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a1f0c0de0003'
down_revision: Union[str, Sequence[str], None] = 'a1f0c0de0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Children first, so no drop is blocked by a foreign key from a table that
# is itself about to be dropped.
OLD_TABLES = (
    "announcement_targets",
    "announcement_templates",
    "announcements",
    "event_tenant_notifications",
    "event_targets",
    "post_log",
    "occurrences",
    "event_definitions",
    "scheduler_state",
)


def upgrade() -> None:
    bind = op.get_bind()
    if os.environ.get("SAMAYA_SKIP_CUTOVER_CHECK") != "1" and sa.inspect(bind).has_table("post_log"):
        left = bind.execute(sa.text("SELECT count(*) FROM post_log WHERE discord_event_id IS NOT NULL")).scalar_one()
        if left:
            raise RuntimeError(
                f"{left} post_log row(s) still reference Discord Scheduled Events. Run "
                "`python cutover_delete_old_discord_events.py --apply` first so those events are "
                "deleted from Discord, then re-run this migration. To skip this check, set "
                "SAMAYA_SKIP_CUTOVER_CHECK=1."
            )
    for table in OLD_TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


def downgrade() -> None:
    raise NotImplementedError(
        "This revision dropped tables and their data. Restore the pre-change dump from "
        "ops/backup.sh (see docs/rollback-runbook.md) instead of downgrading."
    )
