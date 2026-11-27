# coppermesh: design note (M1)

An experimental 2D quality mesher in Rust. It is a backend for KiPI's
`kipi.core.mesh.mesher.mesh_polygon`: a constrained Delaunay triangulation (CDT) followed by
Delaunay refinement. It is written from the papers listed at the end.

Clean room: no code, header or documentation of the program Triangle, and no port or
translation of it, was opened. Triangle is only run, through KiPI, as a reference in the
benchmarks.

## 1. The CDT: our own, not spade's

We write a small array-based triangulation. Each triangle stores three vertex ids, three
neighbour ids and a bit per edge marking a subsegment. The predicates come from the `robust`
crate.

Insertion (changed from M1). A new vertex splits its triangle into 3, or splits the edge it lies
on into 4 triangles, or into 2 on the boundary. A split subsegment's halves stay subsegments.
Then Lawson's flips restore the constrained Delaunay property, never flipping a subsegment
[de Berg et al., ch. 9; Shewchuk 2002 §3.1].
- M1 planned Bowyer–Watson cavities. A cavity can wrap around the free end of a dangling
  segment and reach both of its sides. It is then not star-shaped, and connecting it to the new
  vertex would delete the segment.
- Lawson's flips never meet that case. The Bowyer–Watson cavity is still computed, read-only,
  to find the subsegments a circumcentre would encroach (§2).

Why not spade 2.15.1 (MIT OR Apache-2.0)? I checked its public API.
- The refiner must split a constrained edge topologically at a floating-point split point and
  keep both halves as constraints. spade's edge split (`dcel_operations::split_edge`) is
  private to the crate.
- From outside the crate, a split point that is 1 ulp off the segment can only be inserted
  into a face, which leaves a sliver against the segment.
- `refine()` has only global area bounds and decides the outer faces by parity. A pad
  outline inside the copper would then become a hole (as a survey of existing meshers found).

So nothing is taken from spade. The CDT is built in three steps.
- **Vertices.** Inside a large super-triangle, in Hilbert-curve order (no randomisation),
  each located by a visibility walk. An exact duplicate becomes an alias of the vertex
  already there.
- **Segments.** Recovered by Sloan's edge flips [Sloan 1993]. A vertex lying exactly on a
  segment splits the segment there.
