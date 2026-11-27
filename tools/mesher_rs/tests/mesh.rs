//! M2: the CDT and the refinement, on small synthetic PSLGs in board-like coordinates.

mod common;
use common::*;
use coppermesh::{Mesh, RefineParams, Rule, Status};

const MAX_EDGE: f64 = 0.5;

fn max_area() -> f64 {
    3f64.sqrt() / 4.0 * MAX_EDGE * MAX_EDGE
}

fn params(rule: Rule) -> RefineParams {
    RefineParams::new(28.0, max_area(), 1_000_000, rule)
}

fn build(g: &Pslg) -> Mesh {
    Mesh::build(&g.verts, &g.segs, &g.holes).expect("build")
}

fn refined(g: &Pslg, rule: Rule) -> (Vec<P>, Vec<[u32; 3]>) {
    let mut m = build(g);
    assert_eq!(m.refine(&params(rule)), Status::Done);
    m.check().expect("mesh invariants");
    m.export()
}

#[test]
fn cdt_of_a_square_is_two_triangles_and_keeps_every_input_vertex() {
    let mut g = Pslg::default();
    g.ring(&rect(X0, Y0, X0 + 1.0, Y0 + 1.0));
    let mut m = build(&g);
    m.check().unwrap();
    let (p, t) = m.export();
    assert_eq!(t.len(), 2);
    assert_valid(&p, &t);
    for v in &g.verts {
        assert!(p.contains(v), "input vertex lost");
    }
}

#[test]
fn constrained_delaunay_property_before_and_after_refinement() {
    let mut g = Pslg::default();
    g.ring(&[[X0, Y0], [X0 + 6.0, Y0], [X0 + 6.0, Y0 + 0.3], [X0 + 3.1, Y0 + 2.0], [X0, Y0 + 4.0]]);
    g.line(&[[X0 + 1.0, Y0 + 1.0], [X0 + 5.0, Y0 + 0.4]]); // an interior constraint
    let mut m = build(&g);
    m.check().unwrap(); // includes the constrained Delaunay test of every non-subsegment edge
    assert_eq!(m.refine(&params(Rule::Area)), Status::Done);
    m.check().unwrap();
    assert_eq!(m.encroached_subsegments(), 0, "Ruppert's output has no encroached subsegment");
}

#[test]
fn refined_rectangle_with_holes_meets_angle_and_area_bounds_and_conforms() {
    let mut g = Pslg::default();
    let outer = rect(X0, Y0, X0 + 8.0, Y0 + 5.0);
    g.ring(&outer);
    let mut holes = vec![];
    for i in 0..3 {
        let h = ngon(X0 + 3.0 + i as f64, Y0 + 2.5, 0.15, 8);
        g.ring(&h);
        g.holes.push([X0 + 3.0 + i as f64, Y0 + 2.5]);
        holes.push(h);
    }
    let (p, t) = refined(&g, Rule::Area);
    assert_valid(&p, &t);
    assert!(min_angle(&p, &t) >= 28.0 - 1e-9, "min angle {}", min_angle(&p, &t));
    for tri in &t {
        assert!(signed_area2(&p, tri) / 2.0 <= max_area() * (1.0 + 1e-12));
        let c = centroid(&p, tri);
        assert!(holes.iter().all(|h| !point_in_ring(c, h)), "triangle inside a hole");
    }
    let want = polygon_area(&outer) - holes.iter().map(|h| polygon_area(h)).sum::<f64>();
    assert!((mesh_area(&p, &t) - want).abs() <= 1e-9 * want);
    for s in &g.segs {
        let (a, b) = (g.verts[s[0] as usize], g.verts[s[1] as usize]);
        assert!(segment_conforms(&p, &t, a, b), "segment {:?} not conforming", s);
    }
}

#[test]
fn edge_rule_bounds_the_longest_edge() {
    let mut g = Pslg::default();
    g.ring(&rect(X0, Y0, X0 + 5.0, Y0 + 3.0));
    let (p, t) = refined(&g, Rule::Edge);
    assert!(max_edge(&p, &t) <= MAX_EDGE * (1.0 + 1e-12), "max edge {}", max_edge(&p, &t));
    assert!(min_angle(&p, &t) >= 28.0 - 1e-9);
}

