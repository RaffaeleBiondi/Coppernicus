"""App logic that needs no window: the board view's hover readout (its text with the
result's drop wording, its place beside the cursor, the stored choice of values)."""


def _rail(v_ref, **kw):
    from coppernicus.results import RailResult
    return RailResult(kw.pop("net", "VCC"), kw.pop("nets", ["VCC"]), v_ref, [], [], [], [],
                      kw.pop("max_drop", 0.0), 0.0, 0.0, 0.0, **kw)


def _probe(v, j=2.5):
    from coppernicus.fields import Probe
    return Probe(1.0, 2.0, 0, v, j)


ALL = ("v", "drop", "j", "layer", "net")


def test_hover_text_shows_each_chosen_value_in_a_fixed_order():
    from coppernicus.app.hover import hover_text, hover_values
    values = hover_values(_rail(3.3, net="+3V3", nets=["+3V3"]), "F.Cu", _probe(3.29))
    assert hover_text(values, ["v"]) == "V = 3.29000 V"
    assert hover_text(values, ["drop"]) == "drop = 10.000 mV below 3.3 V"
    assert hover_text(values, ["j"]) == "|J| = 2.50 A/mm²"
    assert hover_text(values, ["layer"]) == "F.Cu"
    assert hover_text(values, ["net"]) == "+3V3"
    assert hover_text(values, ["net", "layer", "drop", "v", "j"]) == (
        "V = 3.29000 V\ndrop = 10.000 mV below 3.3 V\n|J| = 2.50 A/mm²\nF.Cu · +3V3")


def test_hover_text_is_empty_when_nothing_is_chosen():
    from coppernicus.app.hover import hover_text, hover_values
    values = hover_values(_rail(3.3), "F.Cu", _probe(3.29))
    assert hover_text(values, []) == "" and hover_text(values, ()) == ""
    assert hover_text(None, ALL) == ""                          # off the copper: nothing to show


def test_hover_drop_uses_the_results_wording_for_each_sign():
    from coppernicus.app.hover import hover_text, hover_values

    def drop(result, v):
        return hover_text(hover_values(result, "F.Cu", _probe(v)), ["drop"])
    assert drop(_rail(3.3), 3.29) == "drop = 10.000 mV below 3.3 V"
    assert drop(_rail(-3.3, drop_sign=-1.0), -3.29) == "drop = 10.000 mV above -3.3 V (toward 0 V)"
    assert drop(_rail(0.0, drop_sign=-1.0), 0.002) == "rise = 2.000 mV above 0 V (ground rise)"   # 0 V alone
    assert drop(_rail(0.0, net="GND", nets=["GND"], kind="ground"), 0.002) == (
        "rise = 2.000 mV above 0 V (ground rise)")
    assert drop(_rail(0.0), -0.002) == "drop = 2.000 mV below 0 V"   # a 0 V rail group of a tree draws
    assert drop(_rail(3.3), 3.301) == "drop = -1.000 mV below 3.3 V"  # a boost reads negative, as the map


def test_hover_on_a_ground_below_0_v_reads_its_depth_below_0_v():
    """A ground under a negative rail (its largest deviation, max_drop, below 0 V) is a
    shift: the readout gives how far below 0 V the point is, never "-2 mV below 0 V" for a
    point 2 mV below it; a point above 0 V on it reads negative, as a boost does."""
    from coppernicus.app.hover import hover_text, hover_values

    def drop(result, v):
        return hover_text(hover_values(result, "F.Cu", _probe(v)), ["drop"])
    shifted = _rail(0.0, net="GND", nets=["GND"], kind="ground", max_drop=-0.005)
    assert drop(shifted, -0.002) == "shift = 2.000 mV below 0 V (ground shift)"
    assert drop(shifted, 0.001) == "shift = -1.000 mV below 0 V (ground shift)"
    alone = _rail(0.0, net="GND", nets=["GND"], drop_sign=-1.0, max_drop=-0.005)
    assert drop(alone, -0.002) == "shift = 2.000 mV below 0 V (ground shift)"
    risen = _rail(0.0, net="GND", nets=["GND"], kind="ground", max_drop=0.005)
    assert drop(risen, -0.002) == "rise = -2.000 mV above 0 V (ground rise)"


def test_hover_on_copper_not_connected_to_a_source_says_so():
    from coppernicus.app.hover import hover_text, hover_values
    values = hover_values(_rail(3.3), "B.Cu", _probe(float("nan"), j=0.0))
    assert hover_text(values, ["v", "drop", "j"]) == "not connected"
    assert hover_text(values, ["layer"]) == "not connected\nB.Cu"
    assert hover_text(values, []) == ""


def test_hover_net_names_every_net_of_the_result():
    from coppernicus.app.hover import hover_text, hover_values
    tied = _rail(3.3, net="VCC", nets=["VCC", "VCC_A"])
    assert hover_text(hover_values(tied, "F.Cu", _probe(3.3)), ["net"]) == "VCC + VCC_A"


def test_hover_label_sits_14_px_from_the_cursor_and_flips_inside_the_viewport():
    from coppernicus.app.hover import label_position
    size, viewport = (80, 40), (800, 600)
    assert label_position((100, 100), size, viewport) == (114, 114)
    assert label_position((790, 100), size, viewport) == (696, 114)      # right edge: left of it
    assert label_position((100, 590), size, viewport) == (114, 536)      # bottom edge: above it
    assert label_position((790, 590), size, viewport) == (696, 536)
    assert label_position((10, 10), size, (50, 30)) == (0, 0)            # bigger than the view: clamped
    for cx in range(0, 800, 37):                                         # never under the cursor
        for cy in range(0, 600, 29):
            x, y = label_position((cx, cy), size, viewport)
            assert 0 <= x <= 720 and 0 <= y <= 560
            assert not (x <= cx < x + 80 and y <= cy < y + 40), (cx, cy, x, y)


def test_hover_choice_round_trips_through_its_setting():
    from coppernicus.app.hover import HOVER_DEFAULT, HOVER_VALUES, chosen_from_setting, setting_from_chosen
    assert [label for _key, label in HOVER_VALUES] == ["V", "drop", "|J| (A/mm²)", "layer", "net"]
    assert HOVER_DEFAULT == ("v", "drop", "layer")
    assert chosen_from_setting(None) == HOVER_DEFAULT                    # never set: the defaults
    assert chosen_from_setting("") == ()                                 # none checked: readout off
    assert chosen_from_setting(setting_from_chosen([])) == ()
    assert chosen_from_setting("net,v,old_key") == ("v", "net")          # known keys, menu order
    assert setting_from_chosen(["layer", "j"]) == "j,layer"
