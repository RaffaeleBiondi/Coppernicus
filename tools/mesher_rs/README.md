# coppermesh (the default mesher, "rust"; in the packaged app)

A 2D quality mesher written for Coppernicus: our own constrained Delaunay triangulation plus
Delaunay refinement (Ruppert's algorithm in Shewchuk's "Terminator" form), in Rust, with a
Python binding. It is meant to replace Triangle behind KiPI's `mesh_polygon`. DESIGN.md gives
the algorithms, the determinism rules and every source used. No Triangle code was read.

It is Coppernicus's default mesher: a solve that names no mesher meshes with it (the CLI's
`--mesher`, the MCP `mesher`, the project's `settings.mesher` choose another; Triangle is
`triangle`). Where it is not installed (removed from the environment), such a solve meshes with
Triangle and its result says so in a warning; one that names `rust` stops with
`mesher_unavailable`. Its AC mesh default is finer than Triangle's: `RUST_AC_MAX_EDGE_MM`
(0.5 mm) in `src/coppernicus/engine/ac.py`, where the project does not set `ac: max_edge_mm`.

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

## Build and develop

Coppernicus depends on coppermesh from PyPI (`pyproject.toml`: `coppermesh>=0.1,<0.2`, not on
Intel macOS, which has no wheel; `uv.lock` pins 0.1.0 with its wheels). A plain `uv sync`,
`uv tool install git+...` and the GitHub Action install the prebuilt wheel and need no Rust. It is
one abi3 extension (CPython ≥ 3.12, so 3.12 and 3.13).

Working on the mesher means testing this folder instead of the wheel: build it over the wheel,
then keep uv from putting the wheel back. With Rust (installed per user: add `~/.cargo/bin` to
`PATH` for these commands; the build output can go anywhere, through `CARGO_TARGET_DIR`):

    uv sync                                           # the project, with PyPI's coppermesh
    uv pip install ./tools/mesher_rs                  # maturin builds this folder, over the wheel
    uv run --no-sync python tools/mesher_rs/check_source.py   # which coppermesh is installed
    uv run --no-sync pytest tests/test_mesher_rs.py tests/test_meshing_seam.py \
        tests/test_meshing_default.py tests/test_mesher_sliver.py
    uv run --no-sync python tools/mesher_rs/check_fma.py      # 0 fused multiply-add, or it fails
    uv run --no-sync python tools/mesher_rs/check_digest.py   # the recorded mesh, bit for bit
    cd tools/mesher_rs && cargo test --release        # core tests, no Python needed