#[test]
fn pad_outline_inside_copper_is_a_constraint_not_a_hole() {
    let mut g = Pslg::default();
    let outer = rect(X0, Y0, X0 + 4.0, Y0 + 4.0);
    g.ring(&outer);
    let pad = rect(X0 + 1.0, Y0 + 1.0, X0 + 1.6, Y0 + 1.3);
    g.ring(&pad);
    let (p, t) = refined(&g, Rule::Area);
    assert!((mesh_area(&p, &t) - 16.0).abs() <= 1e-9 * 16.0, "the pad area was removed");
    assert!(t.iter().any(|tri| point_in_ring(centroid(&p, tri), &pad)), "no triangle in the pad");
    for k in 0..4 {
        assert!(segment_conforms(&p, &t, pad[k], pad[(k + 1) % 4]));
    }
}

#[test]
fn interior_points_and_duplicates_become_single_nodes() {
    let mut g = Pslg::default();
    g.ring(&rect(X0, Y0, X0 + 3.0, Y0 + 2.0));
    let q1 = [X0 + 1.2345678901, Y0 + 1.1111111];
    let q2 = [X0 + 2.7654321, Y0 + 0.4101010101];
    g.point(q1);
    g.point(q2);
    g.point(q1); // an exact duplicate
    g.point([X0, Y0]); // a duplicate of a ring vertex
    let (p, t) = refined(&g, Rule::Area);
    assert_valid(&p, &t);
    for q in [q1, q2] {
        assert_eq!(p.iter().filter(|&&x| x == q).count(), 1, "point not a single node");
    }
}

#[test]
fn collinear_vertices_on_a_segment_split_it() {
    // A vertex exactly on an outline segment, and a near-duplicate 1 nm away.
    let mut g = Pslg::default();
    g.ring(&rect(X0, Y0, X0 + 2.0, Y0 + 1.0));
    g.point([X0 + 1.0, Y0]); // exactly on the bottom segment
    g.point([X0 + 1.0, Y0 + 0.5]);
    g.point([X0 + 1.0 + 1e-6, Y0 + 0.5]); // 1 nm from the previous one
    let (p, t) = refined(&g, Rule::Area);
    assert_valid(&p, &t);
    assert!(p.contains(&[X0 + 1.0, Y0]));
    assert!(segment_conforms(&p, &t, [X0, Y0], [X0 + 2.0, Y0]));
    assert!((mesh_area(&p, &t) - 2.0).abs() <= 1e-9 * 2.0);
}

#[test]
fn neck_spike_stub_and_slit_terminate_with_quality_outside_the_spike() {
    // Two lobes joined by a 0.15 mm neck, a 4 degree spike, a 0.01 mm pad stub, a 0.02 mm slit.
    let tan2 = (2.0f64).to_radians().tan();
    let tip = Y0 + 4.0 + 0.05 / tan2;
    let mut g = Pslg::default();
    let outline = vec![
        [X0, Y0],
        [X0 + 4.0, Y0],
        [X0 + 4.0, Y0 + 1.925],
        [X0 + 6.0, Y0 + 1.925],
        [X0 + 6.0, Y0],
        [X0 + 10.0, Y0],
        [X0 + 10.0, Y0 + 4.0],
        [X0 + 8.0, Y0 + 4.0],
        [X0 + 8.0, Y0 + 1.0], // the slit: 0.02 mm wide, 3 mm deep
        [X0 + 7.98, Y0 + 1.0],
        [X0 + 7.98, Y0 + 4.0],
        [X0 + 6.0, Y0 + 4.0],
        [X0 + 6.0, Y0 + 2.075],
        [X0 + 4.0, Y0 + 2.075],
        [X0 + 4.0, Y0 + 4.0],
        [X0 + 2.1, Y0 + 4.0],
        [X0 + 2.05, tip], // 4 degree spike
        [X0 + 2.0, Y0 + 4.0],
        [X0, Y0 + 4.0],
    ];
    g.ring(&outline);
    // A pad outline stub of 0.01 mm ending on the copper edge (what KiPI's clipping leaves).
    g.line(&[[X0 + 1.0, Y0], [X0 + 1.0, Y0 + 0.01]]);
    let mut m = build(&g);
    assert_eq!(m.refine(&params(Rule::Area)), Status::Done);
    m.check().unwrap();
    let (p, t) = m.export();
    assert_valid(&p, &t);
    let ang = angles(&p, &t);
    let in_spike = |tri: &[u32; 3]| centroid(&p, tri)[1] > Y0 + 4.0;
    let bad: Vec<_> = t
        .iter()
        .zip(&ang)
        .filter(|(tri, a)| !in_spike(tri) && a.iter().cloned().fold(180.0, f64::min) < 28.0 - 1e-9)
        .collect();
    assert!(bad.is_empty(), "{} triangles below 28 deg outside the spike", bad.len());
    assert!((mesh_area(&p, &t) - polygon_area(&outline)).abs() <= 1e-9 * polygon_area(&outline));
}

