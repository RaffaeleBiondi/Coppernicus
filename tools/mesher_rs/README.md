# coppermesh (experiment, not shipped)

A 2D quality mesher written for Coppernicus: our own constrained Delaunay triangulation plus
Delaunay refinement (Ruppert's algorithm in Shewchuk's "Terminator" form), in Rust, with a
Python binding. It is meant to replace Triangle behind KiPI's `mesh_polygon`. DESIGN.md gives
the algorithms, the determinism rules and every source used. No Triangle code was read.

- `src/`: the core.
  - `geom.rs`: predicates and the scalar helpers.
  - `mesh.rs`: the CDT, the carving and the checks.
  - `refine.rs`: refinement and the background size field.
  - `size.rs`: KiPI's graded size field, evaluated natively.
  - `python.rs`: the PyO3 binding, built only with the `python` feature.
- `tests/mesh.rs`: cargo tests. They cover:
  - the Delaunay property, conformity, the angle, area and edge bounds;
  - holes, pad outlines, interior points;
  - collinear points, duplicates and near-duplicates;
  - the 0.01 mm stub, the 4° spike, the slit, wedges;
  - determinism, the cap, the size field.
- The Python side is the "rust" backend of the engine's mesher seam,
  `src/coppernicus/engine/meshing/rust_backend.py` (`RustBackend`). The seam's pipeline keeps
  KiPI's pre-processing and fallbacks for every backend.
- Its `triangle_compat()` stands in for Triangle where KiPI calls it directly: the 3D view and
  the noise cross-section.
- Tests:
  - `tests/test_mesher_rs.py`: the backend against the seam's protocol;
  - `tests/test_meshing_seam.py`: KiPI's contract, through the seam, for every backend.

  Both skip the rust cases when the extension is not built.

## Build

Rust is installed per user. Add `~/.cargo/bin` to `PATH` only for these commands. The build
output can go anywhere, through `CARGO_TARGET_DIR`.

    cd tools/mesher_rs
    cargo test --release                              # core tests, no Python needed
    uv pip install --python ../../.venv/bin/python maturin
    VIRTUAL_ENV=../../.venv ../../.venv/bin/maturin develop --release --uv
    cd ../.. && .venv/bin/python -m pytest tests/test_mesher_rs.py tests/test_meshing_seam.py

`maturin develop` installs `coppermesh` into the venv as one abi3 extension (CPython ≥ 3.12,
so 3.12 and 3.13). For wheels, `maturin build --release` builds the current platform; for
macOS arm64 and x86_64 and Windows x64, run maturin-action in CI.
- Tried: `maturin build --release --target x86_64-apple-darwin` on macOS arm64. The x86_64 wheel
  gives bit-identical meshes under Rosetta (measured).
- Not tried: Windows, and CI.

Use from Python:

    from coppernicus.engine import meshing
    meshing.install("rust")        # KiPI's mesh_polygon goes through the seam with coppermesh
    meshing.uninstall()

## Licences

- coppermesh is GPL-3.0-or-later, like Coppernicus.
- The core links only:
  - `robust` 1.2.0, MIT OR Apache-2.0. It is a port of Shewchuk's public-domain
    `predicates.c`.
- With the `python` feature it also links:

  | Crate | Licence |
  |---|---|
  | `pyo3` 0.30, with `pyo3-ffi`, `-macros`, `-build-config` | MIT OR Apache-2.0 |
  | `numpy` 0.30 (rust-numpy) | BSD-2-Clause |
  | `ndarray` 0.17 | MIT OR Apache-2.0 |
  | `num-traits`, `num-integer`, `num-complex` | MIT OR Apache-2.0 |
  | `matrixmultiply`, `rawpointer` | MIT/Apache-2.0 |
  | `libc`, `once_cell`, `portable-atomic`(`-util`), `rustc-hash` | MIT OR Apache-2.0 |
  | build only: `heck`, `proc-macro2`, `quote`, `syn`, `autocfg` | MIT OR Apache-2.0 |
  | build only: `unicode-ident` | (MIT OR Apache-2.0) AND Unicode-3.0 |
  | build only: `target-lexicon` | Apache-2.0 WITH LLVM-exception |

  All of these may be combined with GPL-3.0 code. That is my reading, not legal advice.
- spade (MIT OR Apache-2.0) was evaluated and is not used (DESIGN.md §1).
