"""The KiCad reader's "remove unused layers" trims (coppernicus.engine.fast_trim): our stand-in
for KiPI's two functions gives the same board to the last bit, on every KiCad board KiPI's own
reader tests use (tests/boards/kipi_features*.kicad_pcb and .kicad_pro: KiPI's features
boards, GPL-3.0 like this project, copied unchanged from KiPI 1.0.1's tests/data/kicad_pcb and
renamed), on the repository's example boards, and on KiPI's board with vias and pads set to
remove unused layers added all over its zone fills. This is the drift guard for a KiPI update:
the stand-in only installs on KiPI 1.0.1."""

import dataclasses
import math
import os
import pickle
import shutil
import subprocess
import sys
import uuid
import warnings
from pathlib import Path

import numpy as np
import pytest

import coppernicus.engine.fast_trim as ft

R = ft.reader                          # KiPI's kipi.sources.kicad_pcb.reader, as the engine imports it
ROOT = Path(__file__).resolve().parents[1]
DATA = Path(__file__).resolve().parent / "boards"
KIPI_BOARDS = [DATA / "kipi_features.kicad_pcb", DATA / "kipi_features_v9.kicad_pcb"]
EXAMPLES = (sorted((ROOT / "examples").glob("*/*.kicad_pcb"))
            + sorted((ROOT / "examples").glob("*/*.kipi.json.gz")))
NETS = ("GND", "+5V", "SIG", "VOUT")


@pytest.fixture(autouse=True)
def kipis_trims_restored():
    """Every test leaves the reader as it found it (installed by default, see coppernicus.engine)."""
    was = ft.installed()
    yield
    ft.uninstall()
    if was:
        ft.install()


def same(a, b, path="snapshot") -> None:
    """Deep equality, floats to the bit (NaN equal to NaN): raises naming the first difference."""
    if dataclasses.is_dataclass(a) and not isinstance(a, type):
        assert type(a) is type(b), f"{path}: {type(a)} != {type(b)}"
        for f in dataclasses.fields(a):
            same(getattr(a, f.name), getattr(b, f.name), f"{path}.{f.name}")
        return
    if isinstance(a, dict):
        assert isinstance(b, dict) and list(a) == list(b), f"{path}: keys {list(a)} != {list(b)}"
        for k in a:
            same(a[k], b[k], f"{path}[{k!r}]")
        return
    if isinstance(a, (list, tuple)):
        assert type(a) is type(b) and len(a) == len(b), f"{path}: {type(a)} len {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            same(x, y, f"{path}[{i}]")
        return
    if isinstance(a, float):
        assert isinstance(b, float) and (a == b and math.copysign(1, a) == math.copysign(1, b)
                                         or (math.isnan(a) and math.isnan(b))), f"{path}: {a!r} != {b!r}"
        return
    if isinstance(a, np.ndarray):
        assert isinstance(b, np.ndarray) and a.dtype == b.dtype and np.array_equal(a, b, equal_nan=True), path
        return
    assert type(a) is type(b) and a == b, f"{path}: {a!r} != {b!r}"


def read_both(path: Path):
    """(KiPI's board, ours): the same file read with KiPI's trims and with the stand-in."""
    ft.uninstall()
    kipi = R.read_board(path)
    assert ft.install()
    ours = R.read_board(path)
    return kipi, ours


def assert_identical(path: Path) -> None:
    kipi, ours = read_both(path)
    same(kipi, ours)
    protocol = pickle.HIGHEST_PROTOCOL
    assert pickle.dumps(kipi, protocol=protocol) == pickle.dumps(ours, protocol=protocol)


# ------------------------------------------------------------------- KiPI's and the repository's boards

@pytest.mark.parametrize("path", KIPI_BOARDS, ids=lambda p: p.name)
def test_kipis_reader_boards_read_identically(path):
    assert_identical(path)


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_the_example_boards_open_identically(path):
    from coppernicus.engine import Board
    ft.uninstall()
    kipi = Board.open(path).snapshot
    assert ft.install()
    ours = Board.open(path).snapshot
    same(kipi, ours)
    assert pickle.dumps(kipi) == pickle.dumps(ours)


# ------------------------------------------------------------------- vias set to remove unused layers

def _via(x: float, y: float, net: str, layers=("F.Cu", "B.Cu"), keep_ends=False, size=0.6, drill=0.3,
         tag="") -> str:
    lay = " ".join(f'"{n}"' for n in layers)
    uid = uuid.uuid5(uuid.NAMESPACE_URL, f"fast-trim/{tag}/{x}/{y}/{net}")
    return (f'\t(via\n\t\t(at {x:g} {y:g})\n\t\t(size {size:g})\n\t\t(drill {drill:g})\n\t\t(layers {lay})\n'
            f'\t\t(remove_unused_layers yes)\n\t\t(keep_end_layers {"yes" if keep_ends else "no"})\n'
            f'\t\t(net "{net}")\n\t\t(uuid "{uid}")\n\t)\n')


