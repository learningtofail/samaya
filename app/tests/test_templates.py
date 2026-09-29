"""Tests for services/templates.py's placeholder substitution (spec §27) —
pure functions, no I/O.
"""
from datetime import datetime, timezone

from services.templates import discord_timestamp, render_placeholders


def _dt(iso):
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


class TestDiscordTimestamp:

    def test_formats_unix_seconds_and_style(self):
        dt = _dt("2026-01-01T00:00:00")
        assert discord_timestamp(dt, "F") == "<t:1767225600:F>"

    def test_relative_style(self):
        dt = _dt("2026-01-01T00:00:00")
        assert discord_timestamp(dt, "R") == "<t:1767225600:R>"


class TestRenderPlaceholders:

    def test_no_placeholders_returns_text_unchanged(self):
        text = "Plain announcement, no substitution needed."
        assert render_placeholders(
            text, tenant_name="MOD", kingdom_name="Kingdom 138",
            scheduled_for=_dt("2026-01-01T23:45:00"),
        ) == text

    def test_alliance_and_kingdom_name(self):
        result = render_placeholders(
            "{alliance_name} is part of {kingdom_name}",
            tenant_name="MOD", kingdom_name="Kingdom 138",
            scheduled_for=_dt("2026-01-01T23:45:00"),
        )
        assert result == "MOD is part of Kingdom 138"

    def test_send_time_placeholders_use_scheduled_for(self):
        scheduled_for = _dt("2026-01-01T23:45:00")
        result = render_placeholders(
            "Sending at {send_time} ({send_time_relative})",
            tenant_name="MOD", kingdom_name="Kingdom 138", scheduled_for=scheduled_for,
        )
        ts = int(scheduled_for.timestamp())
        assert result == f"Sending at <t:{ts}:F> (<t:{ts}:R>)"

    def test_event_time_placeholders_apply_offset(self):
        """The daily-reset case: a warning sent at 23:45 UTC for a reset
        at 00:00 UTC the same night — event_offset_minutes=15."""
        scheduled_for = _dt("2026-01-01T23:45:00")
        result = render_placeholders(
            "Reset {event_time_relative}",
            tenant_name="MOD", kingdom_name="Kingdom 138",
            scheduled_for=scheduled_for, event_offset_minutes=15,
        )
        event_ts = int(scheduled_for.timestamp()) + 15 * 60
        assert result == f"Reset <t:{event_ts}:R>"

    def test_zero_offset_makes_event_time_equal_send_time(self):
        scheduled_for = _dt("2026-01-01T23:45:00")
        result = render_placeholders(
            "{send_time}|{event_time}",
            tenant_name="MOD", kingdom_name="Kingdom 138",
            scheduled_for=scheduled_for, event_offset_minutes=0,
        )
        ts = discord_timestamp(scheduled_for, "F")
        assert result == f"{ts}|{ts}"

    def test_negative_offset_for_an_event_already_underway(self):
        scheduled_for = _dt("2026-01-01T00:10:00")
        result = render_placeholders(
            "{event_time_relative}",
            tenant_name="MOD", kingdom_name="Kingdom 138",
            scheduled_for=scheduled_for, event_offset_minutes=-10,
        )
        event_ts = int(scheduled_for.timestamp()) - 10 * 60
        assert result == f"<t:{event_ts}:R>"

    def test_unrecognized_placeholder_left_untouched(self):
        result = render_placeholders(
            "Hello {not_a_real_placeholder}",
            tenant_name="MOD", kingdom_name="Kingdom 138",
            scheduled_for=_dt("2026-01-01T00:00:00"),
        )
        assert result == "Hello {not_a_real_placeholder}"

    def test_all_placeholders_together(self):
        scheduled_for = _dt("2026-01-01T23:45:00")
        result = render_placeholders(
            "{alliance_name}/{kingdom_name}: {send_time} {send_time_relative} {event_time} {event_time_relative}",
            tenant_name="NSR", kingdom_name="Kingdom 138",
            scheduled_for=scheduled_for, event_offset_minutes=15,
        )
        send_ts = int(scheduled_for.timestamp())
        event_ts = send_ts + 15 * 60
        assert result == (
            f"NSR/Kingdom 138: <t:{send_ts}:F> <t:{send_ts}:R> <t:{event_ts}:F> <t:{event_ts}:R>"
        )
