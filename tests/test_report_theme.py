"""The theme of every HTML page Coppernicus writes: dark by default (data-theme on <html>), a
button in the header that switches to light and back, the choice kept in localStorage where the
browser allows it; the plots follow the page's theme (in dark their light PNG is shown inverted,
hues turned back; maps keep their colour scales), and the PNGs written to
--out are the same bytes whether a page is written or not."""

import base64
import json
import re
import shutil
import subprocess
from importlib.resources import files
from pathlib import Path

import pytest
from matplotlib.figure import Figure

from coppernicus.cli import main
from coppernicus.engine import Board, solve_rail
from coppernicus.engine.synthetic import demo_board, power_chain_board, power_chain_project
from coppernicus.engine.tree import solve_project_tree
from coppernicus.power_tree import tree_summary
from coppernicus.project import Limits, Load, Rail, Source
from coppernicus.report import (
    AC_PAGE_CSS,
    NOISE_PAGE_CSS,
    TOLERANCE_PAGE_CSS,
    RailReport,
    write_ac_report,
    write_noise_report,
    write_report,
    write_tolerance_report,
    write_tree_report,
)

DEMO = Path(__file__).resolve().parents[1] / "examples" / "demo"
THEME_JS = files("coppernicus").joinpath("report_assets", "theme.js").read_text(encoding="utf-8")
REPORT_CSS = files("coppernicus").joinpath("report_assets", "report.css").read_text(encoding="utf-8")


def assert_themed(text: str) -> None:
    """Dark by default, the theme script in the head, the toggle once, in the header."""
    head = text[:text.index("</head>")]
    assert '<html lang="en" data-theme="dark">' in text
    assert "prefers-color-scheme" not in text                 # the reader's choice, not the OS's
    assert "localStorage" in head and '"theme-toggle"' in head       # theme.js, before the body
    assert text.count('id="theme-toggle"') == 1
    header = re.search(r"<header>(.*?)</header>", text, re.S).group(1)
    button = re.search(r'<button id="theme-toggle" type="button"[^>]*>([^<]*)</button>', header)
    assert button, header
    assert button.group(1) == "☀ Light" and 'aria-label="Switch to the light theme"' in button.group(0)
    assert "http://" not in text and "https://" not in text          # offline


def png(path: Path) -> Path:
    fig = Figure(figsize=(2, 1.5), dpi=50)
    fig.add_subplot().plot([1, 2, 3], [3, 1, 2])
    fig.savefig(path)
    return path


def embedded(text: str):
    return [base64.b64decode(m) for m in re.findall(r'src="data:image/png;base64,([^"]+)"', text)]


# ---------------------------------------------------------------------- every page kind

def test_the_rail_report_opens_dark_with_the_toggle(tmp_path):
    board = Board(demo_board())
    rail = Rail("+3V3", sources=[Source("J1", 3.3)], loads=[Load("U1", 2.0), Load("U2", 1.5)],
                limits=Limits(max_drop_pct=0.25))
    path = write_report([RailReport(solve_rail(board, rail))], None, tmp_path / "rail.html",
                        outline=board.outline())
    text = path.read_text(encoding="utf-8")
    assert_themed(text)
    assert not re.search(r'\b(src|href)\s*=', text)


def test_the_power_tree_report_opens_dark_with_the_toggle(tmp_path):
    tree = solve_project_tree(Board(power_chain_board()), power_chain_project())
    path = write_tree_report(tree, tree_summary(tree), tmp_path / "tree.html")
    assert_themed(path.read_text(encoding="utf-8"))


def test_the_thermal_page_opens_dark_and_leaves_the_maps_written_to_out_unchanged(tmp_path, capsys):
    for name in ("demo.kipi.json.gz", "demo-thermal.coppernicus.yml"):
        shutil.copy(DEMO / name, tmp_path / name)
    project = str(tmp_path / "demo-thermal.coppernicus.yml")
    main(["solve", project, "--thermal", "--html", "--out", str(tmp_path / "with")])
    main(["solve", project, "--thermal", "--out", str(tmp_path / "without")])
    capsys.readouterr()
    [page] = (tmp_path / "with").glob("*.html")
    text = page.read_text(encoding="utf-8")
    assert_themed(text)
    assert '"maps":[{"layer"' in text and ".thermalmaps img" in text
    maps = {p.name: p.read_bytes() for p in (tmp_path / "with").glob("*.png")}
    assert len(maps) == 2
    assert maps == {p.name: p.read_bytes() for p in (tmp_path / "without").glob("*.png")}