#[test]
fn spike_alone_leaves_few_skinny_triangles() {
    let tan2 = (2.0f64).to_radians().tan();
    let mut g = Pslg::default();
    g.ring(&[[X0, Y0], [X0 + 0.1, Y0], [X0 + 0.05, Y0 + 0.05 / tan2]]);
    let mut m = build(&g);
    assert_eq!(m.refine(&params(Rule::Area)), Status::Done);
    let (p, t) = m.export();
    assert_valid(&p, &t);
    let skinny = angles(&p, &t).iter().filter(|a| a.iter().cloned().fold(180.0, f64::min) < 28.0).count();
    assert!(skinny <= 3, "{} skinny triangles in a lone spike", skinny);
}

#[test]
fn same_input_gives_bit_identical_meshes() {
    let mut g = Pslg::default();
    g.ring(&rect(X0, Y0, X0 + 6.0, Y0 + 4.0));
    for i in 0..4 {
        g.ring(&ngon(X0 + 1.0 + 1.3 * i as f64, Y0 + 2.0, 0.15, 8));
        g.holes.push([X0 + 1.0 + 1.3 * i as f64, Y0 + 2.0]);
    }
    let a = refined(&g, Rule::Area);
    let b = refined(&g, Rule::Area);
    assert_eq!(a.1, b.1);
    assert!(a.0.iter().zip(&b.0).all(|(x, y)| x[0].to_bits() == y[0].to_bits() && x[1].to_bits() == y[1].to_bits()));
}

#[test]
fn steiner_cap_stops_refinement_with_a_status() {
    let mut g = Pslg::default();
    g.ring(&rect(X0, Y0, X0 + 20.0, Y0 + 20.0));
    let mut m = build(&g);
    let mut prm = params(Rule::Area);
    prm.cap = 10;
    assert_eq!(m.refine(&prm), Status::CapHit);
    m.check().unwrap(); // still a valid triangulation
}

#[test]
fn sized_refinement_follows_a_per_node_size_field() {
    // h = 0.02 at a via ring growing 0.2 per mm (KiPI's graded_size), sampled once at the
    // first-pass nodes; triangles near the ring end up much smaller than far away.
    let mut g = Pslg::default();
    g.ring(&rect(X0, Y0, X0 + 4.0, Y0 + 4.0));
    let (cx, cy, r) = (X0 + 2.0, Y0 + 2.0, 0.15);
    g.ring(&ngon(cx, cy, r, 8));
    g.holes.push([cx, cy]);
    let mut m = build(&g);
    assert_eq!(m.refine(&params(Rule::Area)), Status::Done);
    let (p1, t1) = m.export();
    let size = |q: P| (0.02 + 0.2 * ((q[0] - cx).hypot(q[1] - cy) - r).max(0.0)).min(MAX_EDGE);
    let h: Vec<f64> = p1.iter().map(|&q| size(q)).collect();
    assert_eq!(m.refine_sized(&h, &params(Rule::Area)), Status::Done);
    m.check().unwrap();
    let (p, t) = m.export();
    assert_valid(&p, &t);
    assert!(t.len() > 2 * t1.len());
    assert!(min_angle(&p, &t) >= 28.0 - 1e-9);
    // Near the ring the triangles respect the field's area limit (with interpolation slack).
    let mut worst: f64 = 0.0;
    for tri in &t {
        let c = centroid(&p, tri);
        let lim = 3f64.sqrt() / 4.0 * size(c).powi(2);
        worst = worst.max(signed_area2(&p, tri) / 2.0 / lim);
    }
    assert!(worst < 4.0, "area / limit up to {}", worst);
}

