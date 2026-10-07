"""Spec §85: the pure analytics rules."""
from datetime import datetime, timedelta, timezone

import pytest

from services.analytics import (
    NO_DURATION_MINUTES, Block, block_end, delivery_trends, free_slots, heatmap, overlaps, percentile, redemption_coverage,
)

UTC = timezone.utc


def at(day, hour=19, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)  # 2026-10-05 is a Monday


def block(i, start, minutes=60, name=None, tenants=(1,), wide=False, channels=()):
    return Block(i, i, name or f"E{i}", start, start + timedelta(minutes=minutes), frozenset(tenants),
                 (), wide, frozenset(channels))


class TestBlockEnd:
    def test_no_end_gets_the_default_block(self):
        assert block_end(at(5), None) == at(5) + timedelta(minutes=NO_DURATION_MINUTES)

    def test_an_end_not_after_the_start_is_treated_as_missing(self):
        assert block_end(at(5), at(5)) == at(5) + timedelta(minutes=NO_DURATION_MINUTES)

    def test_a_real_end_is_kept(self):
        assert block_end(at(5), at(5, 21)) == at(5, 21)


class TestHeatmap:
    def test_counts_by_weekday_and_utc_hour(self):
        result = heatmap([block(1, at(5)), block(2, at(12)), block(3, at(6, 8))])
        assert result["grid"][0][19] == 2 and result["grid"][1][8] == 1 and result["total"] == 3

    def test_busiest_cell_and_empty_case(self):
        assert heatmap([block(1, at(5)), block(2, at(12))])["busiest"] == {"weekday": 0, "hour": 19, "count": 2}
        assert heatmap([])["busiest"] is None and heatmap([])["total"] == 0

    def test_ties_resolve_to_the_earliest_weekday_and_hour(self):
        assert heatmap([block(1, at(7, 5)), block(2, at(5, 20))])["busiest"]["weekday"] == 0

    def test_cell_lists_are_capped(self):
        result = heatmap([block(i, at(5) + timedelta(days=7 * i)) for i in range(15)])
        assert result["grid"][0][19] == 15 and len(result["events"]["0,19"]) == 10

    def test_every_row_has_24_hours(self):
        grid = heatmap([])["grid"]
        assert len(grid) == 7 and all(len(row) == 24 for row in grid)


class TestOverlaps:
    def test_partial_overlap(self):
        found = overlaps([block(1, at(5, 19), 120), block(2, at(5, 20), 120)])
        assert len(found) == 1 and found[0]["minutes"] == 60

    def test_nested_overlap_counts_the_inner_length(self):
        found = overlaps([block(1, at(5, 19), 180), block(2, at(5, 20), 30)])
        assert found[0]["minutes"] == 30

    def test_touching_blocks_do_not_overlap(self):
        assert overlaps([block(1, at(5, 19), 60), block(2, at(5, 20), 60)]) == []

    def test_same_start_overlaps_fully(self):
        assert overlaps([block(1, at(5), 30), block(2, at(5), 30)])[0]["minutes"] == 30

    def test_a_no_duration_event_overlaps_through_its_default_block(self):
        a = Block(1, 1, "A", at(5, 19), block_end(at(5, 19), None))
        b = Block(2, 2, "B", at(5, 19, 20), block_end(at(5, 19, 20), None))
        assert overlaps([a, b])[0]["minutes"] == 10

    def test_shared_channels_are_named(self):
        found = overlaps([block(1, at(5), 60, channels=("c1", "c2")), block(2, at(5, 19, 30), 60, channels=("c2", "c3"))])
        assert found[0]["shared_channels"] == ["c2"]

    def test_unrelated_days_do_not_pair_up(self):
        assert overlaps([block(1, at(5)), block(2, at(6))]) == []

    def test_three_way_overlap_lists_each_pair(self):
        assert len(overlaps([block(i, at(5, 19, i), 120) for i in range(3)])) == 3

    def test_the_list_is_capped(self):
        many = [block(i, at(5, 19), 60) for i in range(40)]  # 780 pairs
        assert len(overlaps(many)) == 200