def test_the_ac_page_opens_dark_and_embeds_the_plot_byte_for_byte(tmp_path):
    plot = png(tmp_path / "x_P3V3_impedance.png")
    before = plot.read_bytes()
    doc = {"verdict": "pass", "rails": [{"rail": "+3V3", "ground": "GND", "source": "U1",
                                         "png_file": str(plot), "probes": []}]}
    text = write_ac_report(doc, tmp_path / "ac.html", project="p.coppernicus.yml").read_text(encoding="utf-8")
    assert_themed(text)
    assert plot.read_bytes() == before and embedded(text) == [before]


def test_the_noise_page_opens_dark_and_embeds_the_plots_byte_for_byte(tmp_path):
    plots = [png(tmp_path / f"x_U1_noise{s}.png") for s in ("", "_h_field", "_e_field")]
    before = [p.read_bytes() for p in plots]
    doc = {"verdict": "pass", "aggressors": [{"aggressor": "U1", "png_files": [str(p) for p in plots]}]}
    text = write_noise_report(doc, tmp_path / "noise.html").read_text(encoding="utf-8")
    assert_themed(text)
    assert [p.read_bytes() for p in plots] == before and embedded(text) == before


def test_the_tolerance_page_opens_dark_with_the_toggle(tmp_path):
    text = write_tolerance_report({"verdict": "not_judged"}, tmp_path / "t.html").read_text(encoding="utf-8")
    assert_themed(text)


# ---------------------------------------------------------------------- the palettes

def palettes(css: str):
    """The variables of each theme: :root's (dark), then those data-theme="light" changes."""
    def block(selector):
        body = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css).group(1)
        return dict(re.findall(r"(--[\w-]+)\s*:\s*(#[0-9a-fA-F]{6})", body))
    dark = block(":root")
    return {"dark": dark, "light": {**dark, **block(':root[data-theme="light"]')}}


def contrast(a: str, b: str) -> float:
    def lum(h):
        c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("css", [REPORT_CSS, AC_PAGE_CSS], ids=["report.css", "AC_PAGE_CSS"])
def test_text_reads_in_both_themes(css):
    for theme, v in palettes(css).items():
        for fg in ("--fg", "--muted", "--fail", "--warn"):
            for bg in ("--bg", "--panel"):
                assert contrast(v[fg], v[bg]) >= 4.5, (theme, fg, bg)


def test_the_badges_keep_white_text_readable_in_both_themes():
    """report.css's note: --selected and the badges carry white text, the same in both themes."""
    dark, light = palettes(REPORT_CSS).values()
    for name in ("--selected", "--badge-fail", "--badge-pass", "--badge-attention"):
        assert dark[name] == light[name] and contrast("#ffffff", dark[name]) >= 4.5, name


def test_the_simple_pages_use_the_palette_not_fixed_colours():
    for css in (AC_PAGE_CSS, NOISE_PAGE_CSS, TOLERANCE_PAGE_CSS):
        rules = re.sub(r":root[^{]*\{[^}]*\}", "", css)
        colours = set(re.findall(r"#[0-9a-fA-F]{3,6}\b", rules))
        assert colours <= {"#fff"}, colours                 # only the plots' light panel


# ---------------------------------------------------------------------- the toggle's script

def test_the_script_switches_the_attribute_and_keeps_the_choice_where_it_can():
    assert 'setAttribute("data-theme"' in THEME_JS and "localStorage" in THEME_JS
    assert THEME_JS.count("try {") >= 2                      # storage blocked on file://: no error
    assert "<" not in THEME_JS                               # nothing the HTML parser acts on