fn min_angle_away_from(p: &[P], t: &[[u32; 3]], apex: P) -> f64 {
    let a = p.iter().position(|&q| q == apex).expect("apex is a node") as u32;
    let keep: Vec<[u32; 3]> = t.iter().filter(|tri| !tri.contains(&a)).cloned().collect();
    min_angle(p, &keep)
}

#[test]
fn an_angle_between_two_subsegments_is_never_refined() {
    // A lone 4 degree spike within the area bound: its apex angle is an input angle, so the
    // triangle is left as it is (Shewchuk 2002, Fig. 38 line 8).
    let tan2 = (2.0f64).to_radians().tan();
    let mut g = Pslg::default();
    g.ring(&[[X0, Y0], [X0 + 0.1, Y0], [X0 + 0.05, Y0 + 0.05 / tan2]]);
    let (p, t) = refined(&g, Rule::Area);
    assert_eq!((p.len(), t.len()), (3, 1));
}

#[test]
fn wedges_with_unequal_arms_terminate() {
    // Without concentric shells, mutual encroachment of unequal arms runs away (Shewchuk 2002,
    // §6.2, Fig. 24).
    for deg in [5.0f64, 10.0, 20.0, 30.0, 45.0, 55.0] {
        let a = deg.to_radians();
        let mut g = Pslg::default();
        g.ring(&[[X0, Y0], [X0 + 5.0, Y0], [X0 + 3.3 * a.cos(), Y0 + 3.3 * a.sin()]]);
        let mut m = build(&g);
        let mut prm = params(Rule::Area);
        prm.cap = 200_000;
        assert_eq!(m.refine(&prm), Status::Done, "wedge {} deg", deg);
        let (p, t) = m.export();
        assert!(t.len() < 2_000, "wedge {} deg: {} triangles", deg, t.len());
        assert_valid(&p, &t);
    }
}

#[test]
fn shell_splits_leave_power_of_two_subsegments_at_input_corners() {
    let mut g = Pslg::default();
    g.ring(&rect(X0, Y0, X0 + 3.0, Y0 + 0.7));
    let (p, _) = refined(&g, Rule::Area);
    let mut xs: Vec<f64> = p.iter().filter(|q| q[1] == Y0).map(|q| q[0] - X0).collect();
    xs.sort_by(|a, b| a.partial_cmp(b).unwrap());
    assert!(xs.len() > 2);
    for d in [xs[1] - xs[0], xs[xs.len() - 1] - xs[xs.len() - 2]] {
        assert!(coppermesh::geom::is_pow2(d, 1e-9), "corner subsegment {} is not a power of two", d);
    }
}

#[test]
fn a_pad_edge_crossing_the_copper_edge_at_a_small_angle_keeps_quality_beside_it() {
    // A constraint leaves the bottom edge at ~48 degrees and ends on the right edge; Condition A
    // of SPLITPERMITTED lets the cluster be split where that creates no shorter edge.
    let mut g = Pslg::default();
    let apex = [X0 + 0.5, Y0];
    g.verts = vec![[X0, Y0], apex, [X0 + 4.0, Y0], [X0 + 4.0, Y0 + 3.9], [X0 + 4.0, Y0 + 4.0], [X0, Y0 + 4.0]];
    g.segs = vec![[0, 1], [1, 2], [2, 3], [3, 4], [4, 5], [5, 0], [1, 3]];
    let (p, t) = refined(&g, Rule::Area);
    assert_valid(&p, &t);
    assert!(min_angle_away_from(&p, &t, apex) > 20.0, "{}", min_angle_away_from(&p, &t, apex));
}

