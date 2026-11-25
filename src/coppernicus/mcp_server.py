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
from pydantic import BaseModel, ConfigDict, Field

from coppernicus import __version__
from coppernicus.service import ServiceError, Session
from coppernicus.service import errors as E

log = logging.getLogger("coppernicus.mcp")

INSTRUCTIONS = """\
Coppernicus: DC power-integrity analysis (IR drop, current density) of KiCad PCBs.
Typical flow: open_board(path) -> list_nets -> net_parts(net) to choose the source (the
regulator output or supply connector, voltage_v) and the loads (ref, current_a) ->
solve_rail -> get_problems / render_map / probe / cut_current -> change settings
(via_plating_um, copper_temp_c; copper_thickness_mm one standard weight from the design at
most) and solve again -> compare_results.
From a project file: solve_project (rails one by one, with maps) or solve_tree (the whole
power tree at once, ground return included; tables only). Set up a project for the user with
write_project (read_project to see one); describe it as KiCad fields with propose_fields (the
user pastes them; KiCad files are never written). Before/after a change in KiCad:
compare_revisions. One standard copper weight up or down on a layer: copper_step.
Units are in every key name (_v, _mv, _a, _a_mm2, _mm, _w, _pct). Coordinates are mm in
KiCad's frame (y down); layer names as in KiCad. Board files are only read.
A verdict (pass/fail) exists only against limits the user gives; without limits results are
ranked, never judged: do not invent thresholds. Read 'assumptions' and 'warnings' in every
result. Errors come back as {"error": {code, message, hint}}; the hint says what to do.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITES_FILE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                              open_world_hint=False)
# Writes a project file; with overwrite: true it replaces one (its comments are not kept).
OVERWRITES_FILE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False,
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


class ThermalSpec(BaseModel):
    """Overrides of the project's ``thermal:`` block for one solve_tree (not saved)."""
    model_config = ConfigDict(extra="forbid")
    ambient_c: Optional[float] = Field(None, description="Ambient temperature, °C (default 25).")
    iterations: Optional[int] = Field(None, ge=1, description="Electro-thermal passes (default 3).")
    airflow_m_s: Optional[float] = Field(None, ge=0, description="Air speed over the board, m/s.")
    grid_mm: Optional[float] = Field(None, ge=0, description="Thermal cell size, mm; 0 = automatic. "
                                     "Coarser cells lower the cost.")
    load_heat: Optional[bool] = Field(None, description="Also the loads' own power as heat.")
    regulator_heat: Optional[Literal["output_pads", "part_pads"]] = None
    max_temp_c: Optional[float] = Field(None, description="Limit on the hottest solved copper, °C.")
    max_rise_c: Optional[float] = Field(None, description="Limit on its rise above the ambient, °C.")


class LimitsSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_drop_pct: Optional[float] = Field(None, gt=0, description="Largest allowed load drop, % "
                                                                  "of the source voltage.")
    max_current_density_a_mm2: Optional[float] = Field(None, gt=0, description="Largest allowed "
                                                       "current density, A/mm².")
    max_via_current_a: Optional[float] = Field(None, gt=0, description="Largest allowed current "
                                                                       "per via, A.")
    max_ground_rise_mv: Optional[float] = Field(None, gt=0, description="For a net at 0 V (a ground "
                                                "solved on its own: a 0 V source, its return pins "
                                                "as loads): largest allowed rise of its copper "
                                                "above 0 V, mV. On a supply rail it is not judged "
                                                "(listed in not_judged).")


class PinRefSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    net: str = Field(description="The net of these pins.")
    pins: Optional[list[str]] = Field(None, description="Pad numbers or globs; omit for every pin "
                                                        "of the part on the net.")
    ref: Optional[str] = Field(None, description="Another part (default: the terminal's own).")


class ProjectSourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: str
    voltage_v: float
    pins: Optional[list[str]] = None
    name: Optional[str] = None
    kind: Optional[Literal["source", "ldo", "smps"]] = Field(None, description="source (ideal, the "
                                                             "default), ldo or smps.")
    efficiency: Optional[float] = Field(None, description="A switcher's efficiency, a fraction 0-1.")
    quiescent_a: Optional[float] = Field(None, description="A regulator's quiescent current, A.")
    current_limit_a: Optional[float] = Field(None, gt=0, description="Its rated output current, A "
                                             "(shown next to I_out; not judged yet).")
    fsw_hz: Optional[float] = Field(None, gt=0, description="A switcher's switching frequency, Hz "
                                    "(stored; not used yet).")
    extra_pins: Optional[list[str]] = Field(None, description="More pads held at the same voltage on "
                                            "other parts, as REF.PAD (BT5.1, BT5.* for all its pads "
                                            "on the rail): one terminal, e.g. cell holders in "
                                            "parallel.")
    input: Optional[PinRefSpec] = Field(None, description="A regulator's input pins.")
    ground: Optional[PinRefSpec] = None
    sense: Optional[PinRefSpec] = None
    ground_sense: Optional[PinRefSpec] = None


class ProjectLoadSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ref: str
    current_a: float = Field(ge=0)
    pins: Optional[list[str]] = None
    name: Optional[str] = None
    ground: Optional[PinRefSpec] = Field(None, description="The pins its current returns through.")


class RailSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    net: str
    sources: list[ProjectSourceSpec] = []
    loads: list[ProjectLoadSpec] = []
    limits: Optional["LimitsSpec"] = None
    follow_net_ties: Optional[bool] = None
    override_board_fields: Optional[bool] = Field(None, description="Use this rail even where the "
                                                  "board's KiCad fields define the net.")


class LinkSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    ref: str
    a: PinRefSpec
    b: PinRefSpec
    resistance_ohm: float = Field(ge=0)


