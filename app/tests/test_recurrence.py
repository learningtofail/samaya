"""
Tests for the recurrence engine — does_occur_on, occurrences_in_window,
normalise_anchor, next_occurrences.

These are pure functions with no I/O. They should be fast and exhaustive.
"""
from datetime import date, timedelta


from services.recurrence import (
    does_occur_on,
    next_occurrences,
    normalise_anchor,
    occurrences_in_window,
    build_start_datetime,
)


# ── does_occur_on ─────────────────────────────────────────────

class TestDoesOccurOn:

    def test_anchor_date_itself_occurs(self):
        anchor = date(2025, 5, 1)
        assert does_occur_on(anchor, 7, anchor) is True

    def test_one_interval_later_occurs(self):
        anchor = date(2025, 5, 1)
        assert does_occur_on(anchor, 7, date(2025, 5, 8)) is True

    def test_two_intervals_later_occurs(self):
        anchor = date(2025, 5, 1)
        assert does_occur_on(anchor, 7, date(2025, 5, 15)) is True

    def test_non_occurrence_day_does_not_occur(self):
        anchor = date(2025, 5, 1)
        assert does_occur_on(anchor, 7, date(2025, 5, 2)) is False

    def test_before_anchor_does_not_occur(self):
        anchor = date(2025, 5, 1)
        assert does_occur_on(anchor, 7, date(2025, 4, 24)) is False

    def test_daily_interval(self):
        anchor = date(2025, 5, 1)
        for i in range(10):
            assert does_occur_on(anchor, 1, anchor + timedelta(days=i)) is True

    def test_every_other_day(self):
        anchor = date(2025, 5, 1)
        assert does_occur_on(anchor, 2, date(2025, 5, 3)) is True
        assert does_occur_on(anchor, 2, date(2025, 5, 2)) is False
        assert does_occur_on(anchor, 2, date(2025, 5, 5)) is True

    def test_biweekly(self):
        anchor = date(2025, 5, 1)
        assert does_occur_on(anchor, 14, date(2025, 5, 15)) is True
        assert does_occur_on(anchor, 14, date(2025, 5, 8))  is False
        assert does_occur_on(anchor, 14, date(2025, 5, 29)) is True

    def test_28_day_cycle(self):
        anchor = date(2025, 5, 1)
        assert does_occur_on(anchor, 28, date(2025, 5, 29)) is True
        assert does_occur_on(anchor, 28, date(2025, 6, 26)) is True
        assert does_occur_on(anchor, 28, date(2025, 6,  1)) is False

    def test_interval_1_always_true_from_anchor(self):
        anchor = date(2025, 1, 1)
        for i in range(365):
            assert does_occur_on(anchor, 1, anchor + timedelta(days=i)) is True


# ── occurrences_in_window ─────────────────────────────────────

class TestOccurrencesInWindow:

    def test_weekly_28_day_window_returns_4(self):
        anchor = date(2025, 5, 1)
        results = list(occurrences_in_window(anchor, 7, anchor, 28))
        assert len(results) == 4

    def test_daily_28_day_window_returns_28(self):
        anchor = date(2025, 5, 1)
        results = list(occurrences_in_window(anchor, 1, anchor, 28))
        assert len(results) == 28

    def test_every_other_day_28_window_returns_14(self):
        anchor = date(2025, 5, 1)
        results = list(occurrences_in_window(anchor, 2, anchor, 28))
        assert len(results) == 14

    def test_28_day_cycle_returns_1(self):
        anchor = date(2025, 5, 1)
        results = list(occurrences_in_window(anchor, 28, anchor, 28))
        assert len(results) == 1

    def test_window_start_after_anchor(self):
        anchor = date(2025, 5, 1)
        window_start = date(2025, 5, 8)
        results = list(occurrences_in_window(anchor, 7, window_start, 28))
        # Should find Thursdays: May 8, 15, 22, 29
        assert len(results) == 4
        assert results[0] == date(2025, 5, 8)

    def test_results_are_date_objects(self):
        anchor = date(2025, 5, 1)
        results = list(occurrences_in_window(anchor, 7, anchor, 28))
        for r in results:
            assert isinstance(r, date)

    def test_results_are_sorted_ascending(self):
        anchor = date(2025, 5, 1)
        results = list(occurrences_in_window(anchor, 7, anchor, 28))
        assert results == sorted(results)

    def test_empty_window(self):
        anchor = date(2025, 5, 1)
        results = list(occurrences_in_window(anchor, 7, anchor, 0))
        assert results == []

    def test_biweekly_returns_2_in_28_days(self):
        anchor = date(2025, 5, 1)
        results = list(occurrences_in_window(anchor, 14, anchor, 28))
        assert len(results) == 2
        assert results[0] == date(2025, 5, 1)
        assert results[1] == date(2025, 5, 15)


# ── normalise_anchor ──────────────────────────────────────────