Pass `--no-sync` to every `uv run` (or `export UV_NO_SYNC=1`) while the source build is
installed: `uv.lock` names PyPI's wheel, and a plain `uv run` or `uv sync` reinstalls it over the
source build without a word (both are coppermesh 0.1.0; only `check_source.py` tells them apart).
After a change here, run `uv pip install ./tools/mesher_rs` again: uv rebuilds it when a Rust
source, `Cargo.toml` or `Cargo.lock` changed (the crate's `cache-keys`). A plain `uv sync` goes
back to the wheel.

Users get what is on PyPI, not this folder: a change here reaches them only with a new version
published (see "Publishing coppermesh") and `uv.lock` updated to it
(`uv lock --upgrade-package coppermesh`; past 0.1.x, `pyproject.toml`'s bound too).

Where it is built and checked:
- `.github/workflows/tests.yml`, on Linux x86_64, macOS arm64 and Windows x64: the cargo tests,
  `uv sync --no-install-package coppermesh` then `uv pip install ./tools/mesher_rs`,
  `check_source.py`, `check_fma.py`, `check_digest.py`, and the whole pytest suite with
  `COPPERNICUS_REQUIRE_RUST=1` (the rust cases then fail instead of skipping when the extension is
  missing), every `uv run` with `--no-sync`. One Ubuntu job tests PyPI's wheel as users get it
  (`pypi-wheel`: `uv sync --no-build-package coppermesh`, `check_source.py --pypi`, the two
  checks, the mesher tests and a CLI solve of the demo with rust); one leaves coppermesh out
  (`no-rust`: the fallback tests and a CLI solve of the demo, coppermesh really missing). On
  Intel macOS (macos-15-intel): the cargo tests, `maturin build`, and the two checks on that
  wheel; pytest cannot run there, Triangle has no Intel macOS wheel for its pin.
- `.github/workflows/triangle-free.yml` builds it from this folder the same way, then removes
  Triangle.
- The packaged app (`packaging/`, `release.yml`) bundles it built from this folder (the build
  scripts sync without PyPI's coppermesh, install this folder and run `check_source.py`);
  `packaging/check_bundle.py` fails without it, and `check_fma.py` runs on the bundled
  extension.
- `.github/workflows/wheels.yml` builds the PyPI wheels (macOS arm64, Windows x86_64, Linux
  x86_64 manylinux_2_28) and the sdist, installs each wheel, and a wheel pip builds from the
  sdist, in a fresh venv and runs the import, `check_fma.py` and `check_digest.py` on it (see
  "Publishing coppermesh").
- `check_fma.py` counts x86_64 `vfmadd`/`vfmsub`/`vfnmadd`/`vfnmsub` and arm64
  `fmadd`/`fmsub`/`fnmadd`/`fnmsub` (and the vector `fmla`/`fmls`) in the disassembly
  (llvm-objdump from `rustup component add llvm-tools`, else objdump or otool). Measured on
  macOS: 0 in the arm64 build and in the x86_64 cross-build; SciPy's arm64 `_ckdtree`, the
  positive control (`--expect-fma`), 112.
- `check_digest.py`'s mesh was recorded on macOS arm64; the x86_64 cross-build
  (`maturin build --release --target x86_64-apple-darwin`) gives the same bits under Rosetta.
- Not seen yet: any of these CI jobs green, so the Linux, Windows and Intel macOS builds, the
  Windows disassembly and the digest on real x86_64 hardware are untried.

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
  (`portable-atomic` is locked but not built for the platforms we ship.)
- The packaged app carries their licence texts: `crate_licences.py` writes
  `packaging/licenses/coppermesh-crates.txt` from `Cargo.lock` (every crate built for the
  `python` feature on macOS, Windows and Linux, each licence file it ships, identical texts once),
  `packaging/notices.py` appends it to `THIRD_PARTY_NOTICES.txt`, and `check_bundle.py` fails a
  bundle whose notices lack a crate. After any change to `Cargo.lock` or `Cargo.toml`, run
  `python tools/mesher_rs/crate_licences.py` (with cargo on `PATH`) and commit the file:
  `tests/test_crate_licences.py` fails until then. It writes the same text to
  `tools/mesher_rs/licenses/coppermesh-crates.txt`, the copy the PyPI wheel and sdist carry
  (`license-files` in `pyproject.toml` may only name files inside the crate), beside
  `tools/mesher_rs/LICENSE`, a copy of the repository's GPL text; the test checks both copies
  byte for byte.
- spade (MIT OR Apache-2.0) was evaluated and is not used (DESIGN.md §1).

## Publishing coppermesh

coppermesh 0.1.0 is published to PyPI as its own package by `.github/workflows/wheels.yml`, with
PyPI's Trusted Publishing (OpenID Connect from GitHub Actions: no API token is stored anywhere).
What reaches PyPI is what `README.pypi.md` and the `[project]` table of `pyproject.toml`
describe. When the workflow runs:

- A push to a branch never starts it.
- A pushed tag `coppermesh-v*` builds, checks and uploads to TestPyPI only. The tag must name
  the version in `pyproject.toml` (`coppermesh-v0.1.0`), or the sdist job fails.
- A manual run (Actions → coppermesh wheels → Run workflow) builds and checks, then uploads
  according to its `publish` input:
  - `none` (the default): nothing is uploaded.
  - `testpypi`: TestPyPI.
  - `pypi`: PyPI, only from `main` or a `coppermesh-v*` tag. The upload also waits for the
    GitHub environment `pypi`'s required reviewer to approve it.

A file once uploaded can never be replaced, on either index. A fix is a new version: change
`version` in both `Cargo.toml` and `pyproject.toml` (a test keeps them equal), then run
`python tools/mesher_rs/crate_licences.py` (`Cargo.toml` changed) and commit. The TestPyPI job
skips files that are already there.

The owner's steps, once this workflow is on `main`:

1. **Create the GitHub environments.** In the repository, go to Settings → Environments.
   - Create `testpypi`.
   - Create `pypi`. Under "Required reviewers", add yourself (RaffaeleBiondi) and leave
     "Prevent self-review" unticked, so that you can approve your own run.
   - Under "Deployment branches and tags", restrict `pypi` to the branch `main` and the tags
     `coppermesh-v*`. The workflow already refuses other refs; this is a second lock.
   - With the reviewer in place, a PyPI upload needs both `publish=pypi` and your approval.
2. **Add a pending publisher on TestPyPI.** Sign in at https://test.pypi.org (a separate account
   from PyPI's), open Account settings → Publishing → "Add a new pending publisher" →
   GitHub, and enter:
   - PyPI Project Name: `coppermesh`
   - Owner: `RaffaeleBiondi`
   - Repository name: `Coppernicus`
   - Workflow name: `wheels.yml`
   - Environment name: `testpypi`
3. **Add a pending publisher on PyPI.** Do the same at https://pypi.org/manage/account/publishing/,
   with Environment name `pypi`.

   A pending publisher does not reserve the name. PyPI creates the project, and binds the name
   to you, only at the first upload. Until then anyone can register `coppermesh` (it was free on
   both indexes on 2026-11-27), so do steps 4 and 6 soon after these.
4. **Publish to TestPyPI.** Run the workflow on `main` with `publish=testpypi`. Pushing the tag
   `coppermesh-v0.1.0` does the same.
5. **Check the TestPyPI release.** Install it in a fresh venv. numpy is taken from PyPI,
   because TestPyPI holds only what people uploaded there for testing:

       python3.12 -m venv /tmp/cm && /tmp/cm/bin/pip install -i https://test.pypi.org/simple/ \
           --extra-index-url https://pypi.org/simple/ coppermesh==0.1.0
       /tmp/cm/bin/python -I tools/mesher_rs/check_digest.py      # from a checkout: the recorded mesh

   Also look at the project page, https://test.pypi.org/project/coppermesh/: the readme, the
   licence, the links, three wheels and the sdist.
6. **Publish to PyPI.** Run the workflow on `main` with `publish=pypi`, then approve the
   `publish to PyPI` job when GitHub asks for the review. Afterwards, check that
   `pip install coppermesh==0.1.0` works in a fresh venv.

Coppernicus depends on the PyPI package since 0.1.0 is there (see "Build and develop").

