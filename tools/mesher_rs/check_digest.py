"""Mesh one fixed polygon with the installed coppermesh and compare the mesh's SHA-256 with the
one recorded here: the same bits on every platform CI builds (arm64 and x86_64, macOS, Linux,
Windows).

    python tools/mesher_rs/check_digest.py

A 3 x 2 mm rectangle with a 0.6 mm square hole, at board-like coordinates (an ulp is ~1e-14 mm
there), refined to 28 degrees and 0.002 mm²: a few thousand triangles, every predicate and
circumcentre of the core on the way. Recorded on macOS arm64, and identical from the x86_64
build under Rosetta. A change to the core's algorithm that changes this mesh on purpose updates
EXPECTED; a mismatch on one platform only is a portability bug (an FMA, a libm call, an order
that depends on a hash or an address).

Exit code 0: the same mesh; 1: another one.
"""

from __future__ import annotations

import hashlib
import sys

import numpy as np

EXPECTED = (2329, 4434, "8fa28bce98675ba26b264b8447692de7b86a8dfea7615763ef90807fc590fd0f")


def mesh():
    import coppermesh
    corners = np.array([[0.0, 0.0], [3.0, 0.0], [3.0, 2.0], [0.0, 2.0],
                        [1.0, 0.7], [1.6, 0.7], [1.6, 1.3], [1.0, 1.3]]) + [120.0, 80.0]
    segments = np.array([[0, 1], [1, 2], [2, 3], [3, 0], [4, 5], [5, 6], [6, 7], [7, 4]], dtype=np.int64)
    m = coppermesh.Mesher(corners, segments, np.array([[121.3, 81.0]]))
    if not m.refine(28.0, 0.002, 1_000_000, "area"):
        raise SystemExit("error: the refinement hit its cap")
    pts, tris = m.export()
    digest = hashlib.sha256(np.ascontiguousarray(pts, dtype="<f8").tobytes()
                            + np.ascontiguousarray(tris, dtype="<i8").tobytes()).hexdigest()
    return len(pts), len(tris), digest


def main() -> int:
    got = mesh()
    print(f"{got[0]} nodes, {got[1]} triangles, sha256 {got[2]}")
    if got != EXPECTED:
        print(f"error: expected {EXPECTED[0]} nodes, {EXPECTED[1]} triangles, sha256 {EXPECTED[2]}",
              file=sys.stderr)
        return 1
    print("OK: the recorded mesh, bit for bit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
