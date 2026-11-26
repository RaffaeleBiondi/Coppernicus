# Mesher selector: design note (target v0.5.0, with the owner's OK)

Status: design only.
- The seam it builds on (`src/coppernicus/engine/meshing/`) and the Rust backend are in the
  product tree, inactive: nothing is installed unless a mesher is selected, and Triangle stays
  the default.
- The CGAL backend is kept on an experimental branch.
- No product file (app, CLI, project, MCP, reports, packaging) is changed yet.

**Goal**
- The default mesher is the best licence-compatible bundled one: CGAL 2D Mesh_2 (GPL-3.0-or-later) or our own in Rust.
- Triangle becomes an optional download, fetched only when a user selects it after reading its licence.

## 1. The seam (built)

```
coppernicus.engine.meshing
  protocol.py   Backend.triangulate(points, segments, holes, max_area, min_angle, size_field,
                                    interior_points, *, first_cap, carve=None)
                  -> Triangulation(pts, tris, status, info)
                status: ok | cap | no_domain | graded_cap
                SizeField: KiPI's graded_size as data (hole centres, radii, h_min, h_max,
                grading; exact), or "sampled" (any other size_fn, evaluated once per pass,
                vectorised); graded_limits() = KiPI's gate, graded_cap() = KiPI's cap
  pipeline.py   KiPI's mesh_polygon around a backend: pad-outline clipping, 1 nm snap, PSLG
                guard, hole seeds, pad nodes, Steiner cap, both fallbacks, carving; plus a
                MeshError for copper meshed into no triangle (KiPI skips it silently), and the
                solver's zero-area and orphan checks
  registry.py   available(), get(name), install(name, **options), uninstall(), installed(),
                register(name, factory)
  triangle_backend.py  KiPI's own Triangle calls: bit-identical to KiPI (tested)
  rust_backend.py      our own CDT + Delaunay refinement in Rust (tools/mesher_rs)
  cgal_backend.py      the native CGAL extension (kept on an experimental branch)
```

### KiPI callers

**Covered by patching `kipi.core.mesh.mesher.mesh_polygon`:**
- DC rail and tree solves (`mesh_layer`);
- AC (`ac/solver/fem.py` → `mesh_many`);
- noise copper (`mesh_net`).

**Call Triangle directly; routed through `sys.modules["triangle"]` when the backend offers `triangle_compat()`:**
- `kipi/core/viz/meshes.py`: the 3D view's fast path, "pQ". Without Triangle it already falls back to GEOS. The 3D view module is patched only if already loaded, because importing it loads pyvista.
- `kipi/noise/extract/xsection.py`: the noise cross-section, "pq28a…" then "rpq28a" with per-triangle limits. CGAL's stand-in applies those limits as a sampled size field, so the result is close to Triangle's, not identical. Not yet checked on a full noise solve.

**Gaps:**
- **KiPI's layer pool** (`kipi/core/mesh/parallel.py`, above ~1.5 M estimated triangles) spawns workers that re-import KiPI and miss the patch. Today `install()` switches the pool off (`KIPI_SERIAL=1`). Fix before shipping, by either:
  - a pool initializer that calls `install(name)` in each worker, needing a hook in `kipi.core.workers.BoardPool`; or
  - upstream, a `mesh_polygon` backend argument / entry point in KiPI.
- **`kipi/core/mesh/mesher.py` imports `triangle` at load.** A Triangle-free install must put a stand-in module in `sys.modules["triangle"]` before anything imports KiPI. This happens in the same early bootstrap step where `bootstrap_triangle.activate()` runs today.

## 2. Choosing the mesher

The setting resolves in this order: CLI flag > project key > app setting > built-in default.

| Where | Design |
|---|---|
| App, Settings | "Mesher" combo: the names from `available()`, plus "Triangle (download…)" when it is not installed. Changing it re-solves nothing by itself. The status bar shows the mesher of the shown result. |
| CLI | `--mesher NAME` on `solve`, `--tree`, `ac`, `noise`, `tolerance`; `coppernicus --meshers` lists the available ones with versions. An unknown or unavailable name is an error that says how to get it (Triangle: the download command below). |
| Project | `settings.mesher: cgal`, so a project's results reproduce. Absent means the app default. |
| MCP | an optional `mesher` parameter on `solve_rail`, `solve_project`, `solve_tree`, `solve_ac`, `solve_noise`, `solve_tolerance`; `read_project` / `write_project` carry the project key. |

Implementation: one function in `coppernicus.engine` (e.g. `use_mesher(name)`) calls `meshing.install(name)` before a solve and records the choice; nothing outside `engine/` imports KiPI or the registry.

## 3. The default

