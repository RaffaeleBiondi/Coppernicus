"""The Tolerance page (D3): one row per load with its verdict; under it the selected load's
stack-up chart and parts, a missing part named, never drawn as zero; the cards of its checks;
and, as `coppernicus tolerance` writes them, the AC mesh's assumption and warning and the
mesher's fallback warning when impedance or noise results are used."""

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from coppernicus.results import ToleranceCheck, TolerancePart, ToleranceRow  # noqa: E402


def row():
    return ToleranceRow(
        load="U2", name="U2", net="VOUT", rail="VOUT", nominal_v=1.8, source="U1", probe="",
        parts={"dc": TolerancePart(0.02705, 0.0, "power tree"),
               "accuracy": TolerancePart(0.018, 0.018, "accuracy_pct of U1"),
               "ripple": None, "transient": TolerancePart(0.03, 0.03, "entered")},
        hints={"ripple": "solve the noise of U1"},
        checks={"total": ToleranceCheck("total", 0.07505, 0.048, 0.054, 0.054, False, False,
                                        missing=["ripple"], limit="tol_pct")},
        verdict="fail")


def test_the_parts_table_names_a_missing_part():
    from coppernicus.app.tolerance_panel import stackup_rows
    rows = stackup_rows(row())
    assert rows[0] == ("DC drop", "27.05", "0", "power tree")
    assert rows[2] == ("ripple", "missing", "missing", "solve the noise of U1")


def test_the_chart_ends_each_bar_at_the_judged_sum_with_the_allowed_line():
    from matplotlib.figure import Figure

    from coppernicus.app.tolerance_panel import draw_stackup
    fig = Figure()
    draw_stackup(fig, row())
    ax = fig.axes[0]
    texts = [t.get_text().strip() for t in ax.texts]
    assert texts == ["75.05 mV", "48 mV"], texts                    # checks["total"], each side
    assert "missing: ripple" in ax.get_title(loc="left")
    labels = [t.get_text() for t in fig.legends[0].get_texts()]
    assert sorted(labels) == ["DC accuracy", "DC drop", "allowed", "transient"], labels      # no ripple bar


def test_the_cards_are_the_checks_and_only_a_failing_one_is_red():
    from coppernicus.app.verdict import tolerance_cards
    cards = tolerance_cards(row())
    assert cards[0].key == "Total, tol_pct" and cards[0].value == "75.05 / 48 mV" and cards[0].state == "fail"
    assert "parts missing" in cards[0].detail


def test_the_app_stack_up_carries_the_ac_mesh_and_the_fallback_as_the_cli_does(monkeypatch):
    import coppernicus.tolerance as tol
    from coppernicus import board_fields as BF
    from coppernicus.app.tolerance_panel import compose_held
    from coppernicus.engine.meshing import session as meshing
    from coppernicus.project import Project
    made = SimpleNamespace(assumptions=[], warnings=[], ac_mesh=None)
    monkeypatch.setattr(tol, "compose", lambda *a, **k: made)
    ac = {"+3V3": (SimpleNamespace(rail="+3V3", probe=lambda name: None), "now")}
    result = compose_held({}, Project(), BF.Merged(Project()), None, ac, {}, "now", "now",
                          mesher="triangle", fallback=meshing.FALLBACK_WARNING)
    assert result.ac_mesh and any(a.startswith("AC mesh: largest edge") for a in result.assumptions)
    assert result.warnings == [meshing.FALLBACK_WARNING]
    plain = SimpleNamespace(assumptions=[], warnings=[], ac_mesh=None)
    monkeypatch.setattr(tol, "compose", lambda *a, **k: plain)
    compose_held({}, Project(), BF.Merged(Project()), None, {}, {}, "now", "now")
    assert plain.assumptions == [] and plain.warnings == []       # nothing AC held: no AC mesh note
