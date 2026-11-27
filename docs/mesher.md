# The mesher

Every solve first cuts the copper into small triangles (a mesh), then solves the field on them.
Coppernicus meshes with its own mesher, **rust** (the `coppermesh` package, in
[tools/mesher_rs](../tools/mesher_rs/README.md)), by default. **Triangle**, the mesher KiPI was
written with, stays available: choose it and it meshes as KiPI always did.

The two give the same verdicts on every example in this repository, and their numbers agree to
the mesh's own accuracy (see [How the two compare](#how-the-two-compare)).

## Choose it

- **App:** the **Mesher** box at the top of the setup panel. It applies to every solve and is
  kept for the next start. A project's `settings.mesher` wins: the box then shows it, locked.
- **Command line:** `--mesher rust` or `--mesher triangle` on `solve` (rails, `--tree`,
  `--thermal`), `ac`, `noise` and `tolerance`.
- **Project file:** `settings: mesher: triangle` (or `rust`). Saved only when set.
- **Agents:** the `mesher` argument of the six solve tools (`solve_rail`, `solve_project`,
  `solve_tree`, `solve_ac`, `solve_noise`, `solve_tolerance`); see
  [Agents](agents.md#choosing-the-mesher).

The order is: the command's `--mesher` or the tool's `mesher`, else the project's
`settings.mesher`, else the app's box, else rust.

Every result records its mesher: `mesher` (`name`, `version`, `default`) in the JSON summary,
the status bar and the HTML report. A result meshed with Triangle by choice says so in its
assumptions. `compare_results` and `compare_revisions` warn when two results were meshed
differently: small differences between them are then the mesh, not the design. A summary saved
before the mesher was recorded reads as Triangle.

## Where the rust mesher is not installed

The packaged app has it built in. A Python install builds it only with the `rust` dependency
group, which needs a Rust toolchain ([rustup.rs](https://rustup.rs)):

    uv sync --group rust

Without it (a plain `uv sync`, a `pip install` from git, the GitHub Action), a solve that names no
mesher meshes with Triangle and says so in its `warnings` (`mesher.fallback_from: "rust"`). A
solve that names `rust` stops with the error `mesher_unavailable` and how to install it.

## Triangle

[Triangle](https://www.cs.cmu.edu/~quake/triangle.html), by Jonathan Richard Shewchuk, is
KiPI's own mesher. Its licence does not allow it to be bundled in a distributed app, so:

- in a Python install it comes with KiPI, as KiPI declares it;
- the packaged app does not contain it. It is downloaded from PyPI, with its licence note, only
  when you choose it (the **Mesher** box, `--mesher triangle`, or `--fetch-triangle`), and
  checked against the version Coppernicus pins. Help → **Remove downloaded mesher** deletes it.
  See [Installing the packaged app](install.md#the-triangle-mesher).

Without Triangle, everything except a solve that chooses it works.

## The AC mesh edge

The AC impedance, the noise and the tolerance stack-up mesh the copper with their own largest
edge, `ac: max_edge_mm`. When the project does not set it, it depends on the mesher:

| Mesher | `ac.max_edge_mm` when unset |
|---|---|
| rust | 0.5 mm |
| Triangle | 1.0 mm (KiPI's) |

At 1.0 mm both meshers put only one or two triangles across a narrow trace, and where KiPI then
places the current of each of its 0.5 mm cells depends on the mesh. For a victim trace running
0.2 mm from a hot loop (the noise example's `SIG_LOOP`) that left rust's coupled noise 3.6 % low
and Triangle's 0.5 % high against the value both converge to when refined, so rust's default is
finer.

- A value set in the project wins, for either mesher, and is kept when the project is saved.
- The results say which edge was used and why: `ac.max_edge_mm_used` and `ac.max_edge_mm_from`
  (`"rust default"`, `"triangle default"` or `"project"`), and an assumption line.
- With rust, a value set coarser than 0.5 mm adds a warning (never a verdict): |Z| and the noise
  can come out a few percent low. Use 0.5 mm or leave it unset; 0.25 mm gives the highest noise
  accuracy.
- The cost depends on the board. On the noise and tolerance examples, 0.5 mm takes about a
  quarter more time and memory than 1.0 mm. On a board of large planes close to the rail (the
  `pdn` example) the mesh is set by the edge, and an AC solve takes about ten times as long. On a
  large board with many pads and vias the mesh is set by them, and 0.5 mm adds only a few
  percent. The estimate before each AC and noise solve uses the edge the solve will use.

## How the two compare

Measured on the 11 example projects of this repository, every analysis that applies to each (DC
rails, power tree, board temperature, AC, noise, tolerance: 35 runs per mesher):

- **Verdicts:** identical in every run, item by item, and so are the exit codes.
- **Drops, losses, via currents, regulator values, temperature rises:** within 0.1 % at the
  default mesh, and within 0.004 % of each other when both meshes are refined.
- **AC and noise,** against the value both meshers converge to when refined:

  | Quantity | rust (0.5 mm) | Triangle (1.0 mm) |
  |---|---|---|
  | `SIG_LOOP` coupled noise (noise example) | −0.56 % | +0.51 % |
  | output ripple rms | −0.58 % | −0.60 % |
  | input ripple rms | −0.18 % | −0.21 % |
  | radiated estimate | −0.11 % | −0.13 % |

  On the `pdn` example, rust's |Z| is no farther from the converged value than Triangle's at
  1.0 mm from an edge of 0.7 mm down.
- **Current density.** The point peak |J| (the "Peak X A/mm²" label above each colour bar) and
  the crowding spots depend on the mesh with either mesher, and do not converge at sharp copper
  corners, where |J| has no finite value. A result judged against your `max_current_density`
  says so in its assumptions: a crowding spot's mean |J| depends on the mesh, typically by 1-2 %
  on detailed boards, up to about 6 %, and by more on a spot of only a few elements at a sharp
  corner. Read a spot within a few percent of your limit, or one of only a few elements, with
  care. See [the current-density default](technical.md#current-density-default).

## The same mesh on every computer

Rust produces the same mesh, bit for bit, on macOS, Windows and Linux, on Apple silicon and x86:
it uses IEEE double arithmetic only, never fuses a multiply and an add, and visits everything in
a fixed order. CI checks it on every platform: the built mesher contains no fused multiply-add
instruction, and a fixed polygon gives the same mesh digest everywhere. The solves on that mesh
still round as each machine's linear algebra does.

## How it works

A constrained Delaunay triangulation of the copper outline, then Delaunay refinement
(Ruppert's algorithm in Shewchuk's "Terminator" form) to the angle and size limits KiPI asks
for, with KiPI's own pre-processing, size field and fallbacks around it. It was written from the
published papers only; no code of Triangle was read. The algorithms, the sources and the
determinism rules are in [DESIGN.md](../tools/mesher_rs/DESIGN.md); building and testing it in
[tools/mesher_rs](../tools/mesher_rs/README.md).