#[test]
fn retain_carves_by_a_mask_and_refinement_continues_inside() {
    // A hole ring meshed without its seed (KiPI's no-domain retry), then carved by centroid.
    let mut g = Pslg::default();
    let hole = rect(X0 + 1.0, Y0 + 1.0, X0 + 2.0, Y0 + 2.0);
    g.ring(&rect(X0, Y0, X0 + 4.0, Y0 + 3.0));
    g.ring(&hole);
    let mut m = build(&g);
    assert_eq!(m.refine(&params(Rule::Area)), Status::Done);
    let (p, t) = m.export();
    let keep: Vec<bool> = t.iter().map(|tri| !point_in_ring(centroid(&p, tri), &hole)).collect();
    assert!(keep.iter().any(|&k| !k));
    m.retain(&keep);
    m.check().unwrap();
    let (p, t) = m.export();
    assert!((mesh_area(&p, &t) - 11.0).abs() <= 1e-9 * 11.0);
    let h = vec![0.1; p.len()];
    assert_eq!(m.refine_sized(&h, &params(Rule::Area)), Status::Done);
    m.check().unwrap();
    let (p, t) = m.export();
    assert!((mesh_area(&p, &t) - 11.0).abs() <= 1e-9 * 11.0);
    assert!(t.iter().all(|tri| !point_in_ring(centroid(&p, tri), &hole)));
    // A cut across non-segment edges: the new boundary edges become subsegments.
    let keep: Vec<bool> = t.iter().map(|tri| centroid(&p, tri)[0] < X0 + 3.0).collect();
    m.retain(&keep);
    m.check().unwrap();
    assert_eq!(m.refine(&params(Rule::Area)), Status::Done);
    m.check().unwrap();
}

#[test]
fn graded_field_is_met_exactly_at_every_centroid() {
    // KiPI's graded_size evaluated natively: after the graded pass every triangle's area is at
    // most min(sqrt(3)/4 h(centroid)^2, max_area), with h computed the same way.
    let mut g = Pslg::default();
    g.ring(&rect(X0, Y0, X0 + 4.0, Y0 + 3.0));
    let centres = vec![[X0 + 1.0, Y0 + 1.5], [X0 + 1.75, Y0 + 1.5], [X0 + 3.0, Y0 + 1.0]];
    let radii = vec![0.15, 0.15, 0.25];
    for (c, r) in centres.iter().zip(&radii) {
        g.ring(&ngon(c[0], c[1], *r, 8));
        g.holes.push(*c);
    }
    let mut m = build(&g);
    assert_eq!(m.refine(&params(Rule::Area)), Status::Done);
    let field = coppermesh::Graded::new(&centres, &radii, 0.02, MAX_EDGE, 0.2);
    let size = |q: P| {
        let (mut d2, mut k) = (f64::INFINITY, 0);
        for (i, c) in centres.iter().enumerate() {
            let (dx, dy) = (q[0] - c[0], q[1] - c[1]);
            if dx * dx + dy * dy < d2 {
                d2 = dx * dx + dy * dy;
                k = i;
            }
        }
        (0.02 + 0.2 * (d2.sqrt() - radii[k]).max(0.0)).max(0.02).min(MAX_EDGE)
    };
    assert_eq!(m.refine_graded(field, &params(Rule::Area)), Status::Done);
    m.check().unwrap();
    let (p, t) = m.export();
    assert!(min_angle(&p, &t) >= 28.0 - 1e-9);
    for tri in &t {
        let q: Vec<P> = tri.iter().map(|&i| p[i as usize]).collect();
        let c = [(q[0][0] + q[1][0] + q[2][0]) / 3.0, (q[0][1] + q[1][1] + q[2][1]) / 3.0];
        let h = size(c);
        let lim = (0.4330127018922193 * h * h).min(max_area());
        assert!(signed_area2(&p, tri) / 2.0 <= lim, "area {} over limit {}", signed_area2(&p, tri) / 2.0, lim);
    }
}

