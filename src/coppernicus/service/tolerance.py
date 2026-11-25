"""The tolerance stack-up's run (Task 11), shared by ``coppernicus tolerance`` and
:meth:`coppernicus.service.Session.solve_tolerance`, so both give the same document.

:func:`plan` reads what the project file sets beyond what loading keeps (a misspelt
tolerance key; the tolerances a KiCad-fields rail carries by ref, and those it loses), decides
which AC and noise solves the loads with a tolerance need and sizes each of them (and the board
temperature) before anything heavy runs. :func:`solve` then solves the power tree, those AC and
noise solves, and puts the stack-up together (:func:`coppernicus.tolerance.compose`, whose
judgement this module never touches). Rules: ``docs/engine-mapping.md`` (Task 11).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from coppernicus import board_fields
from coppernicus.project import LOAD_TOLERANCE_KEYS, SOURCE_ACCURACY_KEYS, Load, Project

Progress = Optional[Callable[[str], None]]
Cancel = Optional[Callable[[], bool]]
#: Told each solve's cost as soon as it is known: ``said(kind, name, estimate)``, ``kind``
#: "thermal" (name "board temperature"), "ac" (a rail's net) or "noise" (an aggressor).
Said = Optional[Callable[[str, str, Dict[str, Any]], None]]


def project_inputs(project_file, loaded: Project, merged: board_fields.Merged
                   ) -> Tuple[List[str], List[Dict[str, Any]], Dict[str, str]]:
    """``(warnings, not-judged entries, lost accuracy)`` from what the project file sets beyond
    what loading keeps: the KiCad-field warnings, a misspelt tolerance key (never a silent
    pass), and for each rail taken from the board's KiCad fields the project's tolerances,
    accuracy and entered parts that no part of the fields' rail takes
    (:func:`coppernicus.board_fields.carried_by_ref`): a warning, each lost tolerance a
    not-judged entry, and per rail net why the accuracy entered for its source was lost."""
    from coppernicus.tolerance import misspelt_keys
    project = merged.project
    warnings, dropped, lost_accuracy = list(merged.warnings), [], {}
    try:                                  # a misspelt tolerance key must never end in a silent pass
        with open(project_file, encoding="utf-8") as fh:
            typos, dropped = misspelt_keys(yaml.safe_load(fh))
        warnings += typos
    except (OSError, yaml.YAMLError):     # Project.load read it a moment ago
        pass
    for net in merged.from_fields:        # the fields' rail keeps by ref what the project gives
        own, kept = loaded.rail(net), project.rail(net)
        if own is None:
            continue
        sides = ((kept.sources if kept else [], own.sources, SOURCE_ACCURACY_KEYS),
                 (kept.loads if kept else [], own.loads, board_fields.TOLERANCE_KEYS))
        lost = [t for ts, own_ts, keys in sides for t in board_fields.carried_by_ref(ts, own_ts, keys)[1]]
        if not lost:
            continue
        others = ", ".join(s.label for s in kept.sources) if kept is not None and kept.sources else "none"
        names = ", ".join(t.label for t in lost)
        warnings.append(f"Rail {net} comes from the board's KiCad footprint fields: the tolerances, accuracy "
                        f"and entered parts of {names} in the project file are not used (no part of the "
                        "fields' rail takes them); set override_board_fields: true on the rail to use the "
                        "project's")
        # A tolerance the user set that cannot be judged: not judged (exit 3), never a pass.
        dropped += [{"item": t.label, "net": net, "limit": key,
                     "reason": f"rail {net} comes from the board's KiCad footprint fields, where no part "
                               f"takes {t.label}'s tolerances: set override_board_fields: true on the rail "
                               "to use the project's"}
                    for t in lost if isinstance(t, Load) for key in LOAD_TOLERANCE_KEYS
                    if getattr(t, key) is not None]
        for t in lost:                    # an accuracy entered, then lost: say so, not "enter it"
            key = next((k for k in SOURCE_ACCURACY_KEYS if getattr(t, k, None) is not None), None)
            if key is not None:
                lost_accuracy[net] = (f"{t.label}'s {key} in the project file was lost: rail {net} comes "
                                      f"from the board's KiCad footprint fields, whose source is {others} "
                                      "(set override_board_fields: true on the rail to use the project's, "
                                      "or give that source its accuracy in KiCad field PI_ACC)")
    return warnings, dropped, lost_accuracy


@dataclass
class Plan:
    """What a tolerance run solves, each solve sized before any runs."""
    project: Project                      # merged with the board's KiCad fields
    merged: board_fields.Merged
    warnings: List[str]
    dropped: List[Dict[str, Any]]         # not-judged entries: misspelt keys, lost tolerances
    lost_accuracy: Dict[str, str]
    pairs: Dict[str, Optional[str]]       # engine load name -> its probe
    rails: List[str]                      # AC solves, for KiPI's transient estimate (shown)
    aggressors: List[str]                 # noise solves, for the ripple
    probes_on: Dict[str, List[str]]       # rail -> the probes of loads judged on it
    ac_notes: Dict[str, str]
    noise_notes: Dict[str, str]
    #: Each solve's cost as the CLI prints it, ``solve`` "board temperature", "AC <net>" or
    #: "noise <name>", then the estimate's own keys (``exceeds_memory``, ``text``…).
    estimates: List[Dict[str, Any]]
    started: float

    def too_large(self) -> List[Dict[str, Any]]:
        """The estimates that exceed this machine's memory."""
        return [e for e in self.estimates if e.get("exceeds_memory")]


