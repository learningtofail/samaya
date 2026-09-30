"""Unit tests for services/db_errors.py — the shared IntegrityError ->
friendly-422 translator (audit remediation, Phase 2). The HTTP-level tests
in test_discord_servers.py::TestDuplicateConstraintMessages already prove
the UNIQUE path end-to-end through both drivers; these cover the CHECK
path directly (which, unlike UNIQUE, is genuinely unreachable through the
API today — services/validators.py's Pydantic field validators already
reject a non-positive duration/interval before a request ever reaches the
DB) and the constraint_name() extraction logic in isolation.
"""
import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from services.db_errors import constraint_name, raise_friendly_integrity_error


def _integrity_error(orig_text: str, constraint_name_attr: str | None = None):
    """Builds a SQLAlchemy IntegrityError shaped like what asyncpg or
    aiosqlite actually raise, without needing a real database — orig is a
    plain object carrying just what constraint_name() reads off it."""
    class _FakeOrig(Exception):
        pass

    orig = _FakeOrig(orig_text)
    if constraint_name_attr is not None:
        orig.constraint_name = constraint_name_attr
    return IntegrityError("statement", {}, orig)


class TestConstraintName:

    def test_asyncpg_structured_attribute_wins(self):
        # Real asyncpg IntegrityConstraintViolationError subclasses expose
        # this directly — should never fall through to text parsing when
        # it's present.
        exc = _integrity_error("irrelevant text", constraint_name_attr="ck_duration_positive")
        assert constraint_name(exc) == "ck_duration_positive"

    def test_postgres_quoted_constraint_text(self):
        exc = _integrity_error(
            'duplicate key value violates unique constraint "tenants_slug_key"\n'
            "DETAIL:  Key (slug)=(mod) already exists."
        )
        assert constraint_name(exc) == "tenants_slug_key"

    def test_postgres_check_violation_text(self):
        exc = _integrity_error('new row violates check constraint "ck_interval_positive"')
        assert constraint_name(exc) == "ck_interval_positive"

    def test_sqlite_named_check_constraint(self):
        exc = _integrity_error("CHECK constraint failed: ck_duration_positive")
        assert constraint_name(exc) == "ck_duration_positive"

    def test_sqlite_unique_constraint_normalized_to_postgres_shape(self):
        exc = _integrity_error("UNIQUE constraint failed: tenants.slug")
        assert constraint_name(exc) == "tenants_slug_key"

    def test_unrecognized_text_returns_none(self):
        exc = _integrity_error("some other database error entirely")
        assert constraint_name(exc) is None


class TestRaiseFriendlyIntegrityError:

    def test_known_constraint_raises_mapped_message(self):
        exc = _integrity_error("CHECK constraint failed: ck_duration_positive")
        with pytest.raises(HTTPException) as excinfo:
            raise_friendly_integrity_error(exc, {"ck_duration_positive": "Duration must be greater than 0"})
        assert excinfo.value.status_code == 422
        assert excinfo.value.detail == "Duration must be greater than 0"

    def test_unknown_constraint_raises_fallback_not_raw_text(self):
        exc = _integrity_error("some driver-specific internal detail nobody should see")
        with pytest.raises(HTTPException) as excinfo:
            raise_friendly_integrity_error(exc, {"ck_duration_positive": "Duration must be greater than 0"}, fallback="Could not save this record")
        assert excinfo.value.status_code == 422
        assert excinfo.value.detail == "Could not save this record"
        assert "internal detail" not in excinfo.value.detail
