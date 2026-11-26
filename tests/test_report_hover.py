"""The HTML report's cursor label, as the app's hover label: next to the pointer, line 1 the value
of the map shown in large type (the drop with its % of the source voltage, V, |J|) at the app's
precision, then small and muted the drop's side and the layer; flipped at the map's edges,
hidden off copper and when the pointer leaves the map. The label's logic is a block of pure
functions in report.js, run here in node against the app's own (app/hover.py)."""

import json
import math
import re
import shutil
import subprocess
from importlib.resources import files

import pytest

from coppernicus.app.hover import HoverValues, hover_readout, label_position

JS = files("coppernicus").joinpath("report_assets", "report.js").read_text(encoding="utf-8")
CSS = files("coppernicus").joinpath("report_assets", "report.css").read_text(encoding="utf-8")
START, END = "// pure (no DOM", "// end pure"

RAIL = {"v_ref_v": 3.3, "drop_sign": 1, "drop_unit": "mV below 3.3 V", "drop_word": "drop"}
GROUND = {"v_ref_v": 0.0, "drop_sign": -1, "drop_unit": "mV above 0 V (ground rise)", "drop_word": "rise"}
SHIFT = {"v_ref_v": 0.0, "drop_sign": 1, "drop_unit": "mV below 0 V (ground shift)", "drop_word": "shift"}
OLD = {"v_ref_v": 5.0}                     # a report written before drop_unit and drop_word
ABOVE = {"v_ref_v": 1.83847, "drop_sign": 1, "drop_unit": "mV below 1.83847 V", "drop_word": "drop"}
NEGATIVE = {"v_ref_v": -3.3, "drop_sign": -1, "drop_unit": "mV above -3.3 V (toward 0 V)",
            "drop_word": "drop"}
# A tree group shown from another 0 V (S2): v_ref_v moved with the voltages, v_offset_v brings
# it back to the solve's frame; no drop_unit, so the label writes it from the source voltage.
REFERENCED = {"v_ref_v": 2.3, "v_offset_v": 1.0, "drop_sign": 1, "drop_word": "drop"}
HERE = {"V": 3.3 - 0.0072613, "J": 2.6512}
CASES = [
    ["drop", HERE, RAIL, "B.Cu"],
    ["voltage", HERE, RAIL, "B.Cu"],
    ["current", HERE, RAIL, "B.Cu"],
    ["drop", {"V": 0.0384, "J": 1.0}, GROUND, "F.Cu"],
    ["drop", {"V": -0.0021, "J": 1.0}, SHIFT, "F.Cu"],
    ["drop", {"V": 4.99, "J": 1.0}, OLD, "In1.Cu"],
    ["drop", {"V": None, "J": 0.0}, RAIL, "F.Cu"],          # copper with no path to a source
    ["current", None, RAIL, "F.Cu"],                         # off copper
    # More points for the parity with the app: a rail above its set point (a negative drop),
    # a negative rail, a tiny |J|, a large one.
    ["drop", {"V": 1.84447123, "J": 28.5714}, ABOVE, "F.Cu"],
    ["drop", {"V": -3.2871234, "J": 0.4}, NEGATIVE, "B.Cu"],
    ["voltage", {"V": 0.001898496, "J": 11.0}, GROUND, "B.Cu"],
    ["current", {"V": 3.29, "J": 0.0031}, RAIL, "In2.Cu"],
    ["current", {"V": 3.29, "J": 187.65432}, RAIL, "In2.Cu"],
    ["drop", {"V": 2.3 - 0.0072613, "J": 1.0}, REFERENCED, "B.Cu"],
]
POSITIONS = [[[100, 100], [80, 40], [400, 300]], [[390, 100], [80, 40], [400, 300]],
             [[100, 290], [80, 40], [400, 300]], [[395, 295], [80, 40], [400, 300]],
             [[10, 10], [500, 400], [400, 300]], [[100.7, 50.2], [80.5, 40.5], [400, 300]]]

NODE = r"""
const fs = require("fs");
const [, , script, cases, positions] = process.argv;
eval(fs.readFileSync(script, "utf8"));
const nan = (at) => at && { ...at, V: at.V === null ? NaN : at.V };
console.log(JSON.stringify({
  labels: JSON.parse(cases).map(([f, at, rail, layer]) => hoverLabel(f, nan(at), rail, layer)),
  positions: JSON.parse(positions).map(([c, s, v]) => labelPosition(c, s, v)),
}));
"""


def pure_block() -> str:
    assert JS.count(START) == 1 and JS.count(END) == 1
    block = JS[JS.index(START):JS.index(END)]
    assert "document" not in block and "M." not in block and "$(" not in block      # no DOM, no page
    return block


def readout_code() -> str:
    """report.js's readout section: the label's box and readout()."""
    return JS[JS.index("// ---------------------------------------------------------------- readout"):
              JS.index("// ---------------------------------------------------------------- colour bar")]


