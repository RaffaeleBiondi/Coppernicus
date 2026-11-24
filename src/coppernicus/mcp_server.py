"""The MCP server: Coppernicus as tools for AI agents (Claude Code, Claude Desktop, any MCP
client), over stdio. Started by ``coppernicus mcp``.

Thin wrappers over :class:`coppernicus.service.Session`, built on the official ``mcp`` SDK
(v2, ``MCPServer``). Every tool returns the session's JSON (units in every key name, with
``assumptions`` and ``warnings``); a failure comes back as a tool error whose text is
``{"error": {"code", "message", "hint"}}``. ``render_map`` also returns the PNG as an image.

stdio hygiene: stdout is the protocol. Nothing here prints; logging (including the engine's
``kipi`` logger and Python warnings) goes to stderr or to ``--log-file``.

No ``from __future__ import annotations`` here: the SDK reads the tools' type hints, and
the parameter models must be real classes at definition time.
"""

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

import anyio.from_thread
from mcp.server import MCPServer
from mcp.server.mcpserver import Context, Image
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from coppernicus import __version__
from coppernicus.service import ServiceError, Session
from coppernicus.service import errors as E

log = logging.getLogger("coppernicus.mcp")

INSTRUCTIONS = """\
Coppernicus: DC power-integrity analysis (IR drop, current density) of KiCad PCBs.
Typical flow: open_board(path) -> list_nets -> net_parts(net) to choose the source (the
regulator output or supply connector, voltage_v) and the loads (ref, current_a) ->
solve_rail -> get_problems / render_map / probe / cut_current -> change settings
(copper_thickness_mm, via_plating_um, copper_temp_c) and solve again -> compare_results.
Units are in every key name (_v, _mv, _a, _a_mm2, _mm, _w, _pct). Coordinates are mm in
KiCad's frame (y down); layer names as in KiCad. Board files are only read.
A verdict (pass/fail) exists only against limits the user gives; without limits results are
ranked, never judged: do not invent thresholds. Read 'assumptions' and 'warnings' in every
result. Errors come back as {"error": {code, message, hint}}; the hint says what to do.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITES_FILE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                              open_world_hint=False)


class SourceSpec(BaseModel):
    ref: str = Field(description="Part reference, e.g. a regulator 'U3' or a connector 'J1'.")
    voltage_v: float = Field(description="Voltage held at its pins, V.")
    pins: Optional[list[str]] = Field(None, description="Pad numbers or globs such as 'A*'; omit "
                                                        "for every pin of the part on the net.")
    name: str = Field("", description="Optional label.")


class LoadSpec(BaseModel):
    ref: str = Field(description="Part reference, e.g. 'U1'.")
    current_a: float = Field(ge=0, description="Total current drawn, A, split equally across its pins.")
    pins: Optional[list[str]] = Field(None, description="Pad numbers or globs such as 'A*'; omit "
                                                        "for every pin of the part on the net.")
    name: str = Field("", description="Optional label (default: the reference).")


class LimitsSpec(BaseModel):
    max_drop_pct: Optional[float] = Field(None, gt=0, description="Largest allowed load drop, % "
                                                                  "of the source voltage.")
    max_current_density_a_mm2: Optional[float] = Field(None, gt=0, description="Largest allowed "
                                                       "current density, A/mm².")
    max_via_current_a: Optional[float] = Field(None, gt=0, description="Largest allowed current "
                                                                       "per via, A.")
    max_ground_rise_mv: Optional[float] = Field(None, gt=0, description="For a ground net solved on "
                                                "its own (a 0 V source, its return pins as loads): "
                                                "largest allowed rise of its copper above the "
                                                "source, mV.")


class SettingsSpec(BaseModel):
    max_edge_mm: Optional[float] = Field(None, gt=0, description="Largest mesh edge on planes, mm "
                                                                 "(default 0.5).")
    via_plating_um: Optional[float] = Field(None, gt=0, description="Plating of every plated hole, "
                                            "µm (default: the engine's table by hole type).")
    copper_temp_c: Optional[float] = Field(None, description="Copper temperature for ρ(T), °C "
                                                             "(default 20).")
    copper_thickness_mm: Optional[dict[str, float]] = Field(
        None, description="What-if copper thickness by layer name, mm (1 oz = 0.035), e.g. "
                          "{\"B.Cu\": 0.07}. The board file is not changed.")


def _error_result(err: dict) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=json.dumps({"error": err},
                                                                         ensure_ascii=False))],
                          structured_content={"error": err}, is_error=True)


def _call(name: str, fn, *args, **kwargs):
    """Run a session method; a ServiceError (or a bug) becomes a structured tool error."""
    try:
        return fn(*args, **kwargs)
    except ServiceError as e:
        log.info("%s: %s: %s", name, e.code, e.message)
        return _error_result(e.to_dict())
    except Exception as e:      # a bug: say so, keep the traceback in the log
        log.exception("%s failed", name)
        return _error_result({"code": E.INTERNAL_ERROR, "message": f"{type(e).__name__}: {e}",
                              "hint": "this is a bug in Coppernicus; the server log has the traceback"})


class _Bridge:
    """Engine progress -> MCP progress notifications, and MCP cancellation -> the engine's
    cancel check. Used from the worker thread a plain ``def`` tool runs in."""

    def __init__(self, ctx: Context):
        self.ctx, self.n = ctx, 0

    def progress(self, text: str) -> None:
        self.n += 1                         # progress must increase with every report
        log.debug("progress: %s", text)
        try:
            anyio.from_thread.run(self.ctx.report_progress, self.n, None, text)
        except Exception:                   # no client listening, or the call is over
            pass

    def cancelled(self) -> bool:
        try:
            anyio.from_thread.check_cancelled()
        except RuntimeError:                # not in an anyio worker thread: never cancelled
            return False
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:               # the backend's cancellation exception
            return True
        return False


def _dump(model: Optional[BaseModel]) -> Optional[dict]:
    return None if model is None else model.model_dump(exclude_none=True)


def build_server(session: Optional[Session] = None, log_level: str = "INFO") -> MCPServer:
    """The MCP server with Coppernicus's tools, over ``session`` (a new one by default)."""
    session = session or Session()
    mcp = MCPServer("coppernicus", title="Coppernicus", version=__version__,
                    instructions=INSTRUCTIONS, log_level=log_level)

    @mcp.tool(annotations=READ_ONLY)
    def open_board(path: Annotated[str, Field(description="Absolute path to a .kicad_pcb (or ODB++, "
                                                          "HyperLynx, KiPI snapshot) file.")]
                   ) -> dict[str, Any]:
        """Open a board read-only. Returns board_id, copper layers with thickness, counts,
        outline bounding box and import warnings. Reopening an unchanged file returns the
        same board_id."""
        return _call("open_board", session.open_board, path)

    @mcp.tool(annotations=READ_ONLY)
    def list_nets(board_id: str,
                  kind: Literal["power_or_ground", "power", "ground", "signal", "all"] = "power_or_ground",
                  filter: Annotated[Optional[str], Field(
                      description="Case-insensitive substring, or a glob over whole names "
                                  "such as '+3V*'.")] = None,
                  limit: Annotated[int, Field(ge=1, le=5000)] = 200) -> dict[str, Any]:
        """Nets of an open board with their kind (a name-and-zone heuristic) and the copper
        layers each one is on."""
        return _call("list_nets", session.list_nets, board_id, kind, filter, limit)

    @mcp.tool(annotations=READ_ONLY)
    def net_parts(board_id: str, net: str) -> dict[str, Any]:
        """Parts on a net with value, footprint and pins (to choose sources and loads), and
        the net ties that join it to other nets (solved as shorts)."""
        return _call("net_parts", session.net_parts, board_id, net)

    @mcp.tool(annotations=READ_ONLY)
    def solve_rail(board_id: str, net: str, sources: list[SourceSpec], loads: list[LoadSpec],
                   ctx: Context, limits: Optional[LimitsSpec] = None,
                   settings: Optional[SettingsSpec] = None, follow_net_ties: bool = True,
                   top: Annotated[int, Field(ge=1, le=50, description="Hotspots and vias to rank.")] = 5
                   ) -> dict[str, Any]:
        """Solve the DC voltage drop and current density of one power net. Sources hold
        voltage_v on their pins; loads draw current_a split equally across their pins.
        Returns result_id and a summary: load voltages and drops, max drop, loss per layer,
        busiest layer, top problems, verdict (only against the limits given), assumptions
        and warnings. No project: only the loads given are drawn (solve_project also draws
        the regulator inputs and links a rail feeds, estimated)."""
        bridge = _Bridge(ctx)
        return _call("solve_rail", session.solve_rail, board_id, net,
                     [s.model_dump() for s in sources], [ld.model_dump() for ld in loads],
                     _dump(limits), _dump(settings), follow_net_ties, top,
                     progress=bridge.progress, cancelled=bridge.cancelled)

    @mcp.tool(annotations=READ_ONLY)
    def get_problems(result_id: str, limits: Optional[LimitsSpec] = None,
                     top: Annotated[int, Field(ge=1, le=50)] = 5) -> dict[str, Any]:
        """Ranked problems of a result (loads by drop, current-crowding spots by mean |J|,
        vias by current), each with layer and x/y in mm. Pass limits to re-judge without
        solving again (default: the limits given at solve time)."""
        return _call("get_problems", session.get_problems, result_id,
                     None if limits is None else _dump(limits), top)

    @mcp.tool(annotations=WRITES_FILE)
    def render_map(result_id: str, layer: Annotated[Optional[str], Field(
                       description="Copper layer name; default: the layer with the most "
                                   "copper loss.")] = None,
                   field: Literal["drop", "voltage", "current"] = "drop",
                   region_mm: Annotated[Optional[list[float]], Field(
                       min_length=4, max_length=4,
                       description="Zoom to [x_min, y_min, x_max, y_max], mm.")] = None,
                   dpi: Annotated[int, Field(ge=50, le=300)] = 110) -> CallToolResult:
        """A colour map of one layer of a result as a PNG image (also saved; the path is
        returned): voltage drop (mV), voltage (V) or current density (A/mm²), with sources,
        loads and current-crowding spots marked."""
        out = _call("render_map", session.render_map, result_id, layer, field, region_mm, dpi)
        if isinstance(out, CallToolResult):
            return out
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(out, ensure_ascii=False)),
                                       Image(path=out["path"]).to_image_content()],
                              structured_content=out)

    @mcp.tool(annotations=READ_ONLY)
    def probe(result_id: str, layer: str, x_mm: float, y_mm: float) -> dict[str, Any]:
        """Voltage, drop and current density (magnitude and vector) at a point of a layer.
        Off copper, says which layers have copper there and where the nearest copper is."""
        return _call("probe", session.probe, result_id, layer, x_mm, y_mm)

    @mcp.tool(annotations=READ_ONLY)
    def cut_current(result_id: str, layer: str, x1_mm: float, y1_mm: float, x2_mm: float,
                    y2_mm: float) -> dict[str, Any]:
        """Cross-section along the segment (x1, y1)→(x2, y2) on one layer: copper width
        crossed, current through it (∫ J·n t ds, signed along 'normal'), mean and max |J|.
        Use it to measure a neck or bottleneck."""
        return _call("cut_current", session.cut_current, result_id, layer, [x1_mm, y1_mm],
                     [x2_mm, y2_mm])

    @mcp.tool(annotations=READ_ONLY)
    def compare_results(result_a: str, result_b: str) -> dict[str, Any]:
        """What-if deltas b − a between two results: settings changed, max drop, loss, drop per
        load and loss per layer, and both verdicts."""
        return _call("compare_results", session.compare_results, result_a, result_b)

    @mcp.tool(annotations=READ_ONLY)
    def solve_project(project_path: Annotated[str, Field(description="Absolute path to a "
                                                                     "*.coppernicus.yml project file.")],
                      ctx: Context,
                      rails: Annotated[Optional[list[str]], Field(
                          description="Nets to solve; default all.")] = None,
                      top: Annotated[int, Field(ge=1, le=50)] = 5) -> dict[str, Any]:
        """Solve the rails of a project file with its sources, loads, limits and settings
        (including its optional what-if copper_thickness_mm, named in each rail's
        assumptions). Each rail also draws the regulator inputs and links it feeds, estimated
        at nominal voltages: loads with "estimated": true. Returns one solve_rail summary
        (with result_id) per rail, a rail that fails reported in place, a rail with nothing
        to draw as skipped, and an overall verdict."""
        bridge = _Bridge(ctx)
        return _call("solve_project", session.solve_project, project_path, rails, top,
                     progress=bridge.progress, cancelled=bridge.cancelled)

    @mcp.tool(annotations=WRITES_FILE)
    def report(result_ids: Annotated[Optional[list[str]], Field(
                   description="Results to include; default: every result still in memory.")] = None,
               path: Annotated[Optional[str], Field(
                   description="Absolute path of the .html file; default: the server's maps folder.")] = None
               ) -> dict[str, Any]:
        """Write an offline, interactive HTML report of solved results for a person to open
        in a browser: every layer's drop, voltage and current-density map with zoom, pan and
        a hover readout, the problems, worst values, assumptions and warnings. Returns its
        path and size; the file needs no network."""
        return _call("report", session.report, result_ids, path)

    return mcp


def _configure_logging(log_file: Optional[str], level: str) -> None:
    """Everything to stderr (or a file), never stdout: stdout is the protocol."""
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    if log_file:
        logging.basicConfig(filename=log_file, level=level, format=fmt, encoding="utf-8")
    else:
        logging.basicConfig(stream=sys.stderr, level=level, format=fmt)
    logging.captureWarnings(True)


def run_stdio(out_dir=None, log_file: Optional[str] = None, log_level: str = "INFO",
              max_results: int = 8, max_result_mb: float = 2048) -> int:
    """Serve over stdio until the client disconnects. Maps go to a new ``session-…`` folder
    inside ``out_dir``: result ids restart at r1 in every server, so sessions must not share
    a folder."""
    _configure_logging(log_file, log_level)
    if out_dir:
        out_dir = Path(out_dir).expanduser() / f"session-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    session = Session(out_dir=out_dir, max_results=max_results, max_result_mb=max_result_mb)
    server = build_server(session, log_level)
    log.info("Coppernicus %s MCP server on stdio; maps go to %s", __version__, session.out_dir())
    server.run()
    return 0
