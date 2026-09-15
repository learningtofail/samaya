"""Not every driver round-trips tzinfo on a DateTime(timezone=True)
column the same way — asyncpg (production/Postgres) does, aiosqlite
(tests) doesn't. Every value in these columns is UTC regardless of what
the driver hands back, so callers normalise rather than assume.

Extracted here after the identical fix turned up independently in
routers/admin/occurrences.py, routers/auth.py, and scheduler/jobs.py —
three separate people (well, three separate times) hitting the same
SQLite quirk and writing the same three lines to fix it.
"""
from datetime import timezone


def ensure_utc(dt):
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