class TestNormaliseAnchor:

    def test_anchor_within_one_interval_unchanged(self):
        today = date(2025, 6, 1)
        anchor = date(2025, 5, 29)  # 3 days ago, interval=7
        result = normalise_anchor(anchor, 7, today)
        assert result == anchor

    def test_anchor_more_than_one_interval_advances(self):
        today = date(2025, 6, 1)
        anchor = date(2025, 5, 1)  # 31 days ago, interval=7
        result = normalise_anchor(anchor, 7, today)
        # Most recent Thursday on or before today
        assert result <= today
        assert does_occur_on(anchor, 7, result)

    def test_normalised_anchor_is_on_or_before_today(self):
        today = date(2025, 9, 10)
        anchor = date(2025, 1, 1)
        result = normalise_anchor(anchor, 14, today)
        assert result <= today

    def test_normalised_anchor_preserves_recurrence_phase(self):
        anchor = date(2025, 5, 1)
        today = date(2025, 9, 10)
        result = normalise_anchor(anchor, 7, today)
        # Result must be a valid occurrence of the original event
        assert does_occur_on(anchor, 7, result)

    def test_daily_anchor_advances_to_yesterday_or_today(self):
        today = date(2025, 9, 10)
        anchor = date(2024, 1, 1)
        result = normalise_anchor(anchor, 1, today)
        assert result <= today
        assert (today - result).days < 1  # daily: should be today or yesterday


# ── next_occurrences ─────────────────────────────────────────

class TestNextOccurrences:

    def test_returns_requested_count(self):
        anchor = date(2025, 5, 1)
        results = next_occurrences(anchor, 7, date(2025, 5, 1), count=10)
        assert len(results) == 10

    def test_first_result_is_on_or_after_from_date(self):
        anchor = date(2025, 5, 1)
        from_date = date(2025, 5, 5)
        results = next_occurrences(anchor, 7, from_date, count=5)
        assert all(r >= from_date for r in results)

    def test_results_spaced_by_interval(self):
        anchor = date(2025, 5, 1)
        results = next_occurrences(anchor, 7, anchor, count=4)
        for i in range(1, len(results)):
            assert (results[i] - results[i-1]).days == 7

    def test_every_other_day_spacing(self):
        anchor = date(2025, 5, 1)
        results = next_occurrences(anchor, 2, anchor, count=5)
        for i in range(1, len(results)):
            assert (results[i] - results[i-1]).days == 2

    def test_all_results_are_valid_occurrences(self):
        anchor = date(2025, 5, 1)
        results = next_occurrences(anchor, 14, date(2025, 6, 1), count=6)
        for r in results:
            assert does_occur_on(anchor, 14, r)


# ── build_start_datetime ──────────────────────────────────────

class TestBuildStartDatetime:
    from datetime import time as dtime

    def test_combines_date_and_time(self):
        from datetime import time as dtime
        occ_date = date(2025, 5, 1)
        t = dtime(19, 0)
        result = build_start_datetime(occ_date, t)
        assert result.year  == 2025
        assert result.month == 5
        assert result.day   == 1
        assert result.hour  == 19
        assert result.minute == 0

    def test_result_is_utc_aware(self):
        from datetime import time as dtime
        result = build_start_datetime(date(2025, 5, 1), dtime(0, 0))
        assert result.tzinfo is not None
        assert result.utcoffset().total_seconds() == 0


class TestUnifiedRecurrence:
    """Spec §66.5: one predicate for 'none' and 'interval_days', with an
    optional last date (set when a series is split)."""

    def test_none_is_only_the_anchor_date(self):
        from datetime import date
        from services.recurrence import occurs_on
        anchor = date(2026, 10, 5)
        assert occurs_on("none", anchor, None, None, anchor)
        assert not occurs_on("none", anchor, None, None, date(2026, 10, 6))
        assert not occurs_on("none", anchor, None, None, date(2026, 10, 4))

    def test_interval_days_matches_existing_predicate(self):
        from datetime import date, timedelta
        from services.recurrence import does_occur_on, occurs_on
        anchor = date(2026, 10, 1)
        for offset in range(-3, 40):
            target = anchor + timedelta(days=offset)
            assert occurs_on("interval_days", anchor, 7, None, target) == does_occur_on(anchor, 7, target)

    def test_until_is_inclusive_and_stops_the_series(self):
        from datetime import date
        from services.recurrence import occurs_on
        anchor, until = date(2026, 10, 1), date(2026, 10, 15)
        assert occurs_on("interval_days", anchor, 7, until, date(2026, 10, 15))
        assert not occurs_on("interval_days", anchor, 7, until, date(2026, 10, 22))

    def test_until_applies_to_one_off_too(self):
        from datetime import date
        from services.recurrence import occurs_on
        assert not occurs_on("none", date(2026, 10, 5), None, date(2026, 10, 4), date(2026, 10, 5))

    def test_unknown_kind_raises(self):
        import pytest
        from datetime import date
        from services.recurrence import occurs_on
        with pytest.raises(ValueError):
            occurs_on("monthly_day", date(2026, 10, 1), None, None, date(2026, 10, 1))

    def test_missing_interval_never_matches(self):
        from datetime import date
        from services.recurrence import occurs_on
        assert not occurs_on("interval_days", date(2026, 10, 1), None, None, date(2026, 10, 1))

    def test_dates_in_window(self):
        from datetime import date
        from services.recurrence import event_dates_in_window
        got = event_dates_in_window("interval_days", date(2026, 10, 1), 7, date(2026, 10, 20), date(2026, 10, 1), 28)
        assert got == [date(2026, 10, 1), date(2026, 10, 8), date(2026, 10, 15)]
        assert event_dates_in_window("none", date(2026, 10, 9), None, None, date(2026, 10, 1), 28) == [date(2026, 10, 9)]
        assert event_dates_in_window("none", date(2026, 12, 9), None, None, date(2026, 10, 1), 28) == []
