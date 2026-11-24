"""The session: opened boards and solved results, addressable by id across calls.

Every public method takes and returns plain JSON-serialisable data and raises
:class:`~coppernicus.service.errors.ServiceError` with a code, a message and a hint. The
CLI (``--json``) and the MCP server are thin layers over it; the GUI can move onto it later.

Results are big (a large ground net is ~1 GB of arrays), so the session keeps the arrays of
only the most recently used results, within a count and a memory bound. An evicted
result keeps its compact summary (``compare_results`` still works) and asks to be solved
again for anything that needs the field.
"""

from __future__ import annotations

import difflib
import fnmatch
import json
import logging
import math
import os
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

import numpy as np
import yaml
from shapely.geometry import LineString
from shapely.ops import unary_union

from coppernicus import board_fields as BF
from coppernicus.engine import Board, Cancelled, EngineError, solve_rail, tied_nets
from coppernicus.engine.board import board_file_filter
from coppernicus.fields import TriangleLocator, current_vectors
from coppernicus.maps import FIELDS, render_layer_map, safe_name, save_layer_maps
from coppernicus.power_tree import EstimateError, estimated_draws
from coppernicus.problems import Problem, find_problems
from coppernicus.project import Limits, Project, ProjectError, Rail, Settings
from coppernicus.results import LayerField, RailResult
from coppernicus.service import convert as C
from coppernicus.service import errors as E
from coppernicus.service import fields as F
from coppernicus.service.errors import ServiceError

log = logging.getLogger("coppernicus.service")

Progress = Optional[Callable[[str], None]]
Cancel = Optional[Callable[[], bool]]

DEFAULT_MAX_RESULTS = 8
DEFAULT_MAX_RESULT_MB = 2048
DEFAULT_MAX_BOARDS = 4
MAX_SUMMARIES = 256
LIST_LIMIT = 200
PIN_LIST_LIMIT = 64


FIELD_HINT = ("fix the field in KiCad (on the symbol, then update the PCB); or give the rail in the "
              "project file with override_board_fields: true; or turn the fields off with "
              "use_board_fields: false (CLI: --no-board-fields)")


@dataclass
class _BoardEntry:
    id: str
    path: Path
    mtime_ns: int
    board: Board
    _outline: Optional[list] = None
    _board_fields: Optional[BF.BoardFields] = None     # read on first use

    def outline(self) -> list:
        if self._outline is None:
            self._outline = self.board.outline()
        return self._outline

    def variant(self, thickness: Mapping[str, float]) -> Board:
        """The board, or a what-if copy with other copper thicknesses (cached by the board)."""
        if not thickness:
            return self.board
        try:
            return self.board.with_copper_thickness(dict(thickness))
        except EngineError as e:
            raise ServiceError(E.LAYER_NOT_FOUND, str(e),
                               "copper_thickness_mm takes copper layer names as in KiCad") from None

    def stale(self) -> bool:
        try:
            return self.path.stat().st_mtime_ns != self.mtime_ns
        except OSError:
            return True

    def warnings(self) -> List[str]:
        out = list(self.board.warnings)
        if self.stale():
            out.append(f"The board file {self.path.name} changed on disk (or is gone) since it "
                       "was opened; call open_board again to analyse the current file.")
        return out


@dataclass
class _ResultRecord:
    id: str
    board_id: str
    board: Optional[Board]
    outline: Optional[list]
    rail: Rail
    settings: Settings
    thickness: Dict[str, float]
    layer_thickness: Dict[str, float]
    result: Optional[RailResult]
    problems: List[Problem]
    summary: Dict[str, Any]
    nbytes: int
    queries: Dict[str, Any] = field(default_factory=dict)      # per layer: locator, J, copper


class _Live(NamedTuple):
    rec: _ResultRecord
    res: RailResult
    board: Board
    outline: list


def _result_nbytes(res: RailResult) -> int:
    return int(sum(a.nbytes for lf in res.layers for a in (
        lf.points, lf.tris, lf.voltage, lf.current_density, lf.tri_power, lf.tri_area)))


def _limited(names: Sequence[str], limit: int = 30) -> str:
    names = list(names)
    more = f" … and {len(names) - limit} more" if len(names) > limit else ""
    return ", ".join(names[:limit]) + more


