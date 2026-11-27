"""Entry point of the packaged app (PyInstaller runs this file; see coppernicus.spec).

Same commands as ``coppernicus`` (no argument or ``gui``: the app; ``solve``, ``nets``, ``mcp``…:
the command line). Before anything imports KiPI, the Triangle mesher, which the bundle does
not contain, is put on ``sys.path``; on the first start it is fetched from PyPI
(coppernicus.bootstrap_triangle): with a small dialog for the app, with lines on stderr for a
command. With ``bootstrap_triangle.on_demand()`` nothing is fetched at the start: Coppernicus's
stand-in takes Triangle's place (coppernicus.triangle_standin) until the triangle mesher is
chosen, or ``--fetch-triangle`` fetches it. In a normal install this file is not used.
"""

import json
import multiprocessing
import os
import sys


def main() -> int:
    multiprocessing.freeze_support()          # a worker process stops here
    # Finder used to add a -psn_… argument to apps it starts; it is not a Coppernicus argument.
    argv = [a for a in sys.argv[1:] if not a.startswith("-psn_")]
    if "--remove-triangle" in argv:
        return remove_triangle()
    if "--fetch-triangle" in argv:
        return fetch_triangle()
    if not {"--version", "-h", "--help"} & set(argv):     # argparse answers those without KiPI
        code = ensure_triangle(argv)
        if code:
            return code
        keep_matplotlib_cache()
    from coppernicus.__main__ import main as coppernicus_main
    return coppernicus_main(argv)


def remove_triangle() -> int:
    """``--remove-triangle``: delete the downloaded Triangle mesher (the next start fetches it
    again). 0 when done or nothing was there, 2 when it could not be deleted."""
    from coppernicus import bootstrap_triangle as bt
    site = bt.site_dir()
    try:
        size = bt.remove(site)
    except bt.RemovalDeferred as e:          # a file in use: removed at the next start
        print(f"{e.message}.\n  {e.hint}.")
        return 0
    except bt.BootstrapError as e:
        print(f"error: {e.message}\n  {e.hint}", file=sys.stderr)
        return 2
    if size:
        print(f"Removed the downloaded Triangle mesher from {site} ({size / 1e6:.1f} MB). "
              "Coppernicus downloads it again on its next start.")
    else:
        print(f"Nothing to remove: no downloaded Triangle mesher in {site}.")
    return 0


def fetch_triangle() -> int:
    """``--fetch-triangle``: download the Triangle mesher now (lines on stderr), whether or not a
    start would. 0 when it is installed, 2 when it could not be fetched."""
    from coppernicus import bootstrap_triangle as bt
    try:
        plan = bt.needed()
        if plan is None:
            print(f"The Triangle mesher is already installed in {bt.site_dir()}.")
        else:
            bt.fetch_on_console(plan)
    except bt.BootstrapError as e:
        print(f"error: {e.message}\n  {e.hint}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"error: cannot prepare the Triangle mesher: {e}", file=sys.stderr)
        return 2
    return 0


def keep_matplotlib_cache() -> None:
    """PyInstaller's run-time hook gives matplotlib a new, empty config folder at every start,
    where it rebuilds its font cache (seconds of each start). Keep it instead in the per-user
    Coppernicus folder, beside the Triangle folder."""
    from coppernicus import bootstrap_triangle as bt
    folder = bt.site_dir().parent / "matplotlib"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return                                # PyInstaller's temporary folder still works
    os.environ["MPLCONFIGDIR"] = str(folder)


def ensure_triangle(argv) -> int:
    """0 once triangle can be imported (the real one, or on demand Coppernicus's stand-in); 2 (the
    CLI's error code) when it cannot."""
    from coppernicus import bootstrap_triangle as bt
    gui = not argv or argv[0] == "gui"
    try:
        try:
            plan = bt.at_start()
            if plan is not None:
                if gui:
                    if not bt.fetch_with_dialog(plan):  # the dialog said why; the user quit
                        return 2
                else:
                    bt.fetch_on_console(plan)
        except OSError as e:                  # never exit 1: the CLI's "a limit was exceeded"
            raise bt.BootstrapError(f"cannot prepare the Triangle mesher: {e}",
                                    f"check that {bt.site_dir().parent} is writable") from e
    except bt.BootstrapError as e:
        print(f"error: {e.message}\n  {e.hint}", file=sys.stderr, flush=True)
        if gui:
            from PySide6.QtWidgets import QApplication, QMessageBox
            _app = QApplication.instance() or QApplication(sys.argv[:1])
            QMessageBox.critical(None, "Coppernicus", f"{e.message}.\n\n{e.hint}.")
        elif "--json" in argv and argv[0] != "mcp":     # the CLI's error document
            print(json.dumps({"error": e.to_dict()}, ensure_ascii=False))
        return 2
    from coppernicus.triangle_standin import ensure_triangle_importable
    ensure_triangle_importable()              # what at_start() did not fetch: the stand-in
    return 0


if __name__ == "__main__":
    sys.exit(main())
