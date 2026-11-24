"""Background solve, so the window stays responsive."""

from __future__ import annotations

import copy
import traceback

from PySide6.QtCore import QThread, Signal

from coppernicus.engine import Board, Cancelled, EngineError, solve_rail
from coppernicus.project import Rail, Settings


class SolveWorker(QThread):
    message = Signal(str)
    succeeded = Signal(object)        # RailResult
    failed = Signal(str)

    def __init__(self, board: Board, rail: Rail, settings: Settings, parent=None):
        super().__init__(parent)
        # Copies: the user may keep editing while the solve runs.
        self._board, self._rail, self._settings = board, copy.deepcopy(rail), copy.deepcopy(settings)
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            result = solve_rail(self._board, self._rail, self._settings, progress=self.message.emit,
                                cancelled=lambda: self._cancel)
        except Cancelled:
            self.failed.emit("Solve cancelled")
        except EngineError as e:
            self.failed.emit(str(e))
        except Exception:                                   # noqa: BLE001 - shown to the user
            self.failed.emit("Unexpected error:\n" + traceback.format_exc())
        else:
            self.succeeded.emit(result)


class ReportWorker(QThread):
    """Writes the offline HTML report (seconds on big boards) away from the UI thread."""
    succeeded = Signal(object)        # Path of the written file
    failed = Signal(str)

    def __init__(self, results, project, path, outline, board_name: str, parent=None):
        super().__init__(parent)
        self._results, self._project, self._path = list(results), project, path
        self._outline, self._board_name = outline, board_name

    def run(self):
        from coppernicus.report import write_report
        try:
            path = write_report(self._results, self._project, self._path, outline=self._outline,
                                board_name=self._board_name)
        except Exception:                                   # noqa: BLE001 - shown to the user
            self.failed.emit("Cannot write the report:\n" + traceback.format_exc())
        else:
            self.succeeded.emit(path)
