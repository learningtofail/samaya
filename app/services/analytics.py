"""Schedule insights (spec §85): pure functions over already loaded rows.

No database, no Discord, no clock of their own: callers pass rows and `now`,
so every rule here is a plain unit test. Weekdays count from Monday = 0, all
times are UTC.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

NO_DURATION_MINUTES = 30
MAX_EVENTS_PER_CELL = 10
MAX_OVERLAPS = 200
FREE_SLOT_RESULTS = 5
GRID_MINUTES = 15
MINUTES_PER_DAY = 24 * 60
NO_NEIGHBOUR = MINUTES_PER_DAY  # clearance cap, so an empty week does not rank by distance to nothing


@dataclass(frozen=True)
class Block:
    """One occurrence as a time interval. `end` already includes the no-duration block."""
    occurrence_id: int
    event_id: int
    name: str
    start: datetime
    end: datetime
    tenant_ids: frozenset[int] = frozenset()
    alliances: tuple[str, ...] = ()
    kingdom_wide: bool = False
    channels: frozenset[str] = field(default_factory=frozenset)


def block_end(start: datetime, end: datetime | None) -> datetime:
    return end if end is not None and end > start else start + timedelta(minutes=NO_DURATION_MINUTES)


def heatmap(blocks: list[Block]) -> dict:
    """Event starts by weekday and UTC hour. `grid[weekday][hour]` is a count; `events` lists what is behind
    each non-empty cell as {"d,h": [{"name", "start"}]}, at most MAX_EVENTS_PER_CELL each."""
    grid = [[0] * 24 for _ in range(7)]
    events: dict[str, list[dict]] = {}
    for b in sorted(blocks, key=lambda x: (x.start, x.occurrence_id)):
        day, hour = b.start.weekday(), b.start.hour
        grid[day][hour] += 1
        cell = events.setdefault(f"{day},{hour}", [])
        if len(cell) < MAX_EVENTS_PER_CELL:
            cell.append({"name": b.name, "start": b.start.isoformat()})
    busiest = None
    for day in range(7):
        for hour in range(24):
            if grid[day][hour] and (busiest is None or grid[day][hour] > busiest["count"]):
                busiest = {"weekday": day, "hour": hour, "count": grid[day][hour]}
    return {"grid": grid, "events": events, "busiest": busiest, "total": sum(map(sum, grid))}


def overlaps(blocks: list[Block]) -> list[dict]:
    """Pairs whose intervals intersect. Intervals that only touch do not overlap."""
    ordered = sorted(blocks, key=lambda x: (x.start, x.occurrence_id))
    found: list[dict] = []
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            if b.start >= a.end:
                break
            minutes = int((min(a.end, b.end) - b.start).total_seconds() // 60)
            found.append({
                "start": b.start.isoformat(), "minutes": minutes,
                "first": {"occurrence_id": a.occurrence_id, "event_id": a.event_id, "name": a.name, "alliances": list(a.alliances)},
                "second": {"occurrence_id": b.occurrence_id, "event_id": b.event_id, "name": b.name, "alliances": list(b.alliances)},
                "shared_channels": sorted(a.channels & b.channels),
            })
            if len(found) >= MAX_OVERLAPS:
                return found
    return found


def _ceil_to_grid(moment: datetime) -> datetime:
    floor = moment.replace(second=0, microsecond=0)
    floor -= timedelta(minutes=floor.minute % GRID_MINUTES)
    return floor if floor == moment else floor + timedelta(minutes=GRID_MINUTES)


def free_slots(blocks: list[Block], now: datetime, duration: int, days: int, from_hour: int, to_hour: int,
               alliance_ids: set[int] | None = None) -> list[dict]:
    """The best start times for an event of `duration` minutes: on a quarter-hour grid, inside the allowed UTC
    hours, not overlapping a relevant block, ranked by clearance (minutes to the nearest block, larger first)
    then by start. Picks never overlap each other. Kingdom-wide events always count; an alliance event counts
    when it belongs to one of `alliance_ids` (everything counts when that is None)."""
    relevant = sorted(
        (b for b in blocks if alliance_ids is None or b.kingdom_wide or b.tenant_ids & alliance_ids),
        key=lambda b: b.start)
    horizon = now + timedelta(days=days)
    length = timedelta(minutes=duration)
    candidates: list[tuple[int, datetime]] = []
    start = _ceil_to_grid(now)
    while start + length <= horizon:
        minute_of_day = start.hour * 60 + start.minute
        if from_hour * 60 <= minute_of_day and minute_of_day + duration <= to_hour * 60:
            end = start + length
            clear = NO_NEIGHBOUR
            blocked = False
            for b in relevant:
                if b.start < end and start < b.end:
                    blocked = True
                    break
                clear = min(clear, int((start - b.end).total_seconds() // 60) if b.end <= start
                            else int((b.start - end).total_seconds() // 60))
            if not blocked:
                candidates.append((min(clear, NO_NEIGHBOUR), start))
        start += timedelta(minutes=GRID_MINUTES)
    candidates.sort(key=lambda c: (-c[0], c[1]))
    picks: list[dict] = []
    for clear, begin in candidates:
        if any(begin < p_end and p_start < begin + length for p_start, p_end in
               ((datetime.fromisoformat(p["start"]), datetime.fromisoformat(p["end"])) for p in picks)):
            continue
        picks.append({"start": begin.isoformat(), "end": (begin + length).isoformat(), "clearance_minutes": clear})
        if len(picks) >= FREE_SLOT_RESULTS:
            break
    return picks


def percentile(values: list[float], pct: float) -> float | None:
    """Nearest rank. None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * pct // 100))  # ceil without float error
    return ordered[int(rank) - 1]