def plan(board, project_file, loaded: Project, merged: board_fields.Merged, skip_ac: bool = False,
         skip_noise: bool = False, progress: Progress = None, cancelled: Cancel = None,
         said: Said = None, started: Optional[float] = None) -> Plan:
    """What :func:`solve` will solve, each cost estimated first (the board temperature when
    ``thermal.enabled``; one AC solve per rail where a load with a tolerance has a probe with
    a step and a slew whose source has a bandwidth given; one noise solve per aggressor listing
    such a probe), with the estimates :mod:`coppernicus.service.session` gives the CLI, the app
    and MCP. ``skip_ac`` / ``skip_noise`` leave those out (the hints name the flag). An AC or
    noise solve that cannot even be sized is left out with a warning, as one that fails: only
    its part is lost. Raises :class:`~coppernicus.engine.EngineError` when the loads cannot be
    paired with their probes."""
    from coppernicus.engine import EngineError
    from coppernicus.engine.ac import probe_rails
    from coppernicus.engine.tolerance import load_probes, loads_by_name
    from coppernicus.service.session import ac_estimate, noise_estimate, thermal_estimate
    started = time.perf_counter() if started is None else started
    project = merged.project
    warnings, dropped, lost_accuracy = project_inputs(project_file, loaded, merged)
    estimates: List[Dict[str, Any]] = []

    def sized(kind: str, name: str, solve: str, estimate: Dict[str, Any]) -> None:
        estimates.append({"solve": solve, **estimate})
        if said is not None:
            said(kind, name, estimate)
    if project.thermal.enabled:           # before a heavy electro-thermal tree
        sized("thermal", "board temperature", "board temperature", thermal_estimate(board, project.thermal))
    pairs = load_probes(board, project)
    rails_of = probe_rails(board, project)
    needed = {pairs[n] for n, (_net, ld) in loads_by_name(project).items()
              if ld.has_tolerance() and pairs.get(n)}             # the probes of loads judged
    ac_notes: Dict[str, str] = {}
    noise_notes: Dict[str, str] = {}
    rails: List[str] = []
    for p in project.probes:              # AC only for KiPI's estimate: a step and a bandwidth given
        rail = rails_of.get(p.name)
        if not (p.step_a and p.slew_a_per_us) or rail is None:
            continue
        if p.name not in needed:
            ac_notes[p.name] = "not solved in AC, as no load with a tolerance needs it"
            continue
        source = project.rail(rail).sources[0]
        if project.ac.bandwidth_hz.get(source.label, project.ac.bandwidth_hz.get(source.ref)) is None:
            continue
        if skip_ac:
            ac_notes[p.name] = "--skip-ac left it out"
        elif rail not in rails:
            rails.append(rail)
    probes_on = {net: [n for n in needed if rails_of.get(n) == net] for net in rails}
    for net in list(rails):               # its cost first: a heavy solve is never a surprise
        try:
            sized("ac", net, f"AC {net}", ac_estimate(board, project, net, progress, cancelled))
        except EngineError as e:          # only KiPI's estimate is lost: shown, never judged
            rails.remove(net)
            warnings.append(f"AC solve of {net} for KiPI's transient estimate failed: {e}")
            ac_notes.update({n: f"the AC solve of {net} failed" for n in probes_on[net]})
    aggressors: List[str] = []
    for a in project.aggressors:
        if not set(a.ripple_probes) & needed:
            noise_notes[a.name] = "no load with a tolerance needs it"
            continue
        if skip_noise:
            noise_notes[a.name] = "--skip-noise"
            continue
        try:                              # its cost first, as coppernicus noise
            sized("noise", a.name, f"noise {a.name}",
                  noise_estimate(board, project, a.name, progress, cancelled, merged.from_fields))
            aggressors.append(a.name)
        except EngineError as e:          # its ripple is missing: not judged, never a pass
            warnings.append(f"Noise solve of {a.name} failed: {e}")
            noise_notes[a.name] = "its noise solve failed"
    return Plan(project, merged, warnings, dropped, lost_accuracy, pairs, rails, aggressors, probes_on,
                ac_notes, noise_notes, estimates, started)


