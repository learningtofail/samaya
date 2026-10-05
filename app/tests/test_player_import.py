"""Spec §79.6, §80.3: pasted player lines."""
import pytest

from services.player_import import MAX_LINES, TooManyLines, parse_player_lines


def _flat(parsed):
    return [(e.fid, e.kid, e.name) for e in parsed.entries]


def test_a_bare_id_per_line():
    assert _flat(parse_player_lines("12345678\n87654321\n")) == [("12345678", None, None), ("87654321", None, None)]


def test_fid_and_kingdom():
    assert _flat(parse_player_lines("12345678,245")) == [("12345678", 245, None)]


def test_fid_kingdom_and_name_with_a_comma():
    assert _flat(parse_player_lines("12345678,245,Smith, John")) == [("12345678", 245, "Smith, John")]


def test_a_second_field_that_is_not_a_kingdom_is_the_name():
    assert _flat(parse_player_lines("12345678,Sir Bear")) == [("12345678", None, "Sir Bear")]


def test_an_empty_kingdom_slot_keeps_the_name():
    assert _flat(parse_player_lines("12345678,,Sir Bear")) == [("12345678", None, "Sir Bear")]


def test_several_ids_on_one_line_are_separate_players():
    assert [e.fid for e in parse_player_lines("111111111,222222222,333333333").entries] == ["111111111", "222222222", "333333333"]
    assert [e.fid for e in parse_player_lines("111111111 222222222").entries] == ["111111111", "222222222"]


def test_comments_blank_lines_and_tabs():
    parsed = parse_player_lines("# fid,kid\n\n12345678\t245\n")
    assert _flat(parsed) == [("12345678", 245, None)]


@pytest.mark.parametrize("line", ["abc", "1234", "12345678901234567890123", "12.5"])
def test_bad_ids_are_rejected_with_a_reason(line):
    parsed = parse_player_lines(line)
    assert parsed.entries == [] and len(parsed.rejected) == 1


def test_a_long_name_is_rejected_not_cut():
    parsed = parse_player_lines("12345678,245," + "x" * 61)
    assert parsed.entries == [] and "longer than 60" in parsed.rejected[0][1]


def test_control_and_invisible_characters_leave_names():
    assert _flat(parse_player_lines("12345678,245,Bear​Hunter\x07")) == [("12345678", 245, "BearHunter")]


def test_a_repeated_id_keeps_the_latest_details_and_is_counted():
    parsed = parse_player_lines("12345678\n12345678,300,Bear")
    assert _flat(parsed) == [("12345678", 300, "Bear")] and parsed.repeated == 1


def test_too_many_lines():
    with pytest.raises(TooManyLines):
        parse_player_lines("\n".join(str(10000000 + i) for i in range(MAX_LINES + 1)))