class TestFreeSlots:
    NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

    def test_empty_schedule_gives_the_earliest_non_overlapping_slots(self):
        slots = free_slots([], self.NOW, 60, 2, 0, 24)
        assert [s["start"][11:16] for s in slots] == ["12:00", "13:00", "14:00", "15:00", "16:00"]

    def test_never_overlaps_a_relevant_block(self):
        blocks = [block(1, at(5, 14), 120)]
        for s in free_slots(blocks, self.NOW, 60, 3, 0, 24):
            start = datetime.fromisoformat(s["start"])
            assert not (start < blocks[0].end and blocks[0].start < start + timedelta(minutes=60))

    def test_touching_a_block_is_allowed(self):
        blocks = [block(1, at(5, 12), 60)]
        slots = free_slots(blocks, self.NOW, 60, 1, 12, 14)
        assert [x["start"][11:16] for x in slots] == ["13:00"]

    def test_allowed_hours_bound_start_and_end(self):
        slots = free_slots([], self.NOW, 120, 5, 18, 22)
        assert slots and all(18 <= datetime.fromisoformat(s["start"]).hour and datetime.fromisoformat(s["end"]).time() <= datetime(2026, 1, 1, 22).time() for s in slots)
        starts = {datetime.fromisoformat(s["start"]).strftime("%H:%M") for s in slots}
        assert "21:00" not in starts  # a 120 minute slot at 21:00 would end at 23:00

    def test_ranks_by_clearance_then_start(self):
        blocks = [block(1, at(5, 14), 60), block(2, at(5, 18), 60)]
        best = free_slots(blocks, self.NOW, 60, 1, 12, 20)[0]
        assert best["clearance_minutes"] == max(s["clearance_minutes"] for s in free_slots(blocks, self.NOW, 60, 1, 12, 20))

    def test_the_centre_of_a_gap_wins(self):
        blocks = [block(1, at(5, 12), 60), block(2, at(5, 18), 60)]  # free 13:00 to 18:00
        best = free_slots(blocks, self.NOW, 60, 1, 12, 19)[0]
        assert best["start"].startswith("2026-10-05T15:") and best["clearance_minutes"] == 120

    def test_picks_do_not_overlap_each_other(self):
        slots = free_slots([], self.NOW, 90, 3, 0, 24)
        for i, a in enumerate(slots):
            for b in slots[i + 1:]:
                assert datetime.fromisoformat(a["end"]) <= datetime.fromisoformat(b["start"]) or datetime.fromisoformat(b["end"]) <= datetime.fromisoformat(a["start"])

    def test_returns_at_most_five(self):
        assert len(free_slots([], self.NOW, 15, 5, 0, 24)) == 5

    def test_nothing_fits_returns_an_empty_list(self):
        blocks = [block(1, at(5, 0), 24 * 60), block(2, at(6, 0), 24 * 60)]
        assert free_slots(blocks, at(5, 0), 60, 2, 0, 24) == []

    def test_only_selected_alliances_are_kept_clear_but_kingdom_wide_always(self):
        blocks = [block(1, at(5, 12), 60, tenants=(1,)), block(2, at(5, 13), 60, tenants=(2,)), block(3, at(5, 14), 60, tenants=(9,), wide=True)]
        slots = free_slots(blocks, self.NOW, 60, 1, 12, 15, alliance_ids={2})
        assert [x["start"][11:16] for x in slots] == ["12:00"]  # alliance 1's 12:00 event is ignored, the rest block 13:00 to 15:00

    def test_the_start_is_rounded_up_to_the_quarter_hour(self):
        slots = free_slots([], datetime(2026, 10, 5, 12, 1, tzinfo=UTC), 60, 1, 0, 24)
        assert slots[0]["start"].startswith("2026-10-05T12:15")


class TestPercentile:
    @pytest.mark.parametrize("values,pct,expected", [
        ([], 50, None), ([5], 50, 5), ([1, 2, 3, 4], 50, 2), ([1, 2, 3, 4], 95, 4),
        ([10, 20, 30, 40, 50, 60, 70, 80, 90, 100], 95, 100), ([10, 20, 30, 40, 50, 60, 70, 80, 90, 100], 50, 50),
    ])
    def test_nearest_rank(self, values, pct, expected):
        assert percentile(values, pct) == expected


def _row(day, status="posted", late=30, dest="A"):
    return {"due": at(day, 18), "status": status, "lateness_seconds": late if status == "posted" else None, "destination": dest}


class TestDeliveryTrends:
    NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)

    def test_every_day_of_the_window_is_present_oldest_first(self):
        series = delivery_trends([], self.NOW, 3)["days"]
        assert [d["date"] for d in series] == ["2026-10-05", "2026-10-06", "2026-10-07"]
        assert all(d["due"] == 0 and d["median_lateness_seconds"] is None for d in series)

    def test_counts_and_lateness_per_day(self):
        rows = [_row(6, late=10), _row(6, late=50), _row(6, "error"), _row(7, late=5)]
        series = {d["date"]: d for d in delivery_trends(rows, self.NOW, 3)["days"]}
        assert (series["2026-10-06"]["due"], series["2026-10-06"]["posted"], series["2026-10-06"]["errors"]) == (3, 2, 1)
        assert series["2026-10-06"]["median_lateness_seconds"] == 10 and series["2026-10-06"]["p95_lateness_seconds"] == 50

    def test_rows_outside_the_window_are_ignored(self):
        assert sum(d["due"] for d in delivery_trends([_row(1)], self.NOW, 3)["days"]) == 0

    def test_by_destination_groups_and_sorts(self):
        result = delivery_trends([_row(6, dest="B"), _row(6, dest="A"), _row(7, "error", dest="A")], self.NOW, 3, "destination")
        assert [d["destination"] for d in result["destinations"]] == ["A", "B"]
        assert result["destinations"][0]["errors"] == 1 and result["destinations"][0]["due"] == 2


class TestCoverage:
    RUNS = [{"id": 2, "code": "NEW", "created_at": "x"}, {"id": 1, "code": "OLD", "created_at": "y"}]

    def test_percentages_per_alliance_and_run(self):
        results = [{"run_id": 2, "tenant": "MOD", "group": g} for g in ("redeemed", "redeemed", "already", "wrong_kingdom")]
        results += [{"run_id": 2, "tenant": "NSR", "group": "other"}, {"run_id": 1, "tenant": "MOD", "group": "redeemed"}]
        out = redemption_coverage(self.RUNS, results)
        mod = out[0]["alliances"][0]
        assert (mod["alliance"], mod["players"], mod["redeemed"], mod["already"], mod["wrong_kingdom"], mod["coverage_percent"]) == ("MOD", 4, 2, 1, 1, 75)
        assert out[0]["alliances"][1]["coverage_percent"] == 0 and out[1]["alliances"][0]["coverage_percent"] == 100

    def test_other_groups_are_lumped_into_other(self):
        out = redemption_coverage(self.RUNS[:1], [{"run_id": 2, "tenant": "MOD", "group": g} for g in ("requirement", "waiting", "code")])
        assert out[0]["alliances"][0]["other"] == 3

    def test_a_run_with_no_results_has_no_alliances(self):
        assert redemption_coverage(self.RUNS[:1], [])[0]["alliances"] == []