- The final benchmark report chooses it, between the bundled candidates (CGAL native, Rust), on measured evidence only:
  - accuracy against converged references, not against Triangle (the refinement series show Triangle at 0.5 mm is not converged either);
  - mesh quality and robustness on near-degenerate input;
  - speed and memory on large boards;
  - bit-identity across macOS arm64, macOS x86_64 and Windows;
  - licence and wheel size.
- Until that report and the owner's OK, the default stays Triangle and nothing changes for users.

## 4. Triangle as an optional download

Reuse `coppernicus/bootstrap_triangle.py`, which already has the pinned wheel names, URLs and SHA-256 from `uv.lock`, the atomic install, the per-user folder, `activate()`, the removal path and the test hooks (`SITE_ENV`, `URL_ENV`).

Change only *when* it runs:
1. Not on first start any more: only when the user selects Triangle (app combo, `--mesher triangle`, a project with `mesher: triangle`).
2. Before downloading, show Triangle's licence terms in full and require an explicit acceptance; record the acceptance with the version. A project that asks for Triangle when it is not installed opens that dialog in the app; on the CLI it fails with the command that fetches it (e.g. `coppernicus --fetch-triangle`, which shows the licence and asks for confirmation).
3. Help → Remove downloaded mesher, and `--remove-triangle`, stay as they are. After removal, a project asking for Triangle behaves as in step 2.
4. Normal (non-frozen) installs: `triangle` stays a dependency only through KiPI. The Triangle-free stand-in from §1 decides whether KiPI loads without it.

## 5. Every result records its mesher

- RailResult, TreeResult, AC, noise and tolerance results gain `mesher`: the name, the version, and for native backends the build (e.g. `cgal 6.0.1, cgal_mesh_ext 0.0.1, fp-contract off`). It is added to `stats` and to the assumptions line ("Meshed with …").
- Reports (HTML/PDF/Markdown), the CI summary and MCP results print it next to the mesh settings (max edge, min angle, grading).
- Saved results and revision snapshots keep it, so a comparison can tell.

## 6. Compare warns on different meshers

When `compare` (CLI, app, MCP `compare_results` / `compare_revisions`) gets two results meshed by different meshers or mesher versions, it adds a warning. The warning says the meshes differ, so small differences are discretisation, not design changes; it states no threshold. The same warning applies to a different max edge, min angle or grading.

## 7. Packaging

**Native CGAL extension:**
- nanobind + scikit-build-core;
- header-only CGAL 6.0.1 and Boost 1.86, fetched in CI and not vendored in the repo;
- GMP-free (`CGAL_DISABLE_GMP`, Boost.Multiprecision);
- `-ffp-contract=off`, MSVC `/fp:strict`;
- measured: a 382 kB `.so` on macOS arm64, ~6 s compile.

**Wheels (cibuildwheel, 3 OS):**
- macOS arm64 (macos-14), macOS x86_64 (macos-13, or a cross build with `CMAKE_OSX_ARCHITECTURES=x86_64`), Windows x64 (MSVC).
- cp312 + cp313, or nanobind's stable ABI (one wheel per platform for 3.12+; not tried yet).

**Rust backend:** maturin abi3 wheels, the same matrix.

**PyInstaller:** the extension goes in like any other binary module; Triangle stays out (it is downloaded).

**CI checks:**
- the seam tests on all three OS;
- a mesh hash of the test polygon, via_array and demo, compared across the three: identical is expected with contraction off (measured identical arm64 vs x86_64 under Rosetta).

## 8. Risks and open points

- **KiPI's private helpers** (`_linework`, `_clip_constraint`, `_hole_seeds`, `_pad_nodes`, `_steiner_cap`, `_carve_holes`, `_triangulate`) are used through the module. A KiPI update can rename them. Mitigations: pin KiPI, and keep the Triangle bit-identity test, which fails loudly.
- **Recognising `graded_size`** relies on its closure variable names. Anything else falls back to a sampled field, which is approximate for a strict backend (e.g. the AC far mode's `zone_size`): to be measured.
- **The noise cross-section stand-in** is approximate; it needs a noise-solve comparison.
- **The pool gap** (§1): until it is fixed, a non-default mesher meshes large boards on one core.

## 9. Integration test plan

- The seam tests (`tests/test_meshing_seam.py`), for every available backend:
  - the 5 contract tests;
  - Triangle bit-identity on via_array and demo;
  - install/uninstall idempotent;
  - the stand-ins for KiPI's direct callers.
- New, with the selector:
  - the project key round trip;
  - CLI `--mesher` precedence;
  - the MCP parameter;
  - the mesher recorded in results and reports;
  - compare's warning;
  - the Triangle download path (licence acceptance, then download, then select; using the existing `URL_ENV` file:// hook);
  - KiPI loads with no Triangle installed.