#[test]
fn subsegments_are_the_conforming_pieces_of_the_input_segments() {
    let mut g = Pslg::default();
    let outer = rect(X0, Y0, X0 + 3.0, Y0 + 2.0);
    g.ring(&outer);
    let pad = rect(X0 + 1.0, Y0 + 0.5, X0 + 1.6, Y0 + 0.9);
    g.ring(&pad);
    let mut m = build(&g);
    assert_eq!(m.refine(&params(Rule::Area)), Status::Done);
    let (p, _) = m.export();
    let segs = m.subsegments();
    let len = |s: &[u32; 2]| { let (a, b) = (p[s[0] as usize], p[s[1] as usize]); (a[0] - b[0]).hypot(a[1] - b[1]) };
    let total: f64 = segs.iter().map(len).sum();
    let want = 2.0 * (3.0 + 2.0) + 2.0 * (0.6 + 0.4);
    assert!((total - want).abs() <= 1e-9 * want, "{} vs {}", total, want);
    let mut seen = std::collections::BTreeSet::new();
    for s in &segs {
        assert!(seen.insert((s[0].min(s[1]), s[0].max(s[1]))), "subsegment listed twice");
    }
}

/// A 2 x 3 mm rectangle whose bottom edge runs on `run` mm to an apex, the outline returning `len` mm at
/// `width` mm above that edge: a near-zero-angle sliver on a polygon edge (rotated by `rot` degrees).
fn sliver(width: f64, len: f64, run: f64, rot: f64) -> (Pslg, [P; 3]) {
    let (s, c) = rot.to_radians().sin_cos();
    let r = |x: f64, y: f64| [X0 + x * c - y * s, Y0 + x * s + y * c];
    let apex = (2.0 + run, 0.0);
    let back = (apex.0 - len, width);
    let mut g = Pslg::default();
    let pts = [r(0.0, 0.0), r(2.0, 0.0), r(apex.0, apex.1), r(back.0, back.1), r(back.0, 3.0), r(0.0, 3.0)];
    g.ring(&pts);
    (g, [pts[1], pts[2], pts[3]])
}

fn floor_params(min_edge: f64, tiny: f64, cap: usize) -> RefineParams {
    let mut p = params(Rule::Area);
    p.cap = cap;
    p.min_edge = min_edge;
    p.tiny_angle_deg = tiny;
    p
}

/// Edges shorter than `floor` that are not input segments, and triangles below 28 degrees that have a
/// vertex off the wedge's two sides (b..apex, apex..c).
fn floor_and_wedge_violations(g: &Pslg, p: &[P], t: &[[u32; 3]], side: [P; 3], floor: f64) -> (usize, usize) {
    let given: std::collections::BTreeSet<(u64, u64, u64, u64)> = g
        .segs
        .iter()
        .map(|s| {
            let (a, b) = (g.verts[s[0] as usize], g.verts[s[1] as usize]);
            let (a, b) = if (a[0], a[1]) <= (b[0], b[1]) { (a, b) } else { (b, a) };
            (a[0].to_bits(), a[1].to_bits(), b[0].to_bits(), b[1].to_bits())
        })
        .collect();
    let mut short = 0;
    for tri in t {
        for i in 0..3 {
            let (a, b) = (p[tri[i] as usize], p[tri[(i + 1) % 3] as usize]);
            let (a, b) = if (a[0], a[1]) <= (b[0], b[1]) { (a, b) } else { (b, a) };
            if (a[0] - b[0]).hypot(a[1] - b[1]) < floor && !given.contains(&(a[0].to_bits(), a[1].to_bits(), b[0].to_bits(), b[1].to_bits())) {
                short += 1;
            }
        }
    }
    let on_side = |q: P| {
        [(side[0], side[1]), (side[1], side[2])].iter().any(|&(a, b)| {
            let d = [b[0] - a[0], b[1] - a[1]];
            let l2 = d[0] * d[0] + d[1] * d[1];
            let u = (((q[0] - a[0]) * d[0] + (q[1] - a[1]) * d[1]) / l2).clamp(0.0, 1.0);
            (a[0] + u * d[0] - q[0]).hypot(a[1] + u * d[1] - q[1]) <= 1e-9
        })
    };
    let ang = angles(p, t);
    let bad = t
        .iter()
        .zip(&ang)
        .filter(|(tri, a)| a.iter().cloned().fold(180.0, f64::min) < 28.0 - 1e-9 && !tri.iter().all(|&v| on_side(p[v as usize])))
        .count();
    (short, bad)
}