- **Carving.** A flood fill from the super-triangle and from KiPI's hole seeds, never
  crossing a segment (the semantics of KiPI's `holes`). Pad outlines therefore stay
  interior constraints. As in [Shewchuk 2002, App. A, line 2], the triangles outside the
  domain are deleted before refinement, so features outside the domain cannot encroach.

Flips and cavities use the strict incircle test. On-grid input is full of cocircular ties,
and these are left as they are.

## 2. Refinement: Ruppert's algorithm in Shewchuk's "Terminator" form

The loop follows Shewchuk 2002, Figures 38–39 and Appendix A, using diametral circles.

- **Encroachment** [§3.1]: a subsegment is encroached when a visible vertex lies on or
  inside its diametral circle. In a CDT only the apices of the one or two triangles on the
  subsegment need testing [App. A, Fig. 41], with `(x−w)·(y−w) ≤ 0`.
- **Priority** [§3.1]: encroached subsegments are split first. Among bad triangles, the
  skinny ones come off a heap, worst ratio first. The oversized but well-shaped ones come
  off a single FIFO queue, as App. A recommends ("it is counterproductive in practice to
  order good-quality triangles").
- **Bad triangle**: an angle below θ that does not separate two subsegments, or too large
  for the size field (§3). The angle test uses dot products against cos²θ, with no
  transcendental call per triangle.
- **Circumcentre** [App. A]: it is located by a straight walk from the triangle's centroid.
  - If a subsegment blocks the walk, the circumcentre encroaches it.
  - Otherwise we build its Bowyer–Watson cavity, read-only and crossing no subsegment, and test
    the subsegments on the cavity boundary.
  - If it encroaches none, it is inserted by a split and Lawson's flips (§1).
  - If it encroaches some, it is rejected. Each encroached subsegment s is queued if the
    triangle is oversized or SPLITPERMITTED(s, d) holds. The triangle goes back in the
    queue only if something was queued; otherwise it is never tried again (App. A).
- **Segment split** [§6.2, Ruppert's concentric shells]:
  - If an endpoint is an input vertex shared by two or more segments, the new vertex goes
    on the power-of-two shell (unit 1 mm) closest to the middle. It always lands in the
    middle third.
  - Otherwise the subsegment is bisected.
  - The split is topological: both halves stay subsegments, and the new triangles inherit
    the domain flag of their side.
- **Small input angles** (the Terminator, §6.3, SPLITPERMITTED in Fig. 38).
  - The subsegment cluster of s at an input vertex a is the run of subsegments at a that are
    separated by 60° or less, consecutively, inside the domain. Exterior angles are ignored.
  - A split of s is allowed if any of these holds:
    - s has no cluster, or a cluster at both ends;
    - |s| is not a power of two (relative tolerance 1e-9);
    - the cluster holds a shorter subsegment;
    - r_min = |s|·sin(φ_min/2) ≥ d.
  - Here d is the shortest edge of the bad triangle. This is the simplification §6.5 uses,
    an upper bound on r_g, so termination is still guaranteed.
  - sin(φ/2) is computed as |u−v|/2 on unit vectors.
  - Conditions B–E are implemented. F (ancestor chains) is not, as in §6.5.
- **Termination**:
  - Theory guarantees termination at 20.7° on acute-free input [§3.3]. With small angles,
    the Terminator always terminates (Theorem 11) and keeps skinny triangles only next to
    input angles below 60°.
  - The 28° target is above the proven bound. §7.3 reports that Ruppert's and Chew's
    algorithms "consistently achieve angle bounds better than 30°" in practice.
  - The hard guarantee is KiPI's Steiner cap (`_steiner_cap`). The core counts the points
    it adds and stops at the cap with a status, which the seam's pipeline turns into KiPI's
    fallbacks.

Off-centres [Üngör 2009] are not implemented. Their paper was not accessible here, and I do
not implement it from memory. Diametral lenses (§6.4) would need vertex deletion, and are not
used either.

## 2b. Near-zero input angles and the resolution floor

A copper sliver one snap step wide, sitting on a polygon edge, is two input segments meeting at an
angle of a few thousandths of a degree. Ruppert's algorithm, even in the Terminator form above,
cannot mesh it well.
- **The cascade.** Every vertex on one side encroaches the other side's subsegments (Shewchuk 2002
  §6.3 calls these Type 3 diminishing vertices). Splitting runs down both sides at the local
  width.
- **Measured on a 1 nm × 50 µm sliver:**
  - the Steiner cap is hit at every rotation tried;
  - with the cap ×10, it converges at 58–98 k triangles, nearly all with an edge under 1 nm
    (smallest 0.24 nm), against 92 triangles for the polygon without the sliver;
  - the concentric shells and the cluster rules handle the apex but not the sides.

Two additions handle it. Both are off by default in the core (`RefineParams::min_edge`,
`tiny_angle_deg`); the "rust" backend turns both on.
- **The floor.** No point is added closer than `min_edge` to an existing vertex. The backend uses
  KiPI's 1 nm noding grid, the resolution of its input.
  - A segment split or a circumcentre is checked against every vertex of its Bowyer–Watson
    cavity: those are the vertices it would be joined to, and every edge refinement makes ends
    at the vertex being inserted.
  - Below the floor the point is not added and the subsegment or triangle stays (`floored` in
    the stats).
  - This is the "sure-fire way to guarantee termination" of Shewchuk 2002 §6.2, never creating
    an edge shorter than a bound, with the bound tied to the snap grid.
  - Refinement then never makes an edge under 1 nm; input edges can be shorter.
- **Tiny wedges.** Two input segments that share an end and meet there at less than
  `tiny_angle_deg` (backend: 1°) bound a tiny wedge. The test uses dot and cross products against
  sin²θ from a fixed series, so no libm.
  - **Rule E.** A subsegment that only vertices on its wedge partner encroach is not split. This
    applies the insertion-radius rule of §6.2 within tiny wedges: "if a subsegment is encroached
    upon by a segment vertex, and the new vertex … would have a smaller insertion radius than the
    encroaching vertex, do not split". Across a tiny wedge the new vertex's radius, at most the
    local width, is always the smaller one, so the rule always declines there. Splits that a
    rejected circumcentre asks for still go ahead; the split queue carries the request's origin.
  - **Rule T.** A skinny triangle, not over the size bound, whose three vertices all lie on the
    two sides of one tiny wedge is not refined. This is §6.1's corner-lopping, without changing
    the domain: the lopped corner is the wedge itself, and its fan is the triangles left between
    its two sides.
- **The documented quality exception** is two kinds of triangle.
  - **The wedge zone:** the triangles with every vertex on the two sides of a tiny wedge.
    - It is as long as the wedge's longer side, which can run on past the shorter side's end.
    - It is no thicker than the wedge's mouth (its width at the shorter side's end).
    - Inside it the mesh is only constrained Delaunay: its sides may stay encroached.
  - **The floor zone:** any triangle the floor kept, whose shortest edge is at the floor, with
    any angle.
  - **Measured in synthetic sweeps** of 3 731 cases (every rotation 0–90° in 1° steps; apex
    0.001–1°, short leg 5–200 µm, width 1–10 grid steps):
    - the wedge zone holds at most 2 triangles, up to 430 µm long and up to about 0.2 µm thick
      (a 0.5° wedge);
    - the floor zone holds one triangle (26°, 1 nm edge), in 3 cases with two input vertices
      1 nm apart.
  - **A wedge over the size bound** is still refined to it (Rule T takes only skinny triangles
    within the bound). A 0.5° wedge 6 mm long gives 2 wedge-zone triangles with the area rule and
    39–43 with the edge rule, 52 µm thick at its mouth.
  - Every other triangle keeps the 28° bound.
- **Why 1°**, chosen by measurement: with the floor alone, refining a wedge properly costs about
  ln(L/ε)/φ points per side.
  - Triangle ratio against the polygon without the wedge: 3 at 1°, 11 at 0.1°, 100 at 0.01°.
  - Below about 0.005° (a width under 2 nm) the floor blocks the cascade, leaving thousands of
    skinny triangles.
  - **The trade-off:** a wedge between about 0.1° and 1° could be refined properly at a ratio of
    3–11, with one skinny triangle at its apex. It is lopped instead, at a ratio of about 1.1,
    with the whole wedge in the zone.
  - The threshold is one backend attribute (`RustBackend.tiny_angle_deg`).
  - Above 1°, nothing changes; a synthetic test polygon's 3° and 4° angles keep normal
    refinement.
  - The bench cases' smallest input angle is 90°, so their meshes are bit-identical with and
    without these rules (verified).
- **Not used:** a pre-pass that collapses sub-resolution spikes. It would change every mesher's
  input, and the two rules above suffice.

## 3. The size field: sampled once, on a background mesh

There are two passes, as in KiPI.
- **Pass 1** refines to θ and `max_area`.
- **Pass 2** runs only with a size field. KiPI's own `graded_size` is evaluated exactly, in
  Rust, at each triangle's centroid (§5). Any other `size_fn` is called once, vectorised, at
  the pass-1 nodes. Pass 1's mesh, frozen, becomes the background: each node carries
  `h = size_fn(node)`, and h is piecewise linear inside its triangles.
  - Every Steiner point added in pass 2 takes h by barycentric interpolation in the
    background triangle it lands in. It is found by a walk from a nearby vertex's
    background triangle, with a full scan as the fallback.
  - So there is no Python callback per insertion.
  - The holes' ring vertices are pass-1 nodes, so the minima of KiPI's `graded_size` (h_min
    on every drill ring) are sampled exactly.
- **Size test** (`rule`):
  - `area` (default, KiPI's convention): too large when the area exceeds
    min(√3/4·h_c², max_area), with h_c the mean of the three vertex values.
  - `edge`: the longest edge exceeds min(h_c, h_max), where h_max = (4·max_area/√3)^½ is
    the project's `max_edge_mm`.
  - Both are kept so that triangle counts compare like for like with other meshers' rules.
- **The same entry point serves solution-adaptive refinement**: a per-node size field from an
  error estimate on a coarse solution.
- The benchmark measures how closely the mesh follows the field: p99 of longest edge /
  `size_fn` (centroid), against Triangle's 2.25 and CGAL's 1.26 on a synthetic test polygon.

## 4. Determinism

- IEEE double only. Rust never contracts a·b+c into an FMA unless `mul_add` is written, and
  the core never writes it. The `robust` crate source has no `mul_add`/FMA (checked, v1.2.0).
- No transcendental functions (libm) in the core.
  - cos²θ comes from a fixed Taylor series in Rust, so the threshold is bit-identical on
    every IEEE platform.
  - √ is IEEE-exact, and it is the only non-arithmetic operation.
  - Powers of two come from bit manipulation, not `log2`.
- Fixed orders:
  - input in Hilbert order, ties broken by input index;
  - queues FIFO, or a heap keyed by (ratio, sequence number);
  - flood fills and cavities in a fixed neighbour order.
- Hash maps are used only for lookups, never iterated. No randomness and no threads.
- The input is KiPI's own 1 nm-snapped PSLG. `points` are not snapped (KiPI does not snap
  them, and the contract test asserts that they are nodes at distance 0.0). The benchmark
  measured their 1-ulp effect, and an option to snap them is a one-line change.

## 5. Meeting KiPI's contract: the "rust" backend of the mesher seam

The owner decided on a mesher selector: Triangle and coppermesh are swappable backends of one
seam, `coppernicus.engine.meshing`. Nothing is installed unless a mesher is selected; Triangle
stays the default.
- `pipeline.py` keeps KiPI v1.0.1's pre-processing and fallbacks for every backend:
  - clipping, the 1 nm snap, the guard, hole seeds;
  - the Steiner caps, the retry without pad outlines, the carve retry;
  - MeshError for copper that yields no triangle and for zero-area triangles.
- `RustBackend` (`rust_backend.py`) meets `protocol.py`'s contract:
  - **Holes, pads**: the input points stay nodes bit for bit, the mesh conforms to the
    segments, and holes are seed flood fills (§1). `interior_points` become nodes where they
    lie in the domain.
  - **Uniform pass**: `refine(min_angle, max_area, first_cap)`, with status "cap".
  - **No domain**: "no_domain" when the seeds leave nothing.
  - **`carve`**: the core's `retain(mask)` keeps the triangles whose centroid lies in the
    polygon. The new boundary edges become subsegments.
  - **Graded pass**: it runs when the protocol's `graded_limits` gate says so, with
    `graded_cap` as the cap. A cap hit returns the uniform arrays, kept, with status
    "graded_cap".
    - A "graded" field (KiPI's `graded_size` as hole features) is evaluated natively at every
      triangle's centroid (`size.rs`: a static k-d tree of the hole centres). So the
      protocol's limit min(√3/4·h², max_area) holds at every centroid (`strict_size`).
    - A "sampled" field is evaluated once, vectorised, at the uniform mesh's nodes, and
      interpolated on the frozen uniform mesh (§3).
- **`triangle_compat()`** stands in for Triangle where KiPI calls it directly. It covers the
  3D view's "pQ" (a CDT, nothing added) and the noise cross-section's "pq28a…" then "rpq28a"
  passes with per-triangle limits.
  - The "r" pass rebuilds the CDT of the given nodes and subsegments (the core's
    `subsegments()`), so it conforms to the points already split onto segments.
  - It turns the limits into a per-node size field: the smallest limit at each node,
    interpolated in between. The refinement is close to Triangle's, not the same.
- **Platform independence of h.** Our native h computes the final distance itself (dx·dx+dy·dy,
  √, no FMA); the k-d tree only picks the candidate. It is bit-identical on arm64 and x86_64,
  and equals KiPI's `size_fn` exactly on x86_64.
  - KiPI's `graded_size` (SciPy's cKDTree) differs between the two architectures by up to
    7 ulp at a few % of points.
  - SciPy's arm64 `_ckdtree` binary contains 112 fused multiply-add instructions, the x86_64
    one none (measured).
  - Only the refinement's h is native. The graded pass's gate and cap use the protocol's
    `graded_limits` / `graded_cap`, which call KiPI's `size_fn`. They depend on SciPy's ulps
    near their thresholds; the measured meshes were identical across the two architectures.

## 6. Layout

- `Cargo.toml`, `src/`:
  - the core is pure Rust, tested with `cargo test`;
  - the PyO3 binding is behind the `python` feature, built by maturin (`pyproject.toml`
    here, not the repo's);
  - the module is `coppermesh`.
- `src/coppernicus/engine/meshing/rust_backend.py`: the thin Python backend.
- `tests/test_mesher_rs.py`, `tests/test_meshing_seam.py` (repo root): skipped when the
  extension is not built.

## Sources

- J. R. Shewchuk, "Delaunay refinement algorithms for triangular mesh generation",
  Computational Geometry: Theory and Applications 22 (2002) 21–74. Read from the author's
  preprint, people.eecs.berkeley.edu/~jrs/papers/2dj.pdf; section and figure numbers above
  refer to it. Used: §3.1, Lemma 1, §6.2–6.3, §6.5, §7.3, App. A (Figs. 38, 39, 41, and the
  queue advice).
- J. Ruppert, "A Delaunay refinement algorithm for quality 2-dimensional mesh generation",
  J. Algorithms 18 (1995) 548–585: concentric shells, as presented in Shewchuk 2002 §6.2.
- L. P. Chew, "Guaranteed-quality mesh generation for curved surfaces", SCG 1993 (the second
  algorithm): context only. Its diametral lenses are not used.
- S. W. Sloan, "A fast algorithm for generating constrained Delaunay triangulations",
  Computers & Structures 47 (1993) 441–450: segment recovery by flips.
- A. Bowyer, Computer Journal 24 (1981) 162–166; D. F. Watson, Computer Journal 24 (1981)
  167–172: the cavity, used here for the encroachment test.
- M. de Berg, O. Cheong, M. van Kreveld, M. Overmars, *Computational Geometry: Algorithms and
  Applications*, 3rd ed., Springer 2008, ch. 9: legal edges, flips, point location by walking.
- N. Amenta, S. Choi, G. Rote, "Incremental constructions con BRIO", SCG 2003: spatial
  (Hilbert) insertion order. We use it without the randomisation.
- J. R. Shewchuk, "Adaptive precision floating-point arithmetic and fast robust geometric
  predicates", Discrete & Computational Geometry 18 (1997) 305–363, and `predicates.c`,
  public domain (cs.cmu.edu/~quake/robust.html). Used through the `robust` crate 1.2.0
  (MIT OR Apache-2.0, the GeoRust/spade developers), a Rust port of those predicates.
- spade 2.15.1 (MIT OR Apache-2.0): its public API was read to decide §1. No code taken.
- For solution-adaptive refinement (planned): O. C. Zienkiewicz, J. Z. Zhu, "The superconvergent patch recovery and a
  posteriori error estimates", Int. J. Numer. Methods Eng. 33 (1992) 1331–1364 (gradient
  recovery).
- §2b uses Shewchuk 2002 §6.1 (corner-lopping), §6.2 (the termination bound and the
  insertion-radius rule, p. 32) and §6.3 (Type 3 diminishing vertices).
- Not accessible here, so not used: Miller–Pav–Walkington 2003 (the URL tried returned 404),
  Üngör 2009 (paywalled), Shewchuk 2000 (small angles; the URL tried returned 404). The
  small-angle handling comes from Shewchuk 2002 §6.3 instead.
