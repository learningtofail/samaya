"""Spec §63.1: contrast math, and the shared fixture table the frontend's
brandInk() is tested against (tests/frontend/events-public.test.js)."""
import itertools
import json
from pathlib import Path

import pytest

from services.contrast import contrast_ratio, faint_on_white_note, pick_ink, relative_luminance

FIXTURES = json.loads((Path(__file__).parent.parent.parent / "tests" / "frontend" / "ink-fixtures.json").read_text())


@pytest.mark.parametrize("hex_color,ink", FIXTURES)
def test_pick_ink_matches_shared_fixture(hex_color, ink):
    assert pick_ink(hex_color) == ink


def test_extremes():
    assert relative_luminance("#000000") == 0
    assert relative_luminance("#FFFFFF") == pytest.approx(1)
    assert contrast_ratio("#000000", "#FFFFFF") == pytest.approx(21)


def test_no_color_is_ever_unreadable():
    # Sweep a coarse RGB grid: black or white always reaches the WCAG floor.
    steps = range(0, 256, 15)
    for r, g, b in itertools.product(steps, steps, steps):
        color = f"#{r:02X}{g:02X}{b:02X}"
        best = max(contrast_ratio(color, "#000000"), contrast_ratio(color, "#FFFFFF"))
        assert best >= 4.58, color


def test_faint_note_only_for_very_light_colors():
    assert faint_on_white_note("#E65100") is None
    assert faint_on_white_note(None) is None
    assert "very light" in faint_on_white_note("#FFFF99")
