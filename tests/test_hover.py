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


def test_hover_label_sits_16_px_right_12_px_below_the_cursor_and_flips_inside_the_viewport():
    from coppernicus.app.hover import label_position
    size, viewport = (80, 40), (800, 600)
    assert label_position((100, 100), size, viewport) == (116, 112)
    assert label_position((790, 100), size, viewport) == (694, 112)      # right edge: left of it
    assert label_position((100, 590), size, viewport) == (116, 538)      # bottom edge: above it
    assert label_position((790, 590), size, viewport) == (694, 538)
    assert label_position((10, 10), size, (50, 30)) == (0, 0)            # bigger than the view: clamped
    assert label_position((100, 100), size, viewport, offset=14) == (114, 114)   # one offset for both
    for cx in range(0, 800, 37):                                         # never under the cursor
        for cy in range(0, 600, 29):
            x, y = label_position((cx, cy), size, viewport)
            assert 0 <= x <= 720 and 0 <= y <= 560
            assert not (x <= cx < x + 80 and y <= cy < y + 40), (cx, cy, x, y)


def test_hover_choice_round_trips_through_its_setting():
    from coppernicus.app.hover import HOVER_DEFAULT, HOVER_VALUES, chosen_from_setting, setting_from_chosen
    assert [label for _key, label in HOVER_VALUES] == ["map value (large)", "V", "drop", "|J| (A/mm²)",
                                                       "layer", "net"]
    assert HOVER_DEFAULT == ("shown", "layer")                           # the map's value and the layer
    assert chosen_from_setting(None) == HOVER_DEFAULT                    # never set: the defaults
    assert chosen_from_setting("") == ()                                 # none checked: readout off
    assert chosen_from_setting(setting_from_chosen([])) == ()
    assert chosen_from_setting("net,v,old_key") == ("v", "net")          # known keys, menu order
    assert chosen_from_setting(["layer", "shown"]) == ("shown", "layer")  # read back as a list
    assert setting_from_chosen(["layer", "j"]) == "j,layer"
    assert setting_from_chosen(["layer", "shown"]) == "shown,layer"


def test_hover_choice_saved_up_to_0_3_1_is_migrated():
    """The old setting (View → Hover shows without the map's value) is read only while the
    new one was never written: its default becomes the new default, a choice of its own
    keeps its values under the map's value, and a readout turned off stays off."""
    from coppernicus.app.hover import HOVER_DEFAULT, chosen_from_setting
    assert chosen_from_setting(None, "v,drop,layer") == HOVER_DEFAULT
    assert chosen_from_setting(None, "j,net") == ("shown", "j", "net")
    assert chosen_from_setting(None, "v") == ("shown", "v")
    assert chosen_from_setting(None, "") == ()                           # it was off: it stays off
    assert chosen_from_setting(None, None) == HOVER_DEFAULT
    assert chosen_from_setting("layer", "j,net") == ("layer",)           # the new setting wins
    assert chosen_from_setting("", "j,net") == ()


# ---------------------------------------------------------------------- the map's value, large

def test_hover_leads_with_the_value_of_the_map_shown():
    """The label's large number is the quantity of the Show selector, at the double-click
    readout's precision; the plain text leads with it in the readout's words."""
    from coppernicus.app.hover import hover_readout, hover_values
    values = hover_values(_rail(3.3, net="+3V3", nets=["+3V3"]), "F.Cu", _probe(3.29274, j=2.6512))
    chosen = ("shown", "layer")
    drop = hover_readout(values, chosen, "drop")
    assert (drop.lead, drop.secondary, drop.caption, drop.lines) == (
        "7.260 mV", "0.22 %", "drop below 3.3 V", ("F.Cu",))              # % of 3.3 V, as the loads
    assert drop.text() == "drop = 7.260 mV below 3.3 V\nF.Cu"
    assert drop.label_lines() == ("drop below 3.3 V", "F.Cu")
    volt = hover_readout(values, chosen, "voltage")
    assert (volt.lead, volt.secondary, volt.caption) == ("3.29274 V", "", "")
    assert volt.text() == "V = 3.29274 V\nF.Cu"
    cur = hover_readout(values, chosen, "current")
    assert (cur.lead, cur.secondary, cur.caption) == ("2.65 A/mm²", "", "")
    assert cur.text() == "|J| = 2.65 A/mm²\nF.Cu"


def test_hover_label_shows_the_extras_muted_without_their_equals_sign():
    from coppernicus.app.hover import hover_readout, hover_values
    values = hover_values(_rail(3.3, net="+3V3", nets=["+3V3"]), "F.Cu", _probe(3.29274, j=2.6512))
    r = hover_readout(values, ("shown", "v", "j", "layer"), "drop")
    assert r.label_lines() == ("drop below 3.3 V", "V 3.29274 V", "|J| 2.65 A/mm²", "F.Cu")
    assert r.text() == "drop = 7.260 mV below 3.3 V\nV = 3.29274 V\n|J| = 2.65 A/mm²\nF.Cu"
    island = hover_values(_rail(3.3), "B.Cu", _probe(float("nan"), j=0.0))
    assert hover_readout(island, ("shown", "layer"), "drop").label_lines() == ("not connected", "B.Cu")


