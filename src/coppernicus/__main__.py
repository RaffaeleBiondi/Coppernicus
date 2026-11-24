"""``python -m coppernicus`` and the ``coppernicus`` command.

The engine meshes large nets in a process pool; on macOS and Windows worker processes
re-import the main module, hence the ``__main__`` guard and ``freeze_support()`` (needed in
a frozen app).
"""

import multiprocessing
import sys


def main(argv=None) -> int:
    multiprocessing.freeze_support()
    from coppernicus.cli import main as cli_main
    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
