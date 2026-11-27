# coppermesh

A 2D quality mesher written for [Coppernicus](https://github.com/RaffaeleBiondi/Coppernicus), the
DC power-integrity analyzer for KiCad boards. It is a constrained Delaunay triangulation plus
Delaunay refinement (Ruppert's algorithm in Shewchuk's "Terminator" form), written in Rust, with a
Python binding. Coppernicus uses it as its default mesher, in place of Triangle behind KiPI's
`mesh_polygon`.

- One abi3 wheel per platform for CPython 3.12 and later: macOS arm64, Windows x86_64 and
  Linux x86_64 (manylinux_2_28). Elsewhere pip builds it from the sdist, which needs a Rust
  toolchain.
- Deterministic: the same input gives the same mesh, bit for bit, on every platform it is
  built for. Its build has no fused multiply-add, and CI checks that and one recorded mesh
  on each wheel.
- Depends only on numpy at run time.

```python
import numpy as np
import coppermesh

# A 3 x 2 mm rectangle with a 0.6 mm square hole (one point inside the hole marks it).
corners = np.array([[0.0, 0.0], [3.0, 0.0], [3.0, 2.0], [0.0, 2.0],
                    [1.0, 0.7], [1.6, 0.7], [1.6, 1.3], [1.0, 1.3]])
segments = np.array([[0, 1], [1, 2], [2, 3], [3, 0],
                     [4, 5], [5, 6], [6, 7], [7, 4]], dtype=np.int64)
m = coppermesh.Mesher(corners, segments, np.array([[1.3, 1.0]]))
m.refine(28.0, 0.002, 1_000_000, "area")   # min angle (deg), max area, point cap: False if hit
points, triangles = m.export()             # (N, 2) float64, (M, 3) int64, counter-clockwise
```

The algorithms, the determinism rules and every source used are in
[DESIGN.md](https://github.com/RaffaeleBiondi/Coppernicus/blob/main/tools/mesher_rs/DESIGN.md);
the source is in
[tools/mesher_rs](https://github.com/RaffaeleBiondi/Coppernicus/tree/main/tools/mesher_rs).

## Licence

GPL-3.0-or-later, like Coppernicus (`LICENSE`). The wheels and the sdist also carry the licence
texts of the Rust crates compiled into the extension (`licenses/coppermesh-crates.txt`): robust,
pyo3, rust-numpy, ndarray and their dependencies, each under its own permissive licence, as that
file lists them.