class Session:
    """Boards and results by id. Thread-safe; solves run one at a time."""

    def __init__(self, out_dir=None, max_results: int = DEFAULT_MAX_RESULTS,
                 max_result_mb: float = DEFAULT_MAX_RESULT_MB, max_boards: int = DEFAULT_MAX_BOARDS):
        self._out_dir = Path(out_dir).expanduser().resolve() if out_dir else None
        self.max_results = max(1, int(max_results))
        self.max_result_bytes = int(max_result_mb * 1024 * 1024)
        self.max_boards = max(1, int(max_boards))
        self._boards: "OrderedDict[str, _BoardEntry]" = OrderedDict()
        self._board_keys: Dict[Tuple[str, int], str] = {}
        self._results: "OrderedDict[str, _ResultRecord]" = OrderedDict()
        self._n_boards = self._n_results = 0
        self._lock = threading.RLock()
        self._solve_lock = threading.Lock()

    # ================================================================== boards
    def open_board(self, path) -> Dict[str, Any]:
        """Open a board file read-only (or reuse it when already open and unchanged)."""
        entry = self._open_entry(path)
        board = entry.board
        kinds = {"power": 0, "ground": 0}
        for n in board.nets():
            k = board.net_kind(n)
            if k in kinds:
                kinds[k] += 1
        rings = entry.outline()
        if rings:
            allpts = np.vstack(rings)
            x0, y0 = allpts.min(axis=0)
            x1, y1 = allpts.max(axis=0)
            bbox = [x0, y0, x1, y1]
        else:
            bbox = None
        return C.jsonable({
            "board_id": entry.id, "path": entry.path, "name": board.name,
            "layers": [{"name": lay.name, "thickness_mm": lay.thickness_mm} for lay in board.layers()],
            "counts": board.counts(), "power_nets": kinds["power"], "ground_nets": kinds["ground"],
            "outline_bbox_mm": bbox,
            "assumptions": ["Coordinates are mm in KiCad's frame (x right, y down); layer names "
                            "as in KiCad.", C.NET_KIND_CAVEAT],
            "warnings": entry.warnings(),
        })

    def _open_entry(self, path) -> _BoardEntry:
        p = Path(str(path)).expanduser()
        try:
            p = p.resolve()
            st = p.stat()
        except OSError:
            raise ServiceError(E.FILE_NOT_FOUND, f"No such file: {path}",
                               "give an absolute path to a board file") from None
        key = (str(p), st.st_mtime_ns)
        with self._lock:
            bid = self._board_keys.get(key)
            if bid in self._boards:
                self._boards.move_to_end(bid)
                return self._boards[bid]
        try:
            board = Board.open(p)
        except Exception as e:         # any importer failure means: this file can't be read
            log.debug("could not read %s", p, exc_info=True)
            hint = (f"{p.name} looks like a project file: use solve_project for it"
                    if p.suffix.lower() in (".yml", ".yaml") else f"readable formats: {board_file_filter()}")
            detail = str(e) if isinstance(e, EngineError) else f"{type(e).__name__}: {e}"
            raise ServiceError(E.BOARD_UNREADABLE, f"Could not read {p.name}: {detail}", hint) from None
        with self._lock:
            self._n_boards += 1
            entry = _BoardEntry(f"b{self._n_boards}", p, st.st_mtime_ns, board)
            self._boards[entry.id] = entry
            self._board_keys[key] = entry.id
            while len(self._boards) > self.max_boards:
                old, _ = self._boards.popitem(last=False)
                self._board_keys = {k: v for k, v in self._board_keys.items() if v != old}
        return entry

    def _entry(self, board_id: str) -> _BoardEntry:
        with self._lock:
            entry = self._boards.get(str(board_id))
            if entry is None:
                open_ids = ", ".join(self._boards) or "none"
                raise ServiceError(E.BOARD_NOT_OPEN, f"No open board with id {board_id!r}",
                                   f"call open_board first (open boards: {open_ids})")
            self._boards.move_to_end(entry.id)
            return entry

    def _check_net(self, board: Board, net: str) -> None:
        nets = board.nets()
        if net in nets:
            return
        folded = {n.casefold(): n for n in nets}
        close = difflib.get_close_matches(net.casefold(), list(folded), n=8, cutoff=0.5)
        same_case = folded.get(net.casefold())
        hint = (f"did you mean {same_case!r}?" if same_case else
                "close matches: " + ", ".join(folded[c] for c in close) if close else
                "use list_nets to see the board's nets")
        raise ServiceError(E.NET_NOT_FOUND, f"No net {net!r} on this board", hint)

    def list_nets(self, board_id: str, kind: str = "power_or_ground", filter: Optional[str] = None,
                  limit: int = LIST_LIMIT) -> Dict[str, Any]:
        """Nets with their kind and the layers they have copper on."""
        entry = self._entry(board_id)
        board = entry.board
        kinds = {"power_or_ground": ("power", "ground"), "power": ("power",), "ground": ("ground",),
                 "signal": ("signal",), "all": ("power", "ground", "signal")}
        if kind not in kinds:
            raise ServiceError(E.INVALID_INPUT, f"unknown kind {kind!r}",
                               f"kind is one of: {', '.join(kinds)}")
        names = {lay.id: lay.name for lay in board.layers()}
        nets = [n for n in board.nets() if board.net_kind(n) in kinds[kind]]
        if filter:
            pat = filter.casefold()
            globbing = any(c in pat for c in "*?[")
            nets = [n for n in nets if (fnmatch.fnmatchcase(n.casefold(), pat) if globbing
                                        else pat in n.casefold())]
        limit = max(1, int(limit))
        shown = nets[:limit]

        def described(n):
            per_layer = board.net_layer_kinds(n)
            copper = [lid for lid, what in per_layer.items() if what & {"track", "zone"}]
            return {"name": n, "kind": board.net_kind(n),
                    "layers": [names[i] for i in per_layer],
                    "layers_with_copper": [names[i] for i in copper],
                    "layers_with_only_vias_or_pads": [names[i] for i in per_layer if i not in copper]}

        return C.jsonable({
            "board_id": entry.id, "kind": kind, "filter": filter, "count": len(nets),
            "returned": len(shown), "truncated": len(nets) > len(shown),
            "nets": [described(n) for n in shown],
            "assumptions": [C.NET_KIND_CAVEAT,
                            "'layers' lists every copper layer the net touches. Through vias and "
                            "through-hole pads reach every layer they cross, so 'layers_with_copper' "
                            "(tracks or zones of the net) is the better guide to where it runs; "
                            "'layers_with_only_vias_or_pads' holds only their rings or pads."],
            "warnings": entry.warnings(),
        })

    def net_parts(self, board_id: str, net: str) -> Dict[str, Any]:
        """Parts and pins on a net, and the net ties joining it to other nets."""
        entry = self._entry(board_id)
        board = entry.board
        self._check_net(board, net)
        parts = []
        for p in board.parts_on_net(net):
            parts.append({"ref": p.ref, "value": p.value, "footprint": p.footprint,
                          "pin_count": len(p.pins), "pins": p.pins[:PIN_LIST_LIMIT],
                          "pins_truncated": len(p.pins) > PIN_LIST_LIMIT})
        ties = [t for t in board.net_ties() if net in (t.net_a, t.net_b)]
        joined, _ = tied_nets(board, net)
        return C.jsonable({
            "board_id": entry.id, "net": net, "kind": board.net_kind(net), "count": len(parts),
            "parts": parts,
            "net_ties": [{"ref": t.ref, "net_a": t.net_a, "net_b": t.net_b} for t in ties],
            "solved_with": [n for n in joined if n != net],
            "assumptions": ["Net ties are solved as shorts: nets in 'solved_with' are solved "
                            "together with this one unless follow_net_ties is false."],
            "warnings": entry.warnings(),
        })

    # ================================================================== solving
    def solve_rail(self, board_id: str, net: str, sources: Sequence[Mapping[str, Any]],
                   loads: Sequence[Mapping[str, Any]], limits: Optional[Mapping[str, Any]] = None,
                   settings: Optional[Mapping[str, Any]] = None, follow_net_ties: bool = True,
                   top: int = 5, progress: Progress = None, cancelled: Cancel = None,
                   base_rail: Optional[Rail] = None,
                   base_settings: Optional[Settings] = None) -> Dict[str, Any]:
        """Solve one rail: sources hold ``voltage_v`` on their pins, loads draw ``current_a``
        split equally across their pins. Returns a compact summary with a ``result_id``.

        ``base_rail`` (in-process callers only) is an existing rail the dicts edit: every
        field they do not set, on the rail and on each matching terminal, is carried over.
        ``base_settings`` (in-process callers only, e.g. a project's) are the settings that
        ``settings`` edits; its ``copper_thickness_mm`` overrides theirs layer by layer."""
        entry = self._entry(board_id)
        if not isinstance(sources, (list, tuple)) or not isinstance(loads, (list, tuple)):
            raise ServiceError(E.INVALID_INPUT, "sources and loads must be lists of objects")
        rail = C.rail_from_dicts(net, sources, loads, limits, follow_net_ties, base=base_rail)
        solve_settings, _ = C.settings_from_dict(settings, base=base_settings)
        live = self._solve(entry, rail, solve_settings, top, progress, cancelled)
        return C.jsonable(live.rec.summary)

    def _validate_rail(self, board: Board, rail: Rail, project: Optional[Project] = None) -> None:
        self._check_net(board, rail.net)
        if not rail.sources:
            raise ServiceError(E.MISSING_SOURCE, f"{rail.net}: add a source (the regulator output) "
                               "before solving", "a source is {ref, voltage_v, pins?}")
        if not rail.loads and project is None:     # with the project, its draws can be the loads
            raise ServiceError(E.MISSING_LOAD, f"{rail.net}: add at least one load before solving",
                               "a load is {ref, current_a, pins?}")
        nets = tied_nets(board, rail.net)[0] if rail.follow_net_ties else [rail.net]
        pins_by_ref: Dict[str, set] = {}
        for n in nets:
            for p in board.parts_on_net(n):
                pins_by_ref.setdefault(p.ref, set()).update(p.pins)
        where = " / ".join(nets)
        for t in [*rail.sources, *rail.loads]:
            if t.ref not in pins_by_ref:
                raise ServiceError(E.PART_NOT_ON_NET, f"{t.ref} has no pads on net {where}",
                                   f"parts on {where}: {_limited(sorted(pins_by_ref))}")
            available = sorted(pins_by_ref[t.ref], key=lambda s: (len(s), s))
            for pattern in t.pins or ():
                if not any(fnmatch.fnmatchcase(p, pattern) for p in available):
                    raise ServiceError(E.PIN_NOT_ON_NET, f"{t.ref} has no pin {pattern!r} on net {where}",
                                       f"pins of {t.ref} on this net: {_limited(available, 64)}")

    def _solve(self, entry: _BoardEntry, rail: Rail, settings: Settings, top: int,
               progress: Progress, cancelled: Cancel, project: Optional[Project] = None,
               notes: Sequence[str] = (), warnings: Sequence[str] = (),
               fields_info: Optional[Mapping[str, Any]] = None) -> _Live:
        """Solve one rail. With ``project`` (a project rail), it also draws the regulator
        inputs and links it feeds, estimated at nominal voltages, as the app solves it.
        ``notes`` and ``warnings`` (where its rail came from) are added to the result's, and
        ``fields_info`` to the summary (``from_board_fields``, ``overrides_board_fields``...)."""
        # The what-if thickness is in the settings; the engine applies it (to the variant
        # here, already that thickness, it is a no-op) and writes the assumption.
        thickness = dict(settings.copper_thickness_mm)
        board = entry.variant(thickness)
        self._validate_rail(board, rail, project)
        with self._solve_lock:
            if progress is not None:
                progress(f"solving {rail.net}")
            try:
                res = solve_rail(board, rail, settings, progress=progress,
                                 cancelled=cancelled or (lambda: False), project=project)
            except Cancelled:
                raise ServiceError(E.CANCELLED, f"{rail.net}: solve cancelled") from None
            except EngineError as e:
                raise ServiceError(E.SOLVE_FAILED, f"{rail.net}: {e}",
                                   "check that the sources and loads are connected by copper of "
                                   "this net" + (", and the regulators and links it feeds"
                                                 if project is not None else "")) from None
        res.assumptions.extend(notes)     # reaches the summary, the CLI text and the report
        res.warnings.extend(warnings)
        layer_thickness = {lay.name: lay.thickness_mm for lay in board.layers()}
        problems = find_problems(res, rail.limits, top=max(1, int(top)))
        with self._lock:
            self._n_results += 1
            rid = f"r{self._n_results}"
        summary = C.rail_summary(rid, entry.id, res, rail, problems, settings, thickness,
                                 layer_thickness, top=max(1, int(top)),
                                 pin_numbers=lambda pid: board.pad(pid).number,
                                 estimated=[t.name for t in res.loads[len(rail.loads):]])
        summary["warnings"] = entry.warnings() + summary["warnings"]
        summary.update({"from_board_fields": False, "overrides_board_fields": False,
                        "board_fields_warnings": [], **(fields_info or {})})
        rec = _ResultRecord(rid, entry.id, board, entry.outline(), rail, settings, dict(thickness),
                            layer_thickness, res, problems, summary, _result_nbytes(res))
        with self._lock:
            self._results[rid] = rec
            self._evict(keep=rid)
        return _Live(rec, res, board, rec.outline)

    def _evict(self, keep: Optional[str] = None) -> None:
        """Drop the arrays of the least recently used results beyond the bounds (never the
        most recently used one, nor ``keep``), keeping their summaries."""
        heavy = [r for r in self._results.values() if r.result is not None]
        total = sum(r.nbytes for r in heavy)
        for r in heavy[:-1]:
            if len(heavy) <= self.max_results and total <= self.max_result_bytes:
                break
            if r.id == keep:
                continue
            heavy.remove(r)
            total -= r.nbytes
            r.result, r.board, r.outline, r.queries, r.nbytes = None, None, None, {}, 0
        while len(self._results) > MAX_SUMMARIES:
            oldest = next(iter(self._results))
            if self._results[oldest].result is not None:
                break
            self._results.pop(oldest)

    def solve_project(self, project_path, rails: Optional[Sequence[str]] = None, top: int = 5,
                      maps_dir=None, stop_on_error: bool = False, progress: Progress = None,
                      cancelled: Cancel = None,
                      on_rail: Optional[Callable[[str, Dict[str, Any]], None]] = None,
                      on_board: Optional[Callable[[Dict[str, Any], int, int], None]] = None,
                      use_board_fields: Optional[bool] = None) -> Dict[str, Any]:
        """Solve the rails of a project file (all, or those named in ``rails``) with the
        project's settings and limits. With ``maps_dir``, write drop and current PNGs per
        layer there. A rail that fails to solve is reported in place unless
        ``stop_on_error``. ``on_board(board_info, n_rails, n_layers)`` is called once the board is open
        and the rails are chosen; ``on_rail(result_id, summary)`` after each rail, while its
        full result is still in memory.

        ``use_board_fields`` (default: the project file's ``use_board_fields``) also takes
        rails from the board's KiCad footprint fields (:mod:`coppernicus.board_fields`); a
        rail in the project file replaces the fields' rail of its net. Each summary says
        ``from_board_fields``, and its assumptions give every value with its field."""
        path, project, board_info, entry = self._open_project(project_path)
        merged = self._merge_board_fields(path, project, entry, use_board_fields)
        project = merged.project
        wanted = list(rails or [])
        selected = [r for r in project.rails if not wanted or r.net in wanted]
        if not selected:
            names = ", ".join(r.net for r in project.rails) or "none"
            hint = f"rails in the project: {names}"
            if not project.rails:
                hint += ("; give rails in the project file, or as KiCad footprint fields (PI_RAIL, "
                         "PI_SOURCE, PI_LOAD) on the parts of a .kicad_pcb board")
            raise ServiceError(E.INVALID_INPUT,
                               "No rails to solve" + (f" matching {wanted}" if wanted else ""), hint)
        if on_board is not None:
            board = entry.board
            n_layers = 0                  # copper layers the selected rails have, net ties followed
            for rail in selected:
                if rail.net not in board.nets():
                    continue
                nets = tied_nets(board, rail.net)[0] if rail.follow_net_ties else [rail.net]
                n_layers += len({lid for n in nets for lid in board.net_layers(n)})
            on_board(board_info, len(selected), n_layers)
        out_rails, verdicts = [], []
        for i, rail in enumerate(selected, start=1):
            if progress is not None:
                progress(f"rail {i}/{len(selected)}: {rail.net}")
            if not rail.loads and not (rail.sources and _feeds(entry.board, project, rail)):
                # Nothing to draw: no load of its own, and no regulator input or link it feeds
                # (or not set up at all). Skip it with a note; this is not an error and does
                # not stop the other rails.
                message = (f"{rail.net}: no loads, and it feeds no regulator input or link"
                           if rail.sources else f"{rail.net}: no source and no loads, so not solved")
                out_rails.append({"net": rail.net, **self._origin_info(merged, rail.net), "skipped": {
                    "code": E.MISSING_LOAD, "message": message,
                    "hint": "add a load, or the regulator or link it feeds, to solve it"}})
                continue
            origin = merged.origins.get(rail.net)
            info = self._origin_info(merged, rail.net)
            try:
                live = self._solve(entry, rail, project.settings, top, progress, cancelled,
                                   project=project, notes=origin.notes if origin else (),
                                   warnings=origin.warnings if origin else (), fields_info=info)
            except ServiceError as e:
                if info["from_board_fields"] and e.code != E.CANCELLED:   # say where it came from
                    parts = ", ".join((origin.board_fields or {}).get("parts", []))
                    e = ServiceError(e.code, f"{e.message} (rail {rail.net} from KiCad footprint "
                                     f"fields: check PI_RAIL and PI_PINS on {parts})", e.hint)
                if stop_on_error or e.code == E.CANCELLED:
                    raise e from None
                out_rails.append({"net": rail.net, **info, "error": e.to_dict()})
                verdicts.append("error")
                continue
            summary = dict(live.rec.summary)
            if maps_dir is not None:
                summary["maps"] = [p.as_posix() for p in save_layer_maps(
                    live.res, live.outline, Path(maps_dir), live.rec.problems)]
            if on_rail is not None:
                on_rail(live.rec.id, summary)
            out_rails.append(summary)
            verdicts.append(summary["verdict"])
        # Skipped rails (no loads) are left out of the verdict.
        unjudged_rails = any(r.get("not_judged") for r in out_rails)
        overall = ("error" if "error" in verdicts else "fail" if "fail" in verdicts else
                   "not_judged" if unjudged_rails else
                   "pass" if verdicts and all(v == "pass" for v in verdicts) else
                   "pass_partially_judged" if "pass" in verdicts else "not_judged")
        unjudged = [{"rail": r["net"], **u} for r in out_rails for u in r.get("not_judged", [])]
        note = ("fail: a limit you set is exceeded on some rail; not_judged: a limit set could "
                "not judge some items (listed in not_judged), or no limits are set; pass: every "
                "rail has limits and none is exceeded; pass_partially_judged: the rails with "
                "limits pass, others have none. Rails skipped for having nothing to draw are "
                "left out.")
        if unjudged:
            note += " Not judged: " + "; ".join(f"{u['rail']}: {u['item']} ({u['limit']}): "
                                                f"{u['reason']}" for u in unjudged)
        return C.jsonable({
            "project": path.resolve(), "board": board_info, "rails": out_rails, "verdict": overall,
            "verdict_note": note, "not_judged": unjudged, "board_fields_warnings": merged.warnings,
            "assumptions": [], "warnings": entry.warnings(),
        })

    def _open_project(self, project_path):
        """Load a project file and open its board: (path, project, board info, entry). A
        project error, or a copper_thickness_mm layer the board does not have, is one
        ServiceError for the whole project."""
        path = Path(str(project_path)).expanduser()
        try:
            project = Project.load(path)
        except FileNotFoundError:
            raise ServiceError(E.FILE_NOT_FOUND, f"No such project file: {project_path}",
                               "give an absolute path to a *.coppernicus.yml file") from None
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
            raise ServiceError(E.INVALID_PROJECT,
                               f"{path.name} is not valid YAML{where}: {getattr(e, 'problem', None) or e}",
                               "fix the project file; its format is in the README") from None
        except (ProjectError, OSError) as e:
            raise ServiceError(E.INVALID_PROJECT, f"{path.name}: {e}",
                               "fix the project file; its format is in the README") from None
        board_info = self.open_board(project.board_path())
        entry = self._entry(board_info["board_id"])
        try:                              # a wrong layer name fails the project, not each rail
            entry.variant(project.settings.copper_thickness_mm)
        except ServiceError as e:
            raise ServiceError(e.code, f"{path.name}: settings.copper_thickness_mm: {e.message}",
                               "use the copper layer names as in KiCad") from None
        return path, project, board_info, entry

    def _merge_board_fields(self, path: Path, project: Project, entry: _BoardEntry,
                            use: Optional[bool]) -> BF.Merged:
        """The project merged with the board's KiCad footprint fields (on by default: ``use``
        None follows the project's ``use_board_fields``). A board with no fields, or of
        another format than .kicad_pcb, leaves the project as it is."""
        try:
            return BF.apply(project, lambda: self._board_fields(entry), use)
        except BF.FieldError as e:
            raise ServiceError(E.INVALID_BOARD_FIELD, f"{path.name}: {e}", FIELD_HINT) from None

    def _board_fields(self, entry: _BoardEntry) -> BF.BoardFields:
        from coppernicus.engine.kicad_fields import read_board_fields
        if entry._board_fields is None:
            try:
                entry._board_fields = read_board_fields(entry.path)
            except EngineError as e:
                raise ServiceError(E.INVALID_BOARD_FIELD, str(e), FIELD_HINT) from None
        return entry._board_fields

    @staticmethod
    def _origin_info(merged: BF.Merged, net: str) -> Dict[str, Any]:
        """The summary keys that say where a rail came from."""
        origin = merged.origins.get(net)
        info: Dict[str, Any] = {
            "from_board_fields": origin is not None and origin.source == "fields",
            "overrides_board_fields": origin is not None and origin.source == "overridden",
            "board_fields_warnings": list(origin.warnings) if origin else []}
        if info["overrides_board_fields"]:
            info["board_fields_overridden"] = origin.board_fields
        return info

    def board_fields(self, board_id: str) -> Dict[str, Any]:
        """The KiCad footprint fields of an open board (``PI_*`` only), the rails and links
        they describe, each value with the field it came from. Nothing is solved."""
        entry = self._entry(board_id)
        found = self._board_fields(entry)
        try:
            listed = BF.listing(found.fields, found.pad_nets)
        except BF.FieldError as e:
            raise ServiceError(E.INVALID_BOARD_FIELD, str(e), FIELD_HINT) from None
        return C.jsonable({
            "board_id": entry.id, "fields": [{"ref": ref, "fields": dict(f)} for ref, f in
                                             sorted(found.fields.items(), key=lambda kv: BF._ref_key(kv[0]))],
            **listed,
            "assumptions": ["Fields are read from the board file's footprints, as KiCad copies "
                            "them from the symbols; only PI_* fields are listed. A rail needs a "
                            "PI_SOURCE, and a PI_LOAD or a regulator input or link it feeds; each "
                            "value carries its unit. Solves use them by default; a project-file "
                            "rail of the same net is ignored unless it sets override_board_fields."],
            "warnings": entry.warnings(),
        })

    def solve_tree(self, project_path, top: int = 5, progress: Progress = None,
                   cancelled: Cancel = None, use_board_fields: Optional[bool] = None) -> Dict[str, Any]:
        """Solve the project's whole power tree at once: every rail and ground group, with
        the regulators' input currents and the ground return computed exactly. Returns
        :func:`coppernicus.power_tree.tree_summary` unchanged (the document of
        ``coppernicus solve --tree --json``, without its ``summary_file``), judged against
        the project's limits only. The groups are not kept as results: no maps or probes."""
        from coppernicus.engine.tree import solve_project_tree
        from coppernicus.power_tree import tree_summary
        path, project, _, entry = self._open_project(project_path)
        merged = self._merge_board_fields(path, project, entry, use_board_fields)
        project = merged.project
        with self._solve_lock:
            if progress is not None:
                progress(f"solving the power tree of {path.name}")
            try:
                tree = solve_project_tree(entry.board, project, progress=progress,
                                          cancelled=cancelled or (lambda: False))
            except Cancelled:
                raise ServiceError(E.CANCELLED, f"{path.name}: solve cancelled") from None
            except EngineError as e:
                raise ServiceError(E.SOLVE_FAILED, f"{path.name}: {e}",
                                   "fix the project file (sources, loads, their ground pins, "
                                   "links), then solve again") from None
        merged.annotate(tree)
        # Through JSON as the CLI writes it: plain types, the same numbers to the last digit.
        return json.loads(json.dumps(tree_summary(tree, top=max(1, int(top))), allow_nan=False))

    # ================================================================== result queries
    def _record(self, result_id: str, heavy: bool = True) -> _ResultRecord:
        with self._lock:
            rec = self._results.get(str(result_id))
            if rec is None:
                known = ", ".join(self._results) or "none"
                raise ServiceError(E.RESULT_NOT_FOUND, f"No result with id {result_id!r}",
                                   f"solve_rail returns a result_id (results: {known})")
            if heavy and rec.result is None:
                raise ServiceError(E.RESULT_EVICTED, f"Result {result_id} was dropped from memory "
                                   "to make room for newer ones (its summary is kept)",
                                   f"solve rail {rec.rail.net} again to query its field")
            self._results.move_to_end(rec.id)
            return rec

    def _live(self, result_id: str) -> _Live:
        """A result with its arrays, board and outline bound at once: an eviction by another
        call while this one runs cannot pull them away."""
        with self._lock:
            rec = self._record(result_id)
            return _Live(rec, rec.result, rec.board, rec.outline)

    def rail_result(self, result_id: str) -> RailResult:
        """The full result object (for in-process callers such as the CLI)."""
        return self._live(result_id).res

    def rail_problems(self, result_id: str) -> List[Problem]:
        return list(self._record(result_id).problems)

    def rail_limits(self, result_id: str) -> Limits:
        """The user's limits the result was judged against."""
        return self._record(result_id, heavy=False).rail.limits

    def rail_estimated(self, result_id: str) -> List[str]:
        """The names of the result's estimated draws (regulator inputs and links its rail
        feeds), which the engine lists after the rail's own loads."""
        rec = self._record(result_id, heavy=False)
        return [d["name"] for d in rec.summary["loads"] if d.get("estimated")]

    def board_outline(self, board_id: str) -> list:
        """Board outline rings (mm), for in-process callers such as the report."""
        return self._entry(board_id).outline()

    def report(self, result_ids: Optional[Sequence[str]] = None, path=None,
               title: str = "") -> Dict[str, Any]:
        """Write an offline HTML report of results (default: every result whose fields are
        still in memory, oldest first) and return its path, size and cell sizes."""
        from coppernicus.report import RailReport, build_report
        with self._lock:
            ids = [str(r) for r in result_ids] if result_ids else [
                r.id for r in self._results.values() if r.result is not None]
        if not ids:
            raise ServiceError(E.RESULT_NOT_FOUND, "No solved result to report",
                               "solve a rail first (solve_rail or solve_project)")
        lives = [self._live(rid) for rid in ids]
        boards = list(dict.fromkeys(lv.rec.board_id for lv in lives))
        warnings = ([] if len(boards) == 1 else
                    [f"These results come from {len(boards)} boards ({', '.join(boards)}); the outline "
                     f"drawn is that of {boards[0]}."])
        board_name = lives[0].board.name if lives[0].board is not None else ""
        items = []
        for lv in lives:
            used = C.settings_to_dict(lv.rec.settings, lv.rec.thickness)
            def shown(v):
                if v is None:
                    return "engine default"
                if isinstance(v, dict):
                    return ", ".join(f"{layer} {mm:g} mm" for layer, mm in v.items())
                return f"{v:g}" if isinstance(v, float) else str(v)
            note = "Settings of this result: " + "; ".join(
                f"{k} = {shown(v)}" for k, v in used.items()) + "."
            items.append(RailReport(lv.res, lv.rec.rail.limits, estimated=self.rail_estimated(lv.rec.id),
                                    label=f"{lv.res.net} ({lv.rec.id})",
                                    notes=[note]))
        builder, text = build_report(items, title=title or f"Coppernicus report: {board_name or 'rails'}",
                                     board_name=board_name, outline=lives[0].outline)
        if path:
            target = Path(str(path)).expanduser().resolve()
        else:
            target = self.out_dir() / f"report-{time.strftime('%Y%m%d-%H%M%S')}.html"
        builder.write(target, text)
        return C.jsonable({
            "path": target, "size_mb": target.stat().st_size / 1e6,
            "rails": [{"result_id": lv.rec.id, "net": lv.res.net, "cell_mm": r["cell_mm"]}
                      for lv, r in zip(lives, builder.rails)],
            "generation_s": builder.seconds,
            "assumptions": ["The maps are sampled on square cells; the page's readout gives the values "
                            "at the centre of the cell under the pointer, from the full-resolution "
                            "solution. Summary numbers and problems use the full mesh."],
            "warnings": warnings,
        })

    def _layer(self, res: RailResult, layer: Optional[str]) -> LayerField:
        if layer is None or layer == "":
            return max(res.layers, key=lambda lf: lf.power)
        lf = res.layer(str(layer))
        if lf is None:
            raise ServiceError(E.LAYER_NOT_FOUND, f"Rail {res.net} has no copper on layer {layer!r}",
                               f"layers of this result: {', '.join(x.name for x in res.layers)}")
        return lf

    def _queries(self, live: _Live, lf: LayerField) -> Dict[str, Any]:
        """Locator and J vectors of a layer, built once (outside the lock: a large layer takes
        a while) and counted in the result's memory."""
        rec = live.rec
        with self._lock:
            q = rec.queries.get(lf.name)
        if q is not None:
            return q
        built = {"locator": TriangleLocator(lf.points, lf.tris), "j": current_vectors(lf)}
        with self._lock:
            q = rec.queries.get(lf.name)
            if q is None:
                q = built
                if rec.result is not None:            # not evicted meanwhile
                    rec.queries[lf.name] = q
                    rec.nbytes += built["locator"].nbytes + built["j"].nbytes
                    self._evict(keep=rec.id)
        return q

    def _copper(self, live: _Live, lf: LayerField):
        q = self._queries(live, lf)
        if "copper" not in q:
            q["copper"] = unary_union([live.board.copper(n, lf.layer_id) for n in live.res.nets])
        return q["copper"]

    def get_problems(self, result_id: str, limits: Optional[Mapping[str, Any]] = None,
                     top: int = 5) -> Dict[str, Any]:
        """Re-rank a result's problems against other limits, without solving again."""
        live = self._live(result_id)
        rec = live.rec
        lim = rec.rail.limits if limits is None else C.limits_from_dict(limits)
        problems = find_problems(live.res, lim, top=max(1, int(top)))
        return C.jsonable({
            "result_id": rec.id, "net": live.res.net, **C.verdict(problems, lim, live.res),
            "problems": [C.problem_dict(p, self.rail_estimated(rec.id)) for p in problems],
            "assumptions": [
                "Loads are ranked by drop at their worst pin; hotspots are connected regions in the "
                "top 1 % of a layer's copper area by |J| (or above max_current_density_a_mm2 when "
                "set), reported by their area-weighted mean |J|; vias by barrel current. With "
                "max_ground_rise_mv, one 'rise' problem gives the copper furthest from the source "
                "voltage."],
            "warnings": [],
        })

    def render_map(self, result_id: str, layer: Optional[str] = None, field: str = "drop",
                   region_mm: Optional[Sequence[float]] = None, dpi: int = 110,
                   out_dir=None) -> Dict[str, Any]:
        """A PNG map of ``field`` (drop / voltage / current) on one layer (default: the layer
        with the most copper loss), optionally zoomed to region_mm = [x_min, y_min, x_max, y_max]."""
        live = self._live(result_id)
        rec, res = live.rec, live.res
        lf = self._layer(res, layer)
        if field not in FIELDS:
            raise ServiceError(E.INVALID_INPUT, f"unknown field {field!r}",
                               f"field is one of: {', '.join(FIELDS)}")
        region = None
        if region_mm is not None:
            if len(region_mm) != 4:
                raise ServiceError(E.INVALID_INPUT, "region_mm must be [x_min, y_min, x_max, y_max]")
            x0, y0, x1, y1 = (C._number(v, "region_mm") for v in region_mm)
            if x0 == x1 or y0 == y1:
                raise ServiceError(E.INVALID_INPUT, "region_mm has no area")
            region = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        dpi = int(min(max(dpi, 50), 300))
        folder = Path(out_dir).expanduser().resolve() if out_dir else self.out_dir()
        folder.mkdir(parents=True, exist_ok=True)
        tag = "" if region is None else "_" + safe_name("_".join(f"{v:g}" for v in region))
        path = folder / f"{rec.id}_{safe_name(res.net)}_{safe_name(lf.name)}_{field}{tag}.png"
        scale = render_layer_map(res, lf, field, live.outline, path, rec.problems, region, dpi)
        key = {"mV": "scale_mv", "V": "scale_v", "A/mm²": "scale_a_mm2"}[scale["unit"]]
        return C.jsonable({
            "result_id": rec.id, "net": res.net, "layer": lf.name, "field": field,
            "path": path, "region_mm": list(region) if region else None,
            key: [scale["min"], scale["max"]], "image_px": [8 * dpi, 6 * dpi],
            "legend": "green triangles: sources; red dots: loads; crosses: current-crowding "
                      "spots on this layer; grey: board outline. y points down as in KiCad.",
            "assumptions": (["Current-density colour scale is clipped at the layer's 99.5th "
                             "percentile; point maxima at sharp corners are mesh-dependent."]
                            if field == "current" else []),
            "warnings": [],
        })

    def probe(self, result_id: str, layer: str, x_mm: float, y_mm: float) -> Dict[str, Any]:
        """Voltage, drop and current density at a point of a layer."""
        live = self._live(result_id)
        rec, res = live.rec, live.res
        lf = self._layer(res, layer)
        x, y = C._number(x_mm, "x_mm"), C._number(y_mm, "y_mm")
        q = self._queries(live, lf)
        loc, jv = q["locator"], q["j"]
        out: Dict[str, Any] = {"result_id": rec.id, "net": res.net, "layer": lf.name,
                               "x_mm": x, "y_mm": y}
        k = loc.locate(x, y)
        if k < 0:
            here = [o.name for o in res.layers
                    if o.name != lf.name and self._queries(live, o)["locator"].locate(x, y) >= 0]
            kn, dist = loc.nearest(x, y)
            near = loc.centroids[kn] if kn >= 0 else (math.nan, math.nan)
            out.update({"on_copper": False, "layers_with_copper_here": here,
                        "nearest_copper_mm": {"x_mm": near[0], "y_mm": near[1], "distance_mm": dist},
                        "assumptions": [], "warnings": [f"No solved copper of {res.net} on {lf.name} "
                                                        "at this point."]})
            return C.jsonable(out)
        w = loc.weights(k, x, y)
        v = float(w @ lf.voltage[lf.tris[k]])
        connected = math.isfinite(v)
        out.update({
            "on_copper": True, "connected": connected, "voltage_v": v,
            "drop_mv": (res.v_ref - v) * 1e3,
            "drop_pct": 100.0 * (res.v_ref - v) / abs(res.v_ref) if res.v_ref else None,
            "j_a_mm2": float(lf.current_density[k]), "j_vector_a_mm2": jv[k],
            "assumptions": ["Voltage is interpolated linearly in the mesh triangle; |J| is constant "
                            "per triangle and uniform through the copper thickness."],
            "warnings": [] if connected else ["This copper is not connected to a source."],
        })
        return C.jsonable(out)

    def cut_current(self, result_id: str, layer: str, p1_mm: Sequence[float],
                    p2_mm: Sequence[float]) -> Dict[str, Any]:
        """Copper width along the segment p1→p2 on a layer and the current through it."""
        live = self._live(result_id)
        rec, res = live.rec, live.res
        lf = self._layer(res, layer)
        p1 = [C._number(v, "p1_mm") for v in p1_mm]
        p2 = [C._number(v, "p2_mm") for v in p2_mm]
        if len(p1) != 2 or len(p2) != 2:
            raise ServiceError(E.INVALID_INPUT, "p1_mm and p2_mm are [x, y] in mm")
        if p1 == p2:
            raise ServiceError(E.INVALID_INPUT, "the two points of a cut must differ")
        q = self._queries(live, lf)
        copper = self._copper(live, lf)
        t_mm = rec.layer_thickness[lf.name]
        cut = F.cut_current(lf, q["locator"], q["j"], copper, t_mm, p1, p2)
        if cut.width_mm <= 0:
            line = LineString([p1, p2])
            crossed = [o.name for o in res.layers if o.name != lf.name
                       and not self._copper(live, o).intersection(line).is_empty]
            raise ServiceError(E.CUT_MISSES_COPPER,
                               f"The segment crosses no copper of {res.net} on {lf.name}",
                               f"layers it does cross: {', '.join(crossed)}" if crossed else
                               "it crosses no copper of this rail on any layer; check the coordinates "
                               "(mm, KiCad frame, y down)")
        warnings = []
        if cut.samples_off_mesh > 0.005 * cut.samples:
            warnings.append(f"{cut.samples_off_mesh} of {cut.samples} samples on the copper fall outside "
                            "the solved mesh (copper not part of the solve, such as an island); only "
                            "meshed copper carries current here.")
        if cut.samples_unconnected:
            warnings.append(f"{cut.samples_unconnected} samples lie on copper not connected to a source.")
        if abs(cut.meshed_width_mm - cut.width_mm) > 0.02 * cut.width_mm:
            warnings.append(f"Meshed width {cut.meshed_width_mm:.4g} mm differs from the copper width "
                            f"{cut.width_mm:.4g} mm by more than 2 %.")
        area = cut.width_mm * t_mm
        return C.jsonable({
            "result_id": rec.id, "net": res.net, "layer": lf.name, "p1_mm": p1, "p2_mm": p2,
            "length_mm": cut.length_mm, "normal": list(cut.normal),
            "copper_spans_mm": [[a, b] for a, b in cut.intervals],
            "width_mm": cut.width_mm, "thickness_mm": t_mm, "area_mm2": area,
            "current_a": cut.current_a, "abs_current_a": abs(cut.current_a),
            "mean_normal_j_a_mm2": abs(cut.current_a) / area if area else None,
            "mean_abs_j_a_mm2": cut.mean_abs_j, "max_abs_j_a_mm2": cut.max_abs_j,
            "samples": cut.samples,
            "assumptions": [
                "current_a = ∫ J·n t ds over the copper the segment crosses; positive means current "
                "flowing along 'normal', which points to the right of p1→p2 as the board is seen in "
                "KiCad (y down).",
                "J per triangle has the direction of −∇V and the engine's |J| (uniform through the "
                "copper thickness); copper width is the copper outline ∩ the segment "
                "('copper_spans_mm' are distances from p1).",
                "A cut through one layer counts only that layer's copper; current can also flow on "
                "other layers and through vias.",
            ],
            "warnings": warnings,
        })

    def compare_results(self, result_a: str, result_b: str) -> Dict[str, Any]:
        """Per-load and per-layer deltas, b − a (works on evicted results too)."""
        a, b = self._record(result_a, heavy=False), self._record(result_b, heavy=False)
        return compare_summaries(a.summary, b.summary)

    # ================================================================== misc
    def out_dir(self) -> Path:
        """Where maps go unless a folder is given: the session's folder, created on first use
        (a fresh folder under the system temp directory by default, never a board's folder)."""
        with self._lock:
            if self._out_dir is None:
                stamp = f"session-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
                self._out_dir = Path(tempfile.gettempdir()).resolve() / "coppernicus" / stamp
            self._out_dir.mkdir(parents=True, exist_ok=True)
            return self._out_dir


