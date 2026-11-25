# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec of the packaged Coppernicus: a one-folder app, Coppernicus.app on macOS,
# a Coppernicus folder (Coppernicus.exe for the app, coppernicus-cli.exe for the command line)
# on Windows. Run it through packaging/build_macos.sh or packaging/build_windows.ps1.
#
# The bundle holds everything except the Triangle mesher (the 'triangle' package): its
# licence forbids bundling it. The build writes the pin uv.lock holds for it (version, wheel
# URLs, SHA-256) into the bundle; the app fetches it on its first start
# (coppernicus.bootstrap_triangle). The build fails if triangle ends up in the bundle.

import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

HERE = Path(SPECPATH).resolve()                     # packaging/
ROOT = HERE.parent
GEN = ROOT / "build" / "packaging"                  # files this spec generates
GEN.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(HERE))                       # notices.py (never import "packaging.*")

import notices  # noqa: E402

from coppernicus import __version__  # noqa: E402
from coppernicus import bootstrap_triangle as bt  # noqa: E402

MACOS = sys.platform == "darwin"
WINDOWS = sys.platform == "win32"

# The pin, from uv.lock; fail now if this platform has no wheel to fetch.
pin = bt.pin_from_lock((ROOT / "uv.lock").read_text(encoding="utf-8"))
bt.write_pin(pin, GEN / bt.PIN_FILE)
print(f"coppernicus.spec: triangle pinned at {pin.version}, wheel {bt.choose_wheel(pin).filename}")

EXAMPLES = [(str(p), str(p.parent.relative_to(ROOT)))
            for p in sorted((ROOT / "examples").glob("*/*")) if p.is_file()]
# Every module of coppernicus, listed from the files: collect_submodules would import them,
# and the engine's modules import KiPI, which imports triangle, absent from the build.
SRC = ROOT / "src"
MODULES = sorted(".".join(p.relative_to(SRC).with_suffix("").parts).removesuffix(".__init__")
                 for p in (SRC / "coppernicus").rglob("*.py"))


def _pyvista_vtk_modules():
    """The VTK modules pyvista can load: the ones its lazy loader (pyvista._vtk) maps classes
    to, plus those of its special loaders. PyInstaller cannot see these dynamic imports; it
    sees instead ``from vtk import *`` under ``if TYPE_CHECKING`` in the same file, which would
    bundle all of VTK (its ``vtk`` module is excluded below)."""
    import pyvista._vtk as v
    names = set(v._CORE_MODULES) | set(v._PLOTTING_MODULES) | set(v._OPENGL_MODULES)
    names |= {"vtkPythonContext2D", "vtkRenderingCore", "vtkRenderingOpenGL2", "vtkCommonDataModel"}
    return sorted(f"vtkmodules.{n}" for n in names)


VTK_MODULES = _pyvista_vtk_modules()

a = Analysis(
    [str(HERE / "frozen_main.py")],
    pathex=[str(ROOT / "src")],
    datas=[
        *collect_data_files("coppernicus"),          # report_assets/, app/resources/icon.png
        (str(GEN / bt.PIN_FILE), "."),
        (str(HERE / "licenses" / "TRIANGLE_NOTICE.txt"), "licenses"),
        (str(HERE / "licenses" / "LGPL-3.0.txt"), "licenses"),
        (str(ROOT / "LICENSE"), "."),
        *EXAMPLES,
    ],
    # The CLI, the app and the engine import most modules inside functions.
    hiddenimports=[*MODULES, *VTK_MODULES],
    excludes=[
        "triangle",                                  # fetched on first start, never bundled
        "vtk", "vtkmodules.all",                     # all of VTK: see _pyvista_vtk_modules
        "tkinter", "_tkinter", "turtle", "idlelib",
        "pytest", "_pytest", "IPython", "jupyter_client", "ipykernel", "notebook",
        "PyObjCTest", "pydoc_data",
        # Build tools, not in uv.lock (PyInstaller installs setuptools): optional imports only.
        "setuptools", "pkg_resources", "_distutils_hack", "PyInstaller",
    ],
    noarchive=False,
)


def _test_part(name):
    parts = re.split(r"[./\\]", name)
    return "tests" in parts or "PyObjCTest" in parts


# Lean: no test suites (numpy, scipy, matplotlib... ship theirs inside the packages).
a.pure = [e for e in a.pure if not _test_part(e[0])]
a.datas = [e for e in a.datas if not _test_part(e[0])]
a.binaries = [e for e in a.binaries if not _test_part(e[0])]


def _triangle(entry):
    first = entry[0].replace("\\", "/").split("/")[0].lower()
    return first.split(".")[0] == "triangle" or re.match(r"triangle-[^/]*\.dist-info", first)


leaks = [e[0] for e in (*a.pure, *a.binaries, *a.datas) if _triangle(e)]
if leaks:
    raise SystemExit(f"coppernicus.spec: triangle must not be bundled: {leaks[:5]}")

# THIRD_PARTY_NOTICES.txt from what was collected.
python_licence = Path(sys.base_prefix) / ("LICENSE.txt" if WINDOWS else
                                          f"lib/python{sys.version_info[0]}.{sys.version_info[1]}/LICENSE.txt")
listed = notices.write(GEN / "THIRD_PARTY_NOTICES.txt",
                       [e[0] for e in (*a.pure, *a.binaries, *a.datas)], __version__, python_licence)
print(f"coppernicus.spec: THIRD_PARTY_NOTICES.txt lists {len(listed)} distributions")
a.datas.append(("THIRD_PARTY_NOTICES.txt", str(GEN / "THIRD_PARTY_NOTICES.txt"), "DATA"))


def _macos_minimum():
    """The newest macOS the bundled wheels ask for (their macosx_X_Y tags); Python itself
    (python-build-standalone) asks for 11.0."""
    best = (11, 0)
    tops = notices.top_level_names(e[0] for e in (*a.pure, *a.binaries, *a.datas))
    for dist in notices.bundled_distributions(tops):
        wheel = dist.read_text("WHEEL") or ""
        for tag in re.findall(r"^Tag: .*-macosx_(\d+)_(\d+)_", wheel, re.MULTILINE):
            best = max(best, (int(tag[0]), int(tag[1])))
    return f"{best[0]}.{best[1]}"


pyz = PYZ(a.pure)
ICON = str(ROOT / "Logo" / "Copper Circuit Orbit Logo NEW.png")   # converted by Pillow

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Coppernicus",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON,
)
executables = [exe]
if WINDOWS:
    # A windowed program has no console: the command line (solve, nets, mcp...) needs its own.
    executables.append(EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="coppernicus-cli",
        debug=False,
        strip=False,
        upx=False,
        console=True,
        icon=ICON,
    ))

coll = COLLECT(
    *executables,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Coppernicus",
)

if MACOS:
    minimum = _macos_minimum()
    print(f"coppernicus.spec: LSMinimumSystemVersion {minimum}")
    app = BUNDLE(
        coll,
        name="Coppernicus.app",
        icon=ICON,
        bundle_identifier="io.github.raffaelebiondi.coppernicus",
        version=__version__,
        info_plist={
            "CFBundleName": "Coppernicus",
            "CFBundleDisplayName": "Coppernicus",
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "LSMinimumSystemVersion": minimum,
            "NSHighResolutionCapable": True,
            "NSHumanReadableCopyright": "Coppernicus is free software under GPL-3.0-or-later",
        },
    )