def trim_board(tmp_path: Path, seed: int) -> Path:
    """KiPI's features board with vias set to remove unused layers: on a grid over the whole board
    (inside and around its zone fills, their holes and pads), at track ends, in close pairs (so
    KiPI's neighbouring-via rule acts), some keeping their end layers, some blind."""
    rng = np.random.default_rng(seed)
    vias = []
    for x in np.arange(2.5, 99.0, 3.7):
        for y in np.arange(2.0, 59.0, 3.1):
            x1, y1 = x + rng.uniform(-1.2, 1.2), y + rng.uniform(-1.2, 1.2)
            net = NETS[int(rng.integers(len(NETS)))]
            blind = rng.random() < 0.1
            layers = ("F.Cu", "In1.Cu") if blind else ("F.Cu", "B.Cu")
            vias.append(_via(round(x1, 4), round(y1, 4), net, layers, keep_ends=rng.random() < 0.3,
                             size=0.45 if blind else 0.6, drill=0.2 if blind else 0.3, tag=f"g{seed}"))
            if rng.random() < 0.08:              # a neighbour 0.5 mm away, same net
                vias.append(_via(round(x1 + 0.5, 4), round(y1, 4), net, tag=f"n{seed}"))
    for x, y, net in ((20, 20, "SIG"), (40, 20, "SIG"), (70, 20, "SIG"), (70, 33, "SIG")):   # track ends
        vias.append(_via(x + 0.05, y, net, tag=f"e{seed}"))
    # Its through-hole pads set to remove unused layers too (KiPI's board sets one of four).
    text = (DATA / "kipi_features.kicad_pcb").read_text(encoding="utf-8")
    text = text.replace("(remove_unused_layers no)", "(remove_unused_layers yes)")
    at = text.index("\t(zone\n")
    out = tmp_path / f"trim{seed}.kicad_pcb"
    out.write_text(text[:at] + "".join(vias) + text[at:], encoding="utf-8")
    shutil.copy(DATA / "kipi_features.kicad_pro", tmp_path / f"trim{seed}.kicad_pro")
    return out


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_vias_set_to_remove_unused_layers_trim_identically(tmp_path, seed):
    path = trim_board(tmp_path, seed)
    kipi, ours = read_both(path)
    same(kipi, ours)
    assert pickle.dumps(kipi) == pickle.dumps(ours)
    # The board exercises the trim: some via pads dropped, some kept, some vias left padless.
    pads = sum(len(v.pad_mm) for v in kipi.vias)
    spans = sum(len(v.layers) for v in kipi.vias)
    assert 0 < pads < spans
    assert any(not v.pad_mm for v in kipi.vias)
    original = R.read_board(DATA / "kipi_features.kicad_pcb")   # through-hole pads lost layers too
    assert (sum(len(p.shapes) for p in kipi.pads) < sum(len(p.shapes) for p in original.pads))


def test_the_stand_in_converts_fewer_geometries(tmp_path, monkeypatch):
    """Where the time went: KiPI converts every pad and zone fill of every net to shapely (and
    repairs the zones' fractured fills); the stand-in converts only those a trimmed via's disc
    can reach, once each."""
    path = trim_board(tmp_path, 1)
    counts = {"n": 0}
    real = ft.poly_to_shapely

    def counting(p):
        counts["n"] += 1
        return real(p)
    monkeypatch.setattr(ft, "poly_to_shapely", counting)
    assert ft.install()
    R.read_board(path)
    ours = counts["n"]
    snap = R.read_board(path)
    every = sum(len(polys) for p in snap.pads for polys in p.shapes.values()) + \
        sum(len(polys) for z in snap.zones for polys in z.filled.values())
    assert 0 < ours < every


# ------------------------------------------------------------------- the switch and the seam

def test_installed_by_default_on_the_kipi_it_was_written_for():
    assert ft.installed()
    assert R.trim_unused_via_pads is ft.trim_unused_via_pads
    assert R.trim_unused_layers is ft.trim_unused_layers


@pytest.mark.parametrize("value, installed", [(None, True), ("0", False), ("1", True)])
def test_importing_the_engine_installs_it_unless_the_variable_says_0(value, installed):
    env = {k: v for k, v in os.environ.items() if k != ft.ENV}
    if value is not None:
        env[ft.ENV] = value
    code = ("import coppernicus.engine.fast_trim as ft; "
            "print(ft.installed(), ft.reader.trim_unused_via_pads is ft.trim_unused_via_pads)")
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == [str(installed)] * 2


def test_uninstall_puts_kipis_back():
    ft.uninstall()
    assert not ft.installed()
    assert R.trim_unused_via_pads is ft.KIPI["trim_unused_via_pads"]
    assert R.trim_unused_layers is ft.KIPI["trim_unused_layers"]


def test_only_on_the_kipi_it_was_written_for(monkeypatch):
    ft.uninstall()
    monkeypatch.setattr(ft.kipi, "__version__", "1.0.2")
    with pytest.warns(UserWarning, match="1.0.2"):
        assert not ft.install()
    assert R.trim_unused_via_pads is ft.KIPI["trim_unused_via_pads"]


def test_not_over_a_function_of_another_shape(monkeypatch):
    ft.uninstall()
    monkeypatch.setattr(R, "trim_unused_layers", lambda trim, tracks: 0)
    with pytest.warns(UserWarning, match="trim_unused_layers"):
        assert not ft.install()
    assert not ft.installed()


def test_install_is_idempotent_and_warns_once(monkeypatch):
    assert ft.install() and ft.install() and ft.installed()
    ft.uninstall()
    monkeypatch.setattr(ft.kipi, "__version__", "9")
    with pytest.warns(UserWarning):
        ft.install()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert not ft.install()