class SettingsSpec(BaseModel):
    max_edge_mm: Optional[float] = Field(None, gt=0, description="Largest mesh edge on planes, mm "
                                                                 "(default 0.5).")
    via_plating_um: Optional[float] = Field(None, gt=0, description="Plating of every plated hole, "
                                            "µm (default: the engine's table by hole type).")
    copper_temp_c: Optional[float] = Field(None, description="Copper temperature for ρ(T), °C "
                                                             "(default 20).")
    copper_thickness_mm: Optional[dict[str, float]] = Field(
        None, description="What-if copper thickness by layer name, mm, e.g. {\"B.Cu\": 0.07}. "
                          "Only a standard weight (0.5/1/2/3 oz = 0.0175/0.035/0.070/0.105 mm) "
                          "at most one step from the design's thickness on that layer; anything "
                          "else is refused (real changes are made in KiCad). copper_step does "
                          "the same from a result. The board file is not changed.")


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
    def board_fields(board_id: str) -> dict[str, Any]:
        """The board's KiCad footprint fields (PI_RAIL, PI_SOURCE, PI_LOAD, PI_PINS,
        PI_LIMIT_DROP, PI_KIND, PI_EFF, PI_IQ, PI_VOUT, PI_IMAX, PI_FSW, PI_INPUT, PI_GND,
        PI_SOURCE_REF, PI_GND_REF, PI_LINK, PI_LINK_PINS...; KiCad board files only) and the rails and
        links they describe, each value with the field it came from. Library-only parts
        (PI_KIND, PI_EFF, PI_IQ, PI_VOUT, PI_IMAX, PI_FSW without PI_RAIL) make no rail.
        Nothing is solved; solve_project and solve_tree use them by default. If you copy these
        values into solve_rail, say they come from the KiCad fields."""
        return _call("board_fields", session.board_fields, board_id)

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
                      top: Annotated[int, Field(ge=1, le=50)] = 5,
                      use_board_fields: Annotated[Optional[bool], Field(
                          description="false: ignore the board's KiCad footprint fields (PI_RAIL, "
                                      "PI_SOURCE, PI_LOAD, ...); default: the project file's "
                                      "use_board_fields, which is on.")] = None,
                      ) -> dict[str, Any]:
        """Solve the rails of a project file with its sources, loads, limits and settings
        (including its optional what-if copper_thickness_mm, named in each rail's
        assumptions). Each rail also draws the regulator inputs and links it feeds, estimated
        at nominal voltages: loads with "estimated": true. Returns one solve_rail summary
        (with result_id) per rail, a rail that fails reported in place, a rail with nothing
        to draw as skipped, and an overall verdict. Rails also come from the board's KiCad
        footprint fields (on by default), which win over a project-file rail of the same net
        unless it sets override_board_fields: each summary has from_board_fields,
        overrides_board_fields and board_fields_warnings, and its assumptions give each value
        with its field ("from KiCad field PI_LOAD ...")."""
        bridge = _Bridge(ctx)
        return _call("solve_project", session.solve_project, project_path, rails, top,
                     progress=bridge.progress, cancelled=bridge.cancelled,
                     use_board_fields=use_board_fields)

    @mcp.tool(annotations=READ_ONLY)
    def solve_tree(project_path: Annotated[str, Field(description="Absolute path to a "
                                                                  "*.coppernicus.yml project file.")],
                   ctx: Context,
                   top: Annotated[int, Field(ge=1, le=50)] = 5,
                   use_board_fields: Annotated[Optional[bool], Field(
                       description="false: ignore the board's KiCad footprint fields (PI_RAIL, "
                                   "PI_SOURCE, PI_LOAD, ...); default: the project file's "
                                   "use_board_fields, which is on.")] = None,
                   thermal: Annotated[Optional[bool], Field(
                       description="true: also solve the board temperature (electro-thermal, "
                                   "heavier); false: the plain tree; default: the project's "
                                   "thermal.enabled.")] = None,
                   thermal_options: Annotated[Optional[ThermalSpec], Field(
                       description="Overrides of the project's thermal: block for this solve "
                                   "(not saved).")] = None,
                   confirm_large: Annotated[bool, Field(
                       description="Solve even when the estimate exceeds this machine's memory "
                                   "(otherwise refused with the estimate: solve_too_large). Ask "
                                   "the user first.")] = False,
                      ) -> dict[str, Any]:
        """Solve a project's whole power tree at once: every rail and ground group together,
        with the regulators' input currents and the ground return computed exactly (when a
        ground net is solved, loads and regulators need their ground pins). Returns the VRM,
        load, link and group tables, each group's problems ("load" on a rail; "rise" and
        "return" on a ground), the verdict per group
        against the project's own limits, assumptions and warnings: the same document as
        `coppernicus solve --tree --json`. No result ids: render_map and probe do not apply
        to tree groups (solve_project per rail for maps). With the board temperature, the
        document has `thermal` (verdict, peak, per-layer rows, history, load drops at the
        ambient and electro-thermal) and `thermal_estimate` (grid, unknowns, memory against
        this machine's), given before the solve starts."""
        bridge = _Bridge(ctx)
        return _call("solve_tree", session.solve_tree, project_path, top,
                     progress=bridge.progress, cancelled=bridge.cancelled,
                     use_board_fields=use_board_fields, thermal=thermal,
                     thermal_options=(thermal_options.model_dump(exclude_none=True)
                                      if thermal_options is not None else None),
                     confirm_large=confirm_large)

    @mcp.tool(annotations=WRITES_FILE)
    def compare_revisions(result_a: Annotated[str, Field(description="The result before the change.")],
                          result_b: Annotated[str, Field(description="The result after it.")],
                          ) -> dict[str, Any]:
        """Compare two revisions of a board: results from different board files (or the same
        file edited and saved in KiCad, then opened again), whose meshes differ. Per-load drop
        deltas (matched by part), per-layer loss deltas, via currents matched by position and
        drill, where the drop grew or shrank most (B's drop mapped onto A's mesh; a PNG map per
        layer), and what could not be matched: parts moved, removed or added, a renamed net,
        copper only one of them has. Warns when the currents, sources or settings differ."""
        return _call("compare_revisions", session.compare_revisions, result_a, result_b)

    @mcp.tool(annotations=READ_ONLY)
    def copper_step(result_id: str, layer: Annotated[str, Field(description="A copper layer, e.g. B.Cu.")],
                    ctx: Context,
                    step: Annotated[Literal["up", "down"], Field(
                        description="One standard weight up or down (0.5, 1, 2, 3 oz).")] = "up",
                    ) -> dict[str, Any]:
        """What-if: re-solve a result with one copper layer ONE standard weight up or down
        (0.5 oz 0.0175 mm, 1 oz 0.035, 2 oz 0.070, 3 oz 0.105), from the design's own thickness,
        and compare it with that result (compare_results' deltas, plus the new result_id).
        Bigger jumps, a non-standard start and stepping a what-if are refused: real changes are
        made in KiCad. The stack is rebuilt, so via barrels change length too."""
        bridge = _Bridge(ctx)
        return _call("copper_step", session.copper_step, result_id, layer, step,
                     progress=bridge.progress, cancelled=bridge.cancelled)

    @mcp.tool(annotations=READ_ONLY)
    def propose_fields(project_path: Annotated[Optional[str], Field(
                           description="A *.coppernicus.yml whose rails and links to describe.")] = None,
                       result_id: Annotated[Optional[str], Field(
                           description="Or a solved rail (from solve_rail or solve_project).")] = None,
                       ) -> dict[str, Any]:
        """The KiCad PI_* field text that describes a project's rails and links (or one solved
        rail), part by part: library_symbol fields (PI_KIND, PI_EFF, PI_IQ, PI_IMAX, PI_FSW)
        and schematic_instance fields (PI_RAIL, PI_SOURCE, PI_SOURCE_REF, PI_LOAD, PI_PINS,
        PI_INPUT, PI_GND, PI_LINK, PI_LINK_PINS...), ';' between rails by position. Give the
        user this text to paste; nothing
        is written to KiCad files. not_expressed lists what fields cannot carry."""
        return _call("propose_fields", session.propose_fields, project_path, result_id)

    @mcp.tool(annotations=READ_ONLY)
    def read_project(project_path: Annotated[str, Field(description="Absolute path to a "
                                                                    "*.coppernicus.yml project file.")]
                     ) -> dict[str, Any]:
        """A project file as written (rails, sources, loads, limits, links, settings; unknown
        keys included), checked by the project loader, with the board it names."""
        return _call("read_project", session.read_project, project_path)

    @mcp.tool(annotations=OVERWRITES_FILE)
    def write_project(project_path: Annotated[str, Field(description="Absolute path of the "
                                                                     "*.coppernicus.yml to write.")],
                      board_path: Annotated[Optional[str], Field(
                          description="The board the project is for (a new project needs it).")] = None,
                      rails: Annotated[Optional[list[RailSpec]], Field(
                          description="Rails to add, or to replace whole (by net).")] = None,
                      remove_rails: Annotated[Optional[list[str]], Field(
                          description="Nets of rails to remove.")] = None,
                      links: Annotated[Optional[list[LinkSpec]], Field(
                          description="Links (ferrite, sense resistor, fuse) to add or replace "
                                      "(by name).")] = None,
                      remove_links: Annotated[Optional[list[str]], Field(
                          description="Names of links to remove.")] = None,
                      settings: Annotated[Optional[dict[str, Any]], Field(
                          description="Settings keys to set (max_edge_mm, via_plating_um, copper_temp_c, "
                                      "copper_thickness_mm); null removes one (its default). "
                                      "copper_thickness_mm merges by layer (a layer set to null "
                                      "goes back to the board file); each layer changed takes a "
                                      "standard weight at most one step from the board file's "
                                      "thickness.")] = None,
                      use_board_fields: Annotated[Optional[bool], Field(
                          description="false: ignore the board's KiCad footprint fields.")] = None,
                      from_project: Annotated[Optional[str], Field(
                          description="Start from this project file (to save an edited copy).")] = None,
                      overwrite: Annotated[bool, Field(description="Replace an existing file.")] = False,
                      ) -> dict[str, Any]:
        """Create or edit a project file for the user and save it to project_path. Starts from
        from_project, else the existing file at project_path, else an empty project for
        board_path. Rails are replaced whole by net. An existing file is replaced only with
        overwrite: true; the file is rewritten from its data (comments are lost, unknown keys
        kept) and checked by the project loader before it is replaced. The board is only
        read. A warning says when the board's KiCad fields define a rail you wrote (they win:
        set override_board_fields: true on it to use yours)."""
        return _call("write_project", session.write_project, project_path, board_path,
                     [r.model_dump(exclude_none=True) for r in rails] if rails is not None else None,
                     remove_rails,
                     [k.model_dump(exclude_none=True) for k in links] if links is not None else None,
                     remove_links, settings, use_board_fields, from_project, overwrite)

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