def solve(board, plan: Plan, progress: Progress = None, cancelled: Cancel = None):
    """The power tree (the DC drop on the worst pins), the planned AC solves (KiPI's transient
    estimate, shown, never judged) and noise solves (the ripple), then each load's stack-up
    (:func:`coppernicus.tolerance.compose`), with the KiCad-field notes, the estimates and the
    stats. A failed AC solve only loses KiPI's estimate (a warning); a failed noise solve leaves
    the ripple missing (a warning; not judged). Raises
    :class:`~coppernicus.engine.EngineError` when the power tree cannot be solved, and
    :class:`~coppernicus.engine.Cancelled` when ``cancelled()`` turns true."""
    from coppernicus.engine import EngineError
    from coppernicus.engine.ac import peak_rss_bytes, solve_ac_rail
    from coppernicus.engine.noise import solve_aggressor
    from coppernicus.engine.tree import solve_project_tree
    from coppernicus.tolerance import compose
    cancelled = cancelled or (lambda: False)
    project, merged = plan.project, plan.merged
    warnings, ac_notes, noise_notes = list(plan.warnings), dict(plan.ac_notes), dict(plan.noise_notes)
    tree = solve_project_tree(board, project, progress=progress, cancelled=cancelled)
    impedance = []
    for net in plan.rails:
        try:
            impedance.append(solve_ac_rail(board, project, net, progress=progress, cancelled=cancelled,
                                           transient=True))
        except EngineError as e:          # only KiPI's estimate is lost: shown, never judged
            warnings.append(f"AC solve of {net} for KiPI's transient estimate failed: {e}")
            ac_notes.update({n: f"the AC solve of {net} failed" for n in plan.probes_on.get(net, [])})
    noise = []
    for name in plan.aggressors:
        try:
            res = solve_aggressor(board, project, name, progress=progress, cancelled=cancelled,
                                  fields_rails=merged.from_fields)
        except EngineError as e:          # its ripple is missing: not judged, never a pass
            warnings.append(f"Noise solve of {name} failed: {e}")
            noise_notes[name] = "its noise solve failed"
            continue
        merged.annotate(res)
        noise.append(res)
    merged.annotate(tree)
    result = compose(project, tree, impedance, noise, plan.pairs, ac_notes, noise_notes, warnings,
                     plan.dropped, plan.lost_accuracy)
    merged.annotate(result)               # the KiCad-field notes, as the other commands
    result.estimates = [dict(e) for e in plan.estimates]
    peak = peak_rss_bytes()
    result.stats = {"seconds": time.perf_counter() - plan.started, "tree_seconds": tree.stats.get("seconds"),
                    "ac_rails": len(impedance), "aggressors": len(noise),
                    "memory_peak_mb": peak / 1e6 if peak else None}
    return result


def too_large_hint(big: List[Dict[str, Any]]) -> str:
    """What to do about the estimates over this machine's memory (``Plan.too_large``)."""
    cures = []
    kinds = [e["solve"].split(" ", 1)[0] for e in big]
    if "board" in kinds:
        cures.append("a coarser thermal grid (the project's thermal: grid_mm)")
    if "AC" in kinds:
        cures.append("a coarser AC mesh (the project's ac: max_edge_mm), or skip_ac: true (KiPI's "
                     "transient estimate, never judged, is then left out)")
    if "noise" in kinds:
        cures.append("a coarser AC mesh (ac: max_edge_mm) or fewer field solves (noise: field_per_decade), "
                     "or skip_noise: true (the ripple is then missing: not judged)")
    return "pass confirm_large: true to solve it anyway (ask the user first), or " + "; or ".join(cures)