def _summary(rows: list[dict]) -> dict:
    late = [r["lateness_seconds"] for r in rows if r["status"] == "posted" and r["lateness_seconds"] is not None]
    return {
        "due": len(rows),
        "posted": sum(1 for r in rows if r["status"] == "posted"),
        "errors": sum(1 for r in rows if r["status"] == "error"),
        "median_lateness_seconds": percentile(late, 50),
        "p95_lateness_seconds": percentile(late, 95),
    }


def delivery_trends(rows: list[dict], now: datetime, days: int, by: str = "day") -> dict:
    """`rows`: {"due": datetime, "status", "lateness_seconds", "destination": str}, merged deliveries already
    removed and only deliveries that have come due. by="day" fills every day of the window (oldest first)
    with zeros; by="destination" groups instead."""
    first = (now - timedelta(days=days - 1)).date()
    in_window = [r for r in rows if first <= r["due"].date() <= now.date()]
    if by == "destination":
        groups: dict[str, list[dict]] = {}
        for r in in_window:
            groups.setdefault(r["destination"], []).append(r)
        return {"destinations": [{"destination": key, **_summary(items)} for key, items in sorted(groups.items())]}
    series = []
    for offset in range(days):
        day = first + timedelta(days=offset)
        series.append({"date": day.isoformat(), **_summary([r for r in in_window if r["due"].date() == day])})
    return {"days": series}


def redemption_coverage(runs: list[dict], results: list[dict]) -> list[dict]:
    """`runs`: {"id", "code", "created_at"} newest first. `results`: {"run_id", "tenant", "group"} with the group
    from giftcode_engine.outcome_group. Coverage is (redeemed + already) over players, per alliance and run."""
    out = []
    for run in runs:
        per: dict[str, dict[str, int]] = {}
        for r in results:
            if r["run_id"] != run["id"]:
                continue
            counts = per.setdefault(r["tenant"], {"players": 0, "redeemed": 0, "already": 0, "wrong_kingdom": 0, "other": 0})
            counts["players"] += 1
            counts[r["group"] if r["group"] in ("redeemed", "already", "wrong_kingdom") else "other"] += 1
        alliances = []
        for name, c in sorted(per.items()):
            covered = c["redeemed"] + c["already"]
            alliances.append({"alliance": name, **c, "coverage_percent": round(100 * covered / c["players"]) if c["players"] else 0})
        out.append({**run, "alliances": alliances})
    return out
