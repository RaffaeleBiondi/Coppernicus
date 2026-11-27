"""The app's mesher: the Mesher box (the default first, whatever it is; "Triangle (download…)"
while only the stand-in takes Triangle's place; a project's settings.mesher shown and locked),
every solve's worker meshing inside meshing.session.using, a mesher error shown as its
message and hint, and the mesher named in the status bar and the report."""

import os

import pytest

pytest.importorskip("PySide6")

from test_app_window import run_in_window  # noqa: E402

from coppernicus.app import mesher as app_mesher  # noqa: E402
from coppernicus.engine.meshing import registry  # noqa: E402
from coppernicus.engine.meshing import session as meshing  # noqa: E402
from coppernicus.project import Project  # noqa: E402
from coppernicus.triangle_standin import TriangleNotInstalled  # noqa: E402

DEMO = os.path.join(os.path.dirname(__file__), "..", "examples", "demo", "demo.coppernicus.yml")


@pytest.fixture
def only_triangle(monkeypatch):
    monkeypatch.setattr(registry, "available", lambda: ["triangle"])
    monkeypatch.setattr(app_mesher, "triangle_missing", lambda: False)


def test_the_entries_put_the_default_first_and_say_why_one_cannot_run(only_triangle, monkeypatch):
    names = [e[0] for e in app_mesher.entries()]
    assert names[0] == meshing.DEFAULT and set(names) >= set(registry.KNOWN)
    entries = {e[0]: e for e in app_mesher.entries()}
    if meshing.DEFAULT == "triangle":
        assert entries["triangle"][1:3] == ("Triangle — default", True)
    assert entries["rust"][2] is False and "coppermesh" in entries["rust"][3]
    monkeypatch.setattr(meshing, "DEFAULT", "rust")                  # whatever the default becomes
    monkeypatch.setattr(registry, "available", lambda: ["rust", "triangle"])
    assert [e[0] for e in app_mesher.entries()][0] == "rust"
    assert app_mesher.entries()[0][1] == "Rust — default"            # the default is not experimental


def test_triangle_offers_its_download_while_only_the_stand_in_is_here(only_triangle, monkeypatch):
    monkeypatch.setattr(app_mesher, "triangle_missing", lambda: True)
    entries = {e[0]: e for e in app_mesher.entries()}
    assert entries["triangle"][1] == "Triangle (download…)" and entries["triangle"][2] is True


def test_the_project_wins_over_the_app_and_the_app_over_the_default():
    project = Project()
    app_mesher.save_choice(None)
    assert app_mesher.effective(project) == meshing.DEFAULT
    app_mesher.save_choice("rust")
    assert app_mesher.effective(project) == "rust"
    project.settings.mesher = "triangle"
    assert app_mesher.effective(project) == "triangle"
    app_mesher.save_choice(None)


def test_a_mesher_error_shows_its_message_and_hint():
    e = TriangleNotInstalled()
    assert app_mesher.failure(e) == f"{e.message}\n{e.hint}"
    u = meshing.MesherUnavailable("no mesher named 'x'", "the meshers are rust, triangle")
    assert app_mesher.failure(u) == "no mesher named 'x'\nthe meshers are rust, triangle"
    assert app_mesher.failure(ValueError("other")) is None


def test_every_worker_meshes_inside_the_session_and_reports_a_mesher_error(monkeypatch):
    """A mesher that cannot run stops the rail, tree, impedance and noise workers (and their
    estimates) before anything is solved, with the error's message and hint, not "Unexpected
    error"; Triangle missing behind KiPI does the same."""
    from coppernicus.app import worker as app_worker
    from coppernicus.app.ac_panel import AcWorker
    from coppernicus.app.noise_panel import NoiseWorker
    workers = [app_worker.SolveWorker(None, None, None), app_worker.TreeWorker(None, None),
               AcWorker(None, None, "+3V3", "size"), AcWorker(None, None, "+3V3", "solve"),
               NoiseWorker(None, None, "U1", "size"), NoiseWorker(None, None, "U1", "solve")]
    for w in workers:
        heard = []
        w.failed.connect(heard.append)
        w.mesher = "no-such-mesher"
        w.run()
        assert len(heard) == 1 and heard[0].startswith("no mesher named 'no-such-mesher'\n"), (w, heard)

    def missing(*_a, **_k):
        raise TriangleNotInstalled()

    monkeypatch.setattr(app_worker, "solve_rail", missing)
    w = app_worker.SolveWorker(None, None, None)
    heard = []
    w.failed.connect(heard.append)
    w.run()
    assert heard == [f"{TriangleNotInstalled().message}\n{TriangleNotInstalled().hint}"]


def test_the_box_sits_atop_the_setup_shows_a_project_mesher_and_the_solve_names_its_mesher():
    run_in_window(f'''
        import coppernicus.app.main_window as mw
        from coppernicus.app import mesher as app_mesher
        from coppernicus.engine.meshing import session as meshing

        class HeldWorker(mw.SolveWorker):
            def start(self):
                pass

        mw.SolveWorker = HeldWorker
        box = window.mesher_box
        assert window.setup_dock.header is box and box.isEnabled()
        assert box.current() == meshing.DEFAULT and box.box.currentData() == meshing.DEFAULT
        window.open_project({DEMO!r})
        window.project.settings.mesher = "triangle"            # as a project file would say it
        window._remerge()
        assert not box.box.isEnabled() and box.current() == "triangle"
        assert "settings.mesher: triangle" in box.box.toolTip()
        window.project.settings.mesher = None
        window._remerge()
        assert box.box.isEnabled()
        window.nets.select("+3V3")
        app.processEvents()
        window.solve()
        assert window.worker.mesher == window.mesher_name()
        window.worker.run()
        app.processEvents()
        msg = window.statusBar().currentMessage()
        assert f"meshed with {{app_mesher.shown(window.mesher_name())}}" in msg, msg
        assert window.solved_mesher["+3V3"] == window.mesher_name()
    ''')
