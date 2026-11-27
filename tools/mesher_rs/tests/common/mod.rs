//! Helpers shared by the integration tests: polygons as PSLGs, and mesh measurements.
#![allow(dead_code)]

use std::collections::{BTreeMap, BTreeSet};

pub type P = [f64; 2];
pub const X0: f64 = 120.0; // board-like coordinates: an ulp is ~1e-14 mm here
pub const Y0: f64 = 80.0;

/// A PSLG under construction: vertices, segments, hole seeds, extra points.
#[derive(Default, Clone)]
pub struct Pslg {
    pub verts: Vec<P>,
    pub segs: Vec<[u32; 2]>,
    pub holes: Vec<P>,
}

impl Pslg {
    /// Add a closed ring (a polygon outline) of the given points.
    pub fn ring(&mut self, pts: &[P]) {
        let base = self.verts.len() as u32;
        self.verts.extend_from_slice(pts);
        let n = pts.len() as u32;
        for k in 0..n {
            self.segs.push([base + k, base + (k + 1) % n]);
        }
    }
    /// Add an open polyline.
    pub fn line(&mut self, pts: &[P]) {
        let base = self.verts.len() as u32;
        self.verts.extend_from_slice(pts);
        for k in 0..pts.len() as u32 - 1 {
            self.segs.push([base + k, base + k + 1]);
        }
    }
    pub fn point(&mut self, p: P) {
        self.verts.push(p);
    }
}

pub fn rect(x0: f64, y0: f64, x1: f64, y1: f64) -> Vec<P> {
    vec![[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
}

/// A regular n-gon, as KiPI's drill holes (8-gons) are.
pub fn ngon(cx: f64, cy: f64, r: f64, n: usize) -> Vec<P> {
    (0..n)
        .map(|k| {
            let a = 2.0 * std::f64::consts::PI * k as f64 / n as f64;
            [cx + r * a.cos(), cy + r * a.sin()]
        })
        .collect()
}

pub fn signed_area2(p: &[P], t: &[u32; 3]) -> f64 {
    let (a, b, c) = (p[t[0] as usize], p[t[1] as usize], p[t[2] as usize]);
    (b[0] - a[0]) * (c[1] - a[1]) - (c[0] - a[0]) * (b[1] - a[1])
}

pub fn polygon_area(ring: &[P]) -> f64 {
    let n = ring.len();
    let mut s = 0.0;
    for k in 0..n {
        let (a, b) = (ring[k], ring[(k + 1) % n]);
        s += a[0] * b[1] - b[0] * a[1];
    }
    s.abs() / 2.0
}

pub fn mesh_area(p: &[P], tris: &[[u32; 3]]) -> f64 {
    tris.iter().map(|t| signed_area2(p, t).abs() / 2.0).sum()
}

/// The three angles of every triangle, in degrees.
pub fn angles(p: &[P], tris: &[[u32; 3]]) -> Vec<[f64; 3]> {
    tris.iter()
        .map(|t| {
            let q: Vec<P> = t.iter().map(|&i| p[i as usize]).collect();
            let mut out = [0.0; 3];
            for i in 0..3 {
                let (a, b, c) = (q[i], q[(i + 1) % 3], q[(i + 2) % 3]);
                let u = [b[0] - a[0], b[1] - a[1]];
                let w = [c[0] - a[0], c[1] - a[1]];
                let cosv = (u[0] * w[0] + u[1] * w[1]) / ((u[0].hypot(u[1])) * (w[0].hypot(w[1])));
                out[i] = cosv.clamp(-1.0, 1.0).acos().to_degrees();
            }
            out
        })
        .collect()
}

pub fn min_angle(p: &[P], tris: &[[u32; 3]]) -> f64 {
    angles(p, tris).iter().flat_map(|a| a.iter().copied()).fold(180.0, f64::min)
}

pub fn max_edge(p: &[P], tris: &[[u32; 3]]) -> f64 {
    let mut m: f64 = 0.0;
    for t in tris {
        for i in 0..3 {
            let (a, b) = (p[t[i] as usize], p[t[(i + 1) % 3] as usize]);
            m = m.max((a[0] - b[0]).hypot(a[1] - b[1]));
        }
    }
    m
}

pub fn centroid(p: &[P], t: &[u32; 3]) -> P {
    let mut c = [0.0, 0.0];
    for &i in t {
        c[0] += p[i as usize][0] / 3.0;
        c[1] += p[i as usize][1] / 3.0;
    }
    c
}

pub fn point_in_ring(q: P, ring: &[P]) -> bool {
    let n = ring.len();
    let mut inside = false;
    for k in 0..n {
        let (a, b) = (ring[k], ring[(k + 1) % n]);
        if (a[1] > q[1]) != (b[1] > q[1]) {
            let x = a[0] + (q[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1]);
            if q[0] < x {
                inside = !inside;
            }
        }
    }
    inside
}

/// Every output vertex is used, no triangle has zero area, all are counter-clockwise.
pub fn assert_valid(p: &[P], tris: &[[u32; 3]]) {
    assert!(!tris.is_empty(), "no triangles");
    let mut used = vec![false; p.len()];
    for t in tris {
        assert!(signed_area2(p, t) > 0.0, "triangle not counter-clockwise or zero area: {:?}", t);
        for &i in t {
            used[i as usize] = true;
        }
    }
    assert!(used.iter().all(|&u| u), "orphan node");
    // Each undirected edge belongs to one or two triangles, and an interior edge is used once
    // in each direction (a manifold, consistently oriented mesh).
    let mut dir: BTreeSet<(u32, u32)> = BTreeSet::new();
    for t in tris {
        for i in 0..3 {
            let e = (t[i], t[(i + 1) % 3]);
            assert!(dir.insert(e), "directed edge used twice: {:?}", e);
        }
    }
}

/// Whether input segment (a, b) is the union of mesh edges: walk from a to b along mesh edges
/// whose vertices lie on the segment (within `tol` of its line) and progress toward b.
pub fn segment_conforms(p: &[P], tris: &[[u32; 3]], a: P, b: P) -> bool {
    let find = |q: P| p.iter().position(|&x| x == q);
    let (ia, ib) = match (find(a), find(b)) {
        (Some(x), Some(y)) => (x as u32, y as u32),
        _ => return false,
    };
    let mut adj: BTreeMap<u32, BTreeSet<u32>> = BTreeMap::new();
    for t in tris {
        for i in 0..3 {
            let (u, v) = (t[i], t[(i + 1) % 3]);
            adj.entry(u).or_default().insert(v);
            adj.entry(v).or_default().insert(u);
        }
    }
    let d = [b[0] - a[0], b[1] - a[1]];
    let len = d[0].hypot(d[1]);
    let along = |q: P| ((q[0] - a[0]) * d[0] + (q[1] - a[1]) * d[1]) / len;
    let off = |q: P| (((q[0] - a[0]) * d[1] - (q[1] - a[1]) * d[0]) / len).abs();
    let tol = 1e-9 * len.max(1.0);
    let mut cur = ia;
    let mut guard = 0;
    while cur != ib {
        guard += 1;
        if guard > 1_000_000 {
            return false;
        }
        let here = along(p[cur as usize]);
        let next = adj
            .get(&cur)
            .into_iter()
            .flat_map(|s| s.iter().copied())
            .filter(|&v| {
                let q = p[v as usize];
                off(q) <= tol && along(q) > here + 1e-15
            })
            .min_by(|&u, &v| along(p[u as usize]).partial_cmp(&along(p[v as usize])).unwrap());
        match next {
            Some(v) => cur = v,
            None => return false,
        }
    }
    true
}