def compare_summaries(summary_a: Dict[str, Any], summary_b: Dict[str, Any]) -> Dict[str, Any]:
    """Per-load and per-layer deltas, b − a, of two rail summaries as built by
    :func:`coppernicus.service.convert.rail_summary` (what :meth:`Session.compare_results`
    returns for two result ids). Usable without a Session, e.g. by the app."""
    sa, sb = summary_a, summary_b
    warnings = []
    if sa["net"] != sb["net"]:
        warnings.append(f"Different rails: {sa['net']} vs {sb['net']}.")
    if sa["board_id"] != sb["board_id"]:
        warnings.append(f"Different board ids: {sa['board_id']} vs {sb['board_id']}.")
    la = {d["name"]: d for d in sa["loads"]}
    lb = {d["name"]: d for d in sb["loads"]}
    loads, differ = [], []
    for name in [*la, *[n for n in lb if n not in la]]:
        da, db = la.get(name), lb.get(name)
        estimated = bool((da or {}).get("estimated") or (db or {}).get("estimated"))
        row = {"name": name, "estimated": estimated,
               "drop_mv_a": da and da["drop_mv"], "drop_mv_b": db and db["drop_mv"],
               "drop_pct_a": da and da["drop_pct"], "drop_pct_b": db and db["drop_pct"]}
        if da and db:
            row["delta_drop_mv"] = _delta(db["drop_mv"], da["drop_mv"])
            row["delta_drop_pct"] = _delta(db["drop_pct"], da["drop_pct"])
            if estimated and not math.isclose(da["current_a"], db["current_a"], rel_tol=1e-6):
                differ.append(f"{name} ({da['current_a']:.4g} A vs {db['current_a']:.4g} A)")
        else:
            warnings.append(f"Load {name} is only in result {'a' if da else 'b'}.")
            if estimated:
                differ.append(f"{name} (only in {'a' if da else 'b'})")
        loads.append(row)
    if differ:
        warnings.append("The estimated draws differ: " + ", ".join(differ) + ". A project solve "
                        "(solve_project, the CLI, the app) draws the regulator inputs and links a "
                        "rail feeds, estimated at nominal voltages; solve_rail has no project and "
                        "draws only the loads given. The deltas mix copper and current: compare "
                        "two solves of the same kind, e.g. a project with its settings changed.")
    pa, pb = sa["loss_breakdown_w"], sb["loss_breakdown_w"]
    losses = [{"name": k, "power_w_a": pa.get(k), "power_w_b": pb.get(k),
               "delta_power_w": _delta(pb.get(k), pa.get(k))}
              for k in [*pa, *[k for k in pb if k not in pa]]]
    changed = {k: {"a": sa["settings"].get(k), "b": sb["settings"].get(k)}
               for k in sorted({*sa["settings"], *sb["settings"]})
               if sa["settings"].get(k) != sb["settings"].get(k)}
    return C.jsonable({
        "result_a": sa["result_id"], "result_b": sb["result_id"], "net_a": sa["net"], "net_b": sb["net"],
        "settings_changed": changed,
        "max_drop_mv": {"a": sa["max_drop_mv"], "b": sb["max_drop_mv"],
                        "delta": _delta(sb["max_drop_mv"], sa["max_drop_mv"])},
        "power_loss_w": {"a": sa["power_loss_w"], "b": sb["power_loss_w"],
                         "delta": _delta(sb["power_loss_w"], sa["power_loss_w"])},
        "loads": loads, "losses": losses,
        "verdict_a": sa["verdict"], "verdict_b": sb["verdict"],
        "assumptions": ["Deltas are b − a. Loads are matched by name, losses by layer name."],
        "warnings": warnings,
    })


def _feeds(board: Board, project: Project, rail: Rail) -> bool:
    """Whether the rail, solved with its project, draws something for the regulator inputs
    or links it feeds (the same nets and ties the engine uses). An estimate that cannot be
    made counts as yes, so the solve reports it as that rail's error."""
    if rail.net not in board.nets():
        return True                      # the solve says the net is not on the board
    nets = tied_nets(board, rail.net)[0] if rail.follow_net_ties else [rail.net]
    try:
        return bool(estimated_draws(project, nets, [(t.net_a, t.net_b) for t in board.net_ties()]))
    except EstimateError:
        return True


def _delta(b, a):
    if a is None or b is None:
        return None
    return b - a