NODE_STUB = r"""
const MODE = process.argv[2];
const attrs = { "data-theme": "dark" }, kept = {}, events = [], ready = [];
const root = { getAttribute: (k) => (k in attrs ? attrs[k] : null),
  setAttribute: (k, v) => { attrs[k] = String(v); } };
const button = { attrs: {}, textContent: "", click: null,
  setAttribute(k, v) { this.attrs[k] = String(v); },
  addEventListener(t, f) { if (t === "click") this.click = f; } };
globalThis.document = { documentElement: root, readyState: "loading",
  getElementById: (id) => (id === "theme-toggle" ? button : null),
  addEventListener: (t, f) => { if (t === "DOMContentLoaded") ready.push(f); },
  dispatchEvent: (e) => events.push(e.type) };
const storage = { getItem: (k) => (k in kept ? kept[k] : null), setItem: (k, v) => { kept[k] = String(v); } };
if (MODE === "kept-light") kept["coppernicus-theme"] = "light";
globalThis.window = { get localStorage() {
  if (MODE === "blocked") throw new Error("SecurityError");
  return storage; } };
const state = () => ({ theme: attrs["data-theme"], label: button.textContent,
  aria: button.attrs["aria-label"], kept: kept["coppernicus-theme"] || null, events: events.length });
const steps = [];
eval(require("fs").readFileSync(process.argv[3], "utf8"));
steps.push(state());                       // after the head ran, before the body is parsed
ready.forEach((f) => f());
steps.push(state());                       // the button wired
button.click(); steps.push(state());
button.click(); steps.push(state());
console.log(JSON.stringify(steps));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("mode", ["blocked", "fresh", "kept-light"])
def test_the_toggle_in_node(mode, tmp_path):
    stub, script = tmp_path / "stub.js", tmp_path / "theme.js"
    stub.write_text(NODE_STUB, encoding="utf-8")
    script.write_text(THEME_JS, encoding="utf-8")
    run = subprocess.run(["node", str(stub), mode, str(script)], capture_output=True, text=True,
                         encoding="utf-8", timeout=60)
    assert run.returncode == 0, run.stderr
    head, wired, once, twice = json.loads(run.stdout)
    light = {"label": "☾ Dark", "aria": "Switch to the dark theme"}
    dark = {"label": "☀ Light", "aria": "Switch to the light theme"}
    first = "light" if mode == "kept-light" else "dark"
    other = "dark" if first == "light" else "light"
    assert head["theme"] == first                            # a kept choice applies in the head
    assert {k: wired[k] for k in ("label", "aria")} == (light if first == "light" else dark)
    assert once["theme"] == other and once["events"] == 1 and twice["theme"] == first
    assert {k: once[k] for k in ("label", "aria")} == (light if other == "light" else dark)
    if mode == "blocked":                                   # nothing kept, and no error either
        assert once["kept"] is None and twice["kept"] is None
    else:
        assert once["kept"] == other and twice["kept"] == first


def test_a_plot_follows_the_pages_theme_and_a_map_keeps_its_colours(tmp_path):
    """The |Z| plot and the noise waveforms carry class "plot", shown inverted in dark with
    their hues turned back; the H and E field maps carry "map" and are never inverted. The
    PNGs embedded are the files' own bytes."""
    plots = [png(tmp_path / f"x_U1_noise{s}.png") for s in ("", "_h_field", "_e_field")]
    doc = {"verdict": "pass", "aggressors": [{"aggressor": "U1", "png_files": [str(p) for p in plots]}]}
    text = write_noise_report(doc, tmp_path / "noise.html").read_text(encoding="utf-8")
    assert re.findall(r'<img class="(\w+)"', text) == ["plot", "map", "map"]
    assert ':root[data-theme="dark"] img.plot { filter: invert(0.88) hue-rotate(180deg); }' in NOISE_PAGE_CSS
    assert "img.map" not in NOISE_PAGE_CSS.split("filter")[0].split("\n")[-1]
    ac = {"verdict": "pass", "rails": [{"rail": "+3V3", "png_file": str(plots[0]), "probes": []}]}
    text = write_ac_report(ac, tmp_path / "ac.html").read_text(encoding="utf-8")
    assert '<img class="plot"' in text and embedded(text) == [plots[0].read_bytes()]


def _css_filter(hex_colour: str, invert: float = 0.88, degrees: float = 180.0):
    """``filter: invert(invert) hue-rotate(degrees)`` on a colour, as the Filter Effects spec
    defines them (invert per channel, then the hue-rotate matrix), in 0..1 RGB."""
    import math
    rgb = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    rgb = [invert * (1 - c) + (1 - invert) * c for c in rgb]
    a = math.radians(degrees)
    cs, sn = math.cos(a), math.sin(a)
    m = [[0.213 + cs * 0.787 - sn * 0.213, 0.715 - cs * 0.715 - sn * 0.715, 0.072 - cs * 0.072 + sn * 0.928],
         [0.213 - cs * 0.213 + sn * 0.143, 0.715 + cs * 0.285 + sn * 0.140, 0.072 - cs * 0.072 - sn * 0.283],
         [0.213 - cs * 0.213 - sn * 0.787, 0.715 - cs * 0.715 + sn * 0.715, 0.072 + cs * 0.928 + sn * 0.072]]
    return [min(max(sum(m[r][k] * rgb[k] for k in range(3)), 0.0), 1.0) for r in range(3)]


def test_the_dark_filter_keeps_the_verdict_colours_recognisable():
    """In dark, a plot's red limit line stays red and a green PASS stays green (hue within
    25°), the white panel goes dark: the filter the pages use, computed as browsers do."""
    import colorsys
    for name, colour in (("fail red", "#c62828"), ("pass green", "#2e7d32"), ("plot blue", "#1f77b4")):
        before = colorsys.rgb_to_hsv(*[int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)])[0] * 360
        after = colorsys.rgb_to_hsv(*_css_filter(colour))[0] * 360
        assert min(abs(after - before), 360 - abs(after - before)) < 25, (name, before, after)
    assert max(_css_filter("#ffffff")) < 0.2                          # the panel goes dark
