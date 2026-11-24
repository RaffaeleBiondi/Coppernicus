"""compare_summaries (module level) is exactly what Session.compare_results returns."""

import pytest

from coppernicus.engine.synthetic import OZ1, Strip, save_snapshot, strip_board
from coppernicus.service import Session
from coppernicus.service.session import compare_summaries


@pytest.fixture()
def results(tmp_path):
    path = tmp_path / "strip.kipi.json.gz"
    save_snapshot(strip_board(), path)
    session = Session(out_dir=tmp_path / "out")
    bid = session.open_board(path)["board_id"]

    def solve(current, **kw):
        return session.solve_rail(bid, Strip.NET, [{"ref": "J1", "voltage_v": 1.0}],
                                  [{"ref": "U1", "current_a": current}], **kw)
    a = solve(1.0)
    b = solve(2.0, settings={"copper_thickness_mm": {"F.Cu": 2 * OZ1}})
    return session, a, b


def test_compare_summaries_equals_session_compare_results(results):
    session, a, b = results
    assert compare_summaries(a, b) == session.compare_results(a["result_id"], b["result_id"])
    assert compare_summaries(b, a) == session.compare_results(b["result_id"], a["result_id"])
    cmp_ = compare_summaries(a, b)
    assert cmp_["loads"][0]["delta_drop_mv"] == pytest.approx(0.0, abs=1e-6)   # 2 A on 2 oz = 1 A on 1 oz
    assert "copper_thickness_mm" in cmp_["settings_changed"]
