"""App logic that needs no window: the copper-thickness what-if of the solve settings (its
summary line, the settings built from the dialog's fields, the keys for layers the board
does not have)."""

import pytest

from coppernicus.project import ProjectError


def test_summary_says_board_file_when_nothing_is_set():
    from coppernicus.app.copper_thickness import thickness_summary
    assert thickness_summary({}) == "board file"


def test_summary_names_each_layer_with_its_thickness_in_mm():
    from coppernicus.app.copper_thickness import thickness_summary
    assert thickness_summary({"B.Cu": 0.07}) == "B.Cu 0.07 mm"
    assert thickness_summary({"F.Cu": 0.035, "B.Cu": 0.0700}) == "F.Cu 0.035 mm, B.Cu 0.07 mm"


def test_an_empty_field_means_the_board_files_value_so_its_key_is_removed():
    from coppernicus.app.copper_thickness import thickness_from_texts
    assert thickness_from_texts({"F.Cu": "", "B.Cu": "  "}, {}) == {}
    assert thickness_from_texts({"F.Cu": "", "B.Cu": "0.07"}, {}) == {"B.Cu": 0.07}


def test_a_typed_value_keeps_its_key_even_when_it_equals_the_board_files():
    from coppernicus.app.copper_thickness import thickness_from_texts
    assert thickness_from_texts({"F.Cu": "0.035", "B.Cu": ""}, {}) == {"F.Cu": 0.035}


def test_a_comma_is_read_as_the_decimal_sign():
    from coppernicus.app.copper_thickness import thickness_from_texts
    assert thickness_from_texts({"B.Cu": "0,07"}, {}) == {"B.Cu": 0.07}


@pytest.mark.parametrize("text", ["35", "0", "-0.035", "nan", "inf", "1.5", "oz"])
def test_a_value_out_of_bounds_or_not_a_number_is_refused_naming_the_layer(text):
    from coppernicus.app.copper_thickness import thickness_from_texts
    with pytest.raises(ProjectError, match=r"copper_thickness_mm\[B\.Cu\]"):
        thickness_from_texts({"F.Cu": "0.07", "B.Cu": text}, {})


def test_the_bound_is_the_projects_one_mm_inclusive():
    from coppernicus.app.copper_thickness import thickness_from_texts
    assert thickness_from_texts({"B.Cu": "1"}, {}) == {"B.Cu": 1.0}


def test_keys_for_layers_not_on_the_board_are_kept_after_the_boards_layers():
    from coppernicus.app.copper_thickness import thickness_from_texts
    kept = {"In3.Cu": 0.05}
    assert list(thickness_from_texts({"F.Cu": "", "B.Cu": "0.07"}, kept).items()) == [
        ("B.Cu", 0.07), ("In3.Cu", 0.05)]
    assert thickness_from_texts({"F.Cu": "", "B.Cu": ""}, {}) == {}


def test_off_board_lists_the_keys_the_board_has_no_copper_layer_for_in_their_order():
    from coppernicus.app.copper_thickness import off_board
    values = {"In3.Cu": 0.05, "B.Cu": 0.07, "In1.Cu": 0.035}
    assert off_board(values, ["F.Cu", "B.Cu"]) == ["In3.Cu", "In1.Cu"]
    assert off_board({}, ["F.Cu", "B.Cu"]) == []


def test_the_window_opens_at_most_as_large_as_the_screen_s_free_area():
    """1500 × 920 on a large screen; shrunk to the available area of a 1440 × 900 one."""
    from coppernicus.app.main_window import initial_size
    assert initial_size(2560, 1400) == (1500, 920)
    assert initial_size(1440, 875) == (1440, 875)
    assert initial_size(None, None) == (1500, 920)          # no screen known (offscreen)
