from datetime import date, datetime, timedelta, timezone
from typing import Generator


def does_occur_on(anchor: date, interval_days: int, target: date) -> bool:
    if target < anchor:
        return False
    return (target - anchor).days % interval_days == 0


def occurrences_in_window(
    anchor: date,
    interval_days: int,
    window_start: date,
    window_days: int = 28,
) -> Generator[date, None, None]:
    for i in range(window_days):
        d = window_start + timedelta(days=i)
        if does_occur_on(anchor, interval_days, d):
            yield d


def normalise_anchor(anchor: date, interval_days: int, today: date) -> date:
    """Advance anchor to the most recent valid occurrence on or before today."""
    if (today - anchor).days <= interval_days:
        return anchor
    n = (today - anchor).days // interval_days
    return anchor + timedelta(days=n * interval_days)


def next_occurrences(
    anchor: date,
    interval_days: int,
    from_date: date,
    count: int = 10,
) -> list[date]:
    """Return the next N occurrence dates from from_date."""
    results = []
    d = from_date
    max_days = interval_days * count + 60
    for i in range(max_days):
        candidate = from_date + timedelta(days=i)
        if does_occur_on(anchor, interval_days, candidate):
            results.append(candidate)
        if len(results) >= count:
            break
    return results


def build_start_datetime(occurrence_date: date, start_time_utc) -> datetime:
    """Combine an occurrence date and a time object into a UTC-aware datetime."""
    return datetime(
        occurrence_date.year,
        occurrence_date.month,
        occurrence_date.day,
        start_time_utc.hour,
        start_time_utc.minute,
        0,
        tzinfo=timezone.utc,
    )
