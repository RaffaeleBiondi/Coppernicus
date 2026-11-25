"""The AC PDN impedance in the agent and CI interfaces: the MCP solve_ac (the
<project>_ac_summary.json document, with the |Z|(f) CSV and PNG paths and a size and memory
estimate first, refused over this machine's memory unless confirmed), the CLI's estimate line
and its HTML page, and the CI's opt-in ac run with its exit codes. The synthetic decoupling
board with a quick sweep."""

import json
from pathlib import Path

import pytest

from coppernicus import ci
from coppernicus.cli import EXIT_LIMIT, main
from coppernicus.engine.synthetic import ac_decap_board, ac_decap_project, save_snapshot
from coppernicus.project import AcOptions
from coppernicus.service import ServiceError, Session

QUICK = dict(f_start_hz=1e5, f_stop_hz=1e8, points_per_decade=20, field_points_per_decade=4,
             max_edge_mm=2.0)


@pytest.fixture()
def decap(tmp_path):
    save_snapshot(ac_decap_board(), tmp_path / "ac_decap.kipi.json.gz")
    path = tmp_path / "d.coppernicus.yml"

    def write(target_ohm=1.0):
        p = ac_decap_project()
        p.ac = AcOptions(**QUICK, bandwidth_hz={"U1": 1e5})
        p.probes[0].target_ohm = target_ohm
        return p.save(path)
    return write


def test_solve_ac_returns_the_summary_with_its_files_and_the_estimate(decap, tmp_path):
    path = decap()
    doc = Session(out_dir=tmp_path / "out").solve_ac(path)
    assert doc["kind"] == "ac_impedance" and doc["verdict"] == "pass"
    [rail] = doc["rails"]
    assert Path(rail["csv_file"]).is_file() and Path(rail["png_file"]).is_file()
    assert Path(doc["summary_file"]).is_file()
    assert (tmp_path / "out").resolve() in Path(doc["summary_file"]).resolve().parents
    est = rail["estimate"]
    assert est["unknowns"] > 0 and est["triangles"] > 0 and est["ports"] >= 1
    assert est["exceeds_memory"] is False and "unknowns" in est["text"]
    for key in ("bandwidth_assumed", "assumed_bandwidth_hz", "probes", "warnings"):
        assert key in rail or key in doc, key


def test_an_ac_estimate_over_the_machines_memory_needs_a_confirmation(decap, tmp_path, monkeypatch):
    from coppernicus.engine import ac
    path = decap()
    monkeypatch.setattr(ac, "total_memory_bytes", lambda: 1_000_000)
    session = Session(out_dir=tmp_path / "out")
    with pytest.raises(ServiceError) as e:
        session.solve_ac(path)
    assert e.value.code == "solve_too_large" and "confirm_large" in e.value.hint
    assert "max_edge_mm" in e.value.hint and "unknowns" in e.value.message
    assert not list((tmp_path / "out").rglob("*_impedance.csv"))        # nothing solved
    doc = session.solve_ac(path, confirm_large=True)
    assert doc["rails"][0]["estimate"]["exceeds_memory"] is True


def test_the_mcp_solve_ac_tool(decap, tmp_path):
    import anyio
    from mcp import Client

    from coppernicus.mcp_server import build_server
    path = decap()

    async def scenario():
        async with Client(build_server(Session(out_dir=tmp_path / "out")), raise_exceptions=True) as c:
            res = await c.call_tool("solve_ac", {"project_path": str(path)})
            names = {t.name for t in (await c.list_tools()).tools}
            return res.structured_content, names
    doc, names = anyio.run(scenario)
    assert "solve_ac" in names and doc["verdict"] == "pass" and doc["rails"][0]["estimate"]["unknowns"] > 0


def test_the_cli_prints_the_estimate_and_writes_an_html_page(decap, tmp_path, capsys):
    import base64
    path = decap()
    page = tmp_path / "ac.html"
    main(["ac", str(path), "--out", str(tmp_path / "o"), "--html", str(page)])
    out = capsys.readouterr().out
    head = out[:out.index("Rail +3V3")]
    assert "Impedance of +3V3: " in head and "unknowns" in head
    text = page.read_text(encoding="utf-8")
    assert "data:image/png;base64," in text and "U1" in text and "https://" not in text
    png = text.split("data:image/png;base64,", 1)[1].split('"', 1)[0]
    assert base64.b64decode(png)[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize("target, code, verdict", [(1.0, 0, "pass"), (0.001, EXIT_LIMIT, "fail")])
def test_ci_runs_ac_when_asked_and_its_verdict_sets_the_code(decap, tmp_path, monkeypatch,
                                                              target, code, verdict):
    path = decap(target_ohm=target)
    monkeypatch.chdir(tmp_path)
    index = ci.solve_all([path.name], Path("ci"), html=False, ac=True)
    [entry] = index["projects"]
    assert entry["ac"]["verdict"] == verdict
    assert index["exit_code"] == code
    md = ci.summary_markdown(index)
    assert "### Impedance" in md and "| U1 |" in md
    plain = ci.solve_all([path.name], Path("ci2"), html=False)
    assert "ac" not in plain["projects"][0]                         # opt-in, off by default


def test_the_ac_summary_document_has_the_memory_keys(decap, tmp_path):
    doc = Session(out_dir=tmp_path / "out").solve_ac(decap())
    stats = doc["rails"][0]["stats"]
    assert "memory_peak_mb" in stats and "memory_peak_raised" in stats
    json.dumps(doc, allow_nan=False)
