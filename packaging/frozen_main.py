"""Entry point of the packaged app (PyInstaller runs this file; see coppernicus.spec).

Same commands as ``coppernicus`` (no argument or ``gui``: the app; ``solve``, ``nets``, ``mcp``…:
the command line). Before anything imports KiPI, the Triangle mesher, which the bundle does
not contain, is put on ``sys.path``; on the first start it is fetched from PyPI
(coppernicus.bootstrap_triangle): with a small dialog for the app, with lines on stderr for a
command. In a normal install this file is not used.
"""

import json
import multiprocessing
import os
import sys


def main() -> int:
    multiprocessing.freeze_support()          # a worker process stops here
    # Finder used to add a -psn_… argument to apps it starts; it is not a Coppernicus argument.
    argv = [a for a in sys.argv[1:] if not a.startswith("-psn_")]
    if not {"--version", "-h", "--help"} & set(argv):     # argparse answers those without KiPI
        code = ensure_triangle(argv)
        if code:
            return code
        keep_matplotlib_cache()
    from coppernicus.__main__ import main as coppernicus_main
    return coppernicus_main(argv)


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
    """0 once triangle can be imported; 2 (the CLI's error code) when it cannot."""
    from coppernicus import bootstrap_triangle as bt
    gui = not argv or argv[0] == "gui"
    try:
        plan = bt.needed()
        if plan is not None:
            if gui:
                if not bt.fetch_with_dialog(plan):      # the dialog said why; the user quit
                    return 2
            else:
                bt.fetch_on_console(plan)
    except bt.BootstrapError as e:
        print(f"error: {e.message}\n  {e.hint}", file=sys.stderr, flush=True)
        if gui:
            from PySide6.QtWidgets import QApplication, QMessageBox
            _app = QApplication.instance() or QApplication(sys.argv[:1])
            QMessageBox.critical(None, "Coppernicus", f"{e.message}.\n\n{e.hint}.")
        elif "--json" in argv and argv[0] != "mcp":     # the CLI's error document
            print(json.dumps({"error": e.to_dict()}, ensure_ascii=False))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