#[test]
fn a_near_zero_angle_sliver_is_accepted_as_it_is_and_nothing_is_made_below_the_floor() {
    for rot in [0.0, 30.0, 31.0, 60.0] {
        let (g, side) = sliver(1e-6, 0.05, 0.1, rot);
        let mut m = build(&g);
        assert_eq!(m.refine(&floor_params(1e-6, 1.0, 21_000)), Status::Done, "rot {}", rot);
        m.check().unwrap();
        let (p, t) = m.export();
        assert_valid(&p, &t);
        let (short, bad) = floor_and_wedge_violations(&g, &p, &t, side, 1e-6);
        assert_eq!((short, bad), (0, 0), "rot {}: edges under the floor, triangles under 28 deg off the wedge", rot);
        assert!(t.len() < 300, "rot {}: {} triangles", rot, t.len());
    }
}

#[test]
fn the_floor_alone_adds_no_edge_below_it() {
    let (g, side) = sliver(1e-6, 0.05, 0.1, 30.0);
    let mut m = build(&g);
    assert_eq!(m.refine(&floor_params(1e-6, 0.0, 1_000_000)), Status::Done);
    m.check().unwrap();
    let (p, t) = m.export();
    let (short, _) = floor_and_wedge_violations(&g, &p, &t, side, 1e-6);
    assert_eq!(short, 0);
    assert!(m.stats.floored > 0);
}

#[test]
fn wedges_above_the_tiny_angle_mesh_exactly_as_without_the_rules() {
    for deg in [2.0f64, 5.0] {
        let a = deg.to_radians();
        let mut g = Pslg::default();
        g.ring(&[[X0, Y0], [X0 + 5.0, Y0], [X0 + 3.3 * a.cos(), Y0 + 3.3 * a.sin()]]);
        let mut m0 = build(&g);
        assert_eq!(m0.refine(&params(Rule::Area)), Status::Done);
        let mut m1 = build(&g);
        assert_eq!(m1.refine(&floor_params(1e-6, 1.0, 1_000_000)), Status::Done);
        let ((p0, t0), (p1, t1)) = (m0.export(), m1.export());
        assert_eq!(t0, t1);
        assert!(p0.iter().zip(&p1).all(|(x, y)| x[0].to_bits() == y[0].to_bits() && x[1].to_bits() == y[1].to_bits()));
    }
}

#[test]
fn the_floor_holds_around_interior_points_closer_than_it() {
    // KiPI's interior points are not snapped: two of them 0.5 nm apart make triangles whose circumcentres
    // would sit a fraction of a nanometre from a vertex.
    let mut g = Pslg::default();
    g.ring(&rect(X0, Y0, X0 + 1.0, Y0 + 1.0));
    let q = [X0 + 0.4123456789, Y0 + 0.5432109876];
    g.point(q);
    g.point([q[0] + 0.5e-6, q[1] + 0.2e-6]);
    g.point([q[0] + 0.3e-6, q[1] - 0.6e-6]);
    let mut m = build(&g);
    assert_eq!(m.refine(&floor_params(1e-6, 0.0, 1_000_000)), Status::Done);
    m.check().unwrap();
    let (p, t) = m.export();
    let input: std::collections::BTreeSet<(u64, u64)> = g.verts.iter().map(|v| (v[0].to_bits(), v[1].to_bits())).collect();
    let is_input = |v: P| input.contains(&(v[0].to_bits(), v[1].to_bits()));
    let made_short = t
        .iter()
        .flat_map(|tri| (0..3).map(move |i| (tri[i], tri[(i + 1) % 3])))
        .filter(|&(a, b)| {
            let (pa, pb) = (p[a as usize], p[b as usize]);
            (pa[0] - pb[0]).hypot(pa[1] - pb[1]) < 1e-6 && !(is_input(pa) && is_input(pb))
        })
        .count();
    assert_eq!(made_short, 0, "edges under the floor at an added vertex");
}