def test_hover_on_the_difference_map_leads_with_the_change_of_drop():
    from coppernicus.app.hover import hover_readout, hover_values
    rail = _rail(3.3)
    more = hover_readout(hover_values(rail, "F.Cu", _probe(3.29), delta_mv=1.23456), ("shown",), "difference")
    assert (more.lead, more.caption) == ("+1.235 mV", "Δ drop vs baseline")
    assert more.text() == "Δ drop vs baseline = +1.235 mV"
    less = hover_readout(hover_values(rail, "F.Cu", _probe(3.29), delta_mv=-0.5), ("shown",), "difference")
    assert less.lead == "-0.500 mV"
    none = hover_readout(hover_values(rail, "F.Cu", _probe(3.29)), ("shown", "drop"), "difference")
    assert none.lead == "" and none.text() == ("Δ drop vs baseline: no baseline copper here\n"
                                               "drop = 10.000 mV below 3.3 V")


def test_hover_lead_on_a_temperature_map_is_the_cells_temperature():
    from coppernicus.app.hover import temperature_readout
    t = temperature_readout(41.83, 0.24, "F.Cu", ("shown", "layer"))
    assert (t.lead, t.caption, t.lines) == ("41.8 °C", "", ("F.Cu",))
    assert t.text() == "T = 41.8 °C (0.24 mm cell)\nF.Cu"               # the double-click's words
    assert temperature_readout(41.83, 0.24, "F.Cu", ("shown",)).text() == "T = 41.8 °C (0.24 mm cell)"
    plain = temperature_readout(41.83, 0.24, "F.Cu", ("layer",))       # the map's value unticked
    assert plain.lead == "" and plain.text() == "T = 41.8 °C (0.24 mm cell)\nF.Cu"
    assert not temperature_readout(float("nan"), 0.24, "F.Cu", ("shown",))     # outside the board


def test_hover_lead_keeps_the_drop_word_and_reference_of_each_sign():
    from coppernicus.app.hover import hover_readout, hover_values

    def lead(result, v):
        r = hover_readout(hover_values(result, "F.Cu", _probe(v)), ("shown",), "drop")
        return r.lead, r.secondary, r.caption
    assert lead(_rail(-3.3, drop_sign=-1.0), -3.29) == (
        "10.000 mV", "0.30 %", "drop above -3.3 V (toward 0 V)")
    assert lead(_rail(0.0, net="GND", nets=["GND"], kind="ground"), 0.002) == (     # no % of 0 V
        "2.000 mV", "", "rise above 0 V (ground rise)")
    shifted = _rail(0.0, net="GND", nets=["GND"], kind="ground", max_drop=-0.005)
    assert lead(shifted, -0.002) == ("2.000 mV", "", "shift below 0 V (ground shift)")
    assert lead(_rail(3.3), 3.301) == ("-1.000 mV", "-0.03 %", "drop below 3.3 V")   # a boost reads negative


def test_hover_extras_follow_the_menu_and_never_repeat_the_lead():
    from coppernicus.app.hover import hover_readout, hover_values
    values = hover_values(_rail(3.3, net="+3V3", nets=["+3V3"]), "F.Cu", _probe(3.29, j=2.5))
    every = ("shown", "v", "drop", "j", "layer", "net")
    assert hover_readout(values, every, "drop").lines == (
        "V = 3.29000 V", "|J| = 2.50 A/mm²", "F.Cu · +3V3")
    assert hover_readout(values, every, "voltage").lines == (
        "drop = 10.000 mV below 3.3 V", "|J| = 2.50 A/mm²", "F.Cu · +3V3")
    assert hover_readout(values, every, "current").lines == (
        "V = 3.29000 V", "drop = 10.000 mV below 3.3 V", "F.Cu · +3V3")
    assert hover_readout(values, ("shown",), "current").lines == ()      # the number alone
    assert hover_readout(values, ("shown", "net"), "voltage").text() == "V = 3.29000 V\n+3V3"
    # The map's value unticked: the extras as before, its own value among them.
    assert hover_readout(values, ("v", "drop", "layer"), "voltage").text() == (
        "V = 3.29000 V\ndrop = 10.000 mV below 3.3 V\nF.Cu")
    assert hover_readout(values, ("v", "drop", "layer"), "voltage").lead == ""


def test_hover_with_the_lead_is_brief_off_or_off_the_source():
    """Off the copper nothing shows, as before; on copper joined to no source the label says
    so, with the layer or net if chosen, and no number."""
    from coppernicus.app.hover import hover_readout, hover_values
    island = hover_values(_rail(3.3), "B.Cu", _probe(float("nan"), j=0.0))
    for field in ("drop", "voltage", "current", "difference"):
        assert hover_readout(island, ("shown", "layer"), field).text() == "not connected\nB.Cu"
        assert hover_readout(island, ("shown",), field).lead == ""
        assert not hover_readout(None, ("shown", "layer"), field)
        assert hover_readout(island, (), field).text() == ""