@pytest.fixture(scope="module")
def node_out(tmp_path_factory):
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    tmp = tmp_path_factory.mktemp("hover")
    script, stub = tmp / "pure.js", tmp / "run.js"
    script.write_text(pure_block(), encoding="utf-8")
    stub.write_text(NODE, encoding="utf-8")
    run = subprocess.run(["node", str(stub), str(script), json.dumps(CASES), json.dumps(POSITIONS)],
                         capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_the_label_leads_with_the_value_of_the_map_shown(node_out):
    drop, volt, cur, ground, shift, old, grey, off = node_out["labels"][:8]
    assert drop == {"lead": "7.261 mV", "secondary": "0.22 %", "lines": ["drop below 3.3 V", "B.Cu"]}
    assert volt == {"lead": "3.29274 V", "secondary": "", "lines": ["B.Cu"]}
    assert cur == {"lead": "2.65 A/mm²", "secondary": "", "lines": ["B.Cu"]}
    # A ground (a tree page): a rise, no % of a 0 V reference; under a negative rail, a shift.
    assert ground == {"lead": "38.400 mV", "secondary": "", "lines": ["rise above 0 V (ground rise)", "F.Cu"]}
    assert shift == {"lead": "2.100 mV", "secondary": "", "lines": ["shift below 0 V (ground shift)", "F.Cu"]}
    assert old == {"lead": "10.000 mV", "secondary": "0.20 %", "lines": ["drop below 5 V", "In1.Cu"]}
    assert grey == {"lead": "", "secondary": "", "lines": ["not connected", "F.Cu"]}    # as the app
    assert off is None                                                                  # hidden


def test_the_label_takes_the_drop_percent_from_the_solves_frame_under_a_reference(node_out):
    """A tree page shown from another 0 V: the drop and its % and the source voltage named are
    the solve's, whatever the offset (the voltage map's frame moves, the drop does not)."""
    assert node_out["labels"][-1] == {"lead": "7.261 mV", "secondary": "0.22 %",
                                      "lines": ["drop below 3.3 V", "B.Cu"]}


def test_the_label_reads_as_the_apps(node_out):
    """Every case against app/hover.py's hover_readout with the map's value and the layer (the
    app's default): the same lead, the same small figure, the same lines, to the character."""
    for (field, at, rail, layer), got in zip(CASES, node_out["labels"]):
        values = None
        if at is not None:
            v_ref, volts = rail["v_ref_v"], math.nan if at["V"] is None else at["V"]
            nominal = v_ref + rail.get("v_offset_v", 0.0)          # the solve's frame (S2)
            drop_mv = (-1 if rail.get("drop_sign") == -1 else 1) * (v_ref - volts) * 1e3
            values = HoverValues(volts, drop_mv, rail.get("drop_word", "drop"),
                                 rail.get("drop_unit", f"mV below {nominal:g} V"), at["J"], layer, "NET",
                                 drop_pct=drop_mv / (10.0 * abs(nominal)) if nominal else math.nan)
        app = hover_readout(values, ("shown", "layer"), field)
        want = {"lead": app.lead, "secondary": app.secondary, "lines": list(app.label_lines())}
        assert got == (want if app else None), (field, at, rail)


def test_the_label_sits_beside_the_pointer_as_the_apps_does(node_out):
    """16 px right and 12 px down, flipped at the map's edges, clamped inside: app/hover.py's
    label_position, to the pixel."""
    want = [list(label_position(c, s, v)) for c, s, v in POSITIONS]
    assert node_out["positions"] == want
    assert want[0] == [116, 112] and want[1] == [294, 112] and want[2] == [116, 238]


def test_the_page_shows_the_label_in_the_map_and_hides_it_off_the_map():
    code = readout_code()
    hover = code[code.index("async function hover(ev)"):code.index("async function readout(ev)")]
    # readout() writes the line and hands back the point (nothing off the grid or the copper:
    # the label hides); the newest pointer event wins, so a late decode never re-shows it.
    assert "p = await readout(ev)" in hover and "if (seq === hoverSeq)" in hover
    assert "showHover(ev, p ? hoverLabel(S.field, p, p.rail, p.layer) : null)" in hover
    assert "return { V: cv === 1 ? NaN : rail.v_lo_v + (cv - 2) * vstep, J, rail, layer: lay.name }" in code
    assert re.search(r'addEventListener\("pointermove",[^;]*;\n[^\n]*else hover\(e\)', JS)
    assert re.search(r'addEventListener\("pointerleave",[^\n]*hoverSeq\+\+; hideHover\(\)', JS)
    assert re.search(r'addEventListener\("pointerdown",[^\n]*hideHover\(\)', JS)       # a pan hides it
    assert "canvas.parentElement.append(hoverBox)" in JS          # inside the map's box
    assert "if (S.hover && !S.drag) hover(S.hover)" in JS         # zoom, layer, field, rail: read again


def test_the_label_takes_the_themes_colours():
    rules = {m.group(1).strip(): m.group(2) for m in re.finditer(r"(\.hoverlabel[^{]*)\{([^}]*)\}", CSS)}
    box = rules[".hoverlabel"]
    for token in ("var(--panel)", "var(--line)", "var(--fg)", "pointer-events:none", "position:absolute"):
        assert token in box, token
    assert "display" not in box                                   # [hidden] keeps hiding it
    assert "font-size:17px" in rules[".hoverlabel .lead"] and "font-weight:600" in rules[".hoverlabel .lead"]
    assert "var(--muted)" in rules[".hoverlabel .sec, .hoverlabel .rest"]
    assert not re.findall(r"#[0-9a-fA-F]{3,6}\b", "".join(rules.values()))       # no fixed colour
