"""Translating a SQLAlchemy IntegrityError into a clean, user-facing 422 —
shared by every admin create/update endpoint that can hit a UNIQUE or CHECK
constraint (routers/admin/events.py, tenants.py). Before this, each place
either substring-matched the constraint name out of str(exception) (fragile
— it happens to work because Postgres/SQLite both mention the constraint
name in their error text, not because anything guarantees that) or just
interpolated the raw exception straight into the HTTP response body,
leaking a driver-specific traceback fragment to an API client for what's
usually an ordinary "that slug is already taken" situation.
"""
import re
from typing import NoReturn

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError


def constraint_name(exc: IntegrityError) -> str | None:
    """Best-effort extraction of the violated constraint's name, across the
    two drivers this app actually runs against: asyncpg in production,
    aiosqlite in tests (see tests/conftest.py). Neither SQLAlchemy nor the
    two drivers expose one single, consistent attribute for this, so this
    tries the structured path first and falls back to pattern-matching the
    driver's own error text — the same information the old per-router
    substring checks were already relying on, just centralized and a
    little more precise about it.
    """
    orig = getattr(exc, "orig", None)

    # asyncpg's own IntegrityConstraintViolationError subclasses (Unique/
    # Check/ForeignKey/NotNull-Violation) carry this directly.
    name = getattr(orig, "constraint_name", None)
    if name:
        return name

    text = str(orig or exc)
    # Postgres: `... violates check constraint "ck_duration_positive"` /
    # `... violates unique constraint "uq_tenant_slug"` — quoted, so this
    # doesn't need to know which of the two kinds it's looking at.
    match = re.search(r'constraint "([^"]+)"', text)
    if match:
        return match.group(1)

    # SQLite (tests): named CHECK constraints report their real name
    # (`CHECK constraint failed: ck_duration_positive`) the same way
    # Postgres does — SQLAlchemy emits the same `CONSTRAINT ck_... CHECK
    # (...)` DDL on both backends, so this one's a direct match.
    match = re.search(r"CHECK constraint failed: (\S+)", text)
    if match:
        return match.group(1)

    # SQLite has no equivalent for a UNIQUE constraint: it only ever
    # reports `UNIQUE constraint failed: tenants.slug` (table.column, not
    # a name it invented). Normalize that to the same "<table>_<column>_key"
    # shape Postgres's own default naming convention produces for an
    # unnamed unique column (see routers/admin/tenants.py's own comment on
    # why callers key their messages dicts off that shape) — so the same
    # messages dict works against both drivers instead of only ever
    # matching in production and silently falling back to the generic
    # message in every test that exercises this path.
    match = re.search(r"UNIQUE constraint failed: (\w+)\.(\w+)", text)
    if match:
        table, column = match.groups()
        return f"{table}_{column}_key"

    return None


def raise_friendly_integrity_error(
    exc: IntegrityError, messages: dict[str, str], fallback: str = "Could not save — a database constraint was violated"
) -> NoReturn:
    """Looks up the violated constraint in `messages` and raises the
    matching HTTPException(422, ...); raises a generic, non-leaking 422
    if the constraint isn't recognized. Always raises — never returns —
    so a caller doesn't need its own trailing `raise` after calling this.
    """
    name = constraint_name(exc)
    if name and name in messages:
        raise HTTPException(status_code=422, detail=messages[name])
    raise HTTPException(status_code=422, detail=fallback)
