//! Delaunay refinement: Ruppert's algorithm in the form of Shewchuk's "Terminator"
//! (Shewchuk 2002, §3.1, §6.2-6.3, Appendix A, Figs. 38-39), with diametral circles,
//! concentric-shell segment splitting and SPLITPERMITTED with subsegment clusters; plus a
//! per-node size field interpolated on a frozen background mesh.

use crate::geom::*;
use crate::mesh::{Mesh, VKind, NEXT, NONE, PREV};
use std::cmp::Ordering;
use std::collections::{BinaryHeap, VecDeque};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Rule {
    /// KiPI's convention: a triangle is too large when its area exceeds
    /// min(sqrt(3)/4 h^2, max_area).
    Area,
    /// The longest edge may not exceed min(h, h_max), h_max the edge of the equilateral
    /// triangle of area max_area.
    Edge,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Status {
    Done,
    CapHit,
}

#[derive(Clone, Debug)]
pub struct RefineParams {
    pub min_angle_deg: f64,
    pub max_area: f64,
    /// At most this many points are added by one refine call (KiPI's Steiner cap).
    pub cap: usize,
    pub rule: Rule,
    /// No point is added closer than this to an existing vertex (0: no floor). The backend sets
    /// KiPI's 1 nm noding grid: refinement never goes below the resolution of its input.
    pub min_edge: f64,
    /// Two input segments meeting at an angle below this (degrees; 0: none) bound a tiny wedge.
    /// The wedge is accepted as it is, a documented quality exception: its sides are not split
    /// for each other's vertices, and a skinny triangle with every vertex on its two sides is
    /// not refined (see DESIGN.md).
    pub tiny_angle_deg: f64,
}

impl RefineParams {
    pub fn new(min_angle_deg: f64, max_area: f64, cap: usize, rule: Rule) -> Self {
        RefineParams { min_angle_deg, max_area, cap, rule, min_edge: 0.0, tiny_angle_deg: 0.0 }
    }
}

const SQRT3_4: f64 = 0.4330127018922193; // sqrt(3)/4
const POW2_TOL: f64 = 1e-9;

/// The pass-1 mesh frozen as the background of a size field: h is linear in each triangle.
pub(crate) struct Background {
    tri: Vec<[u32; 3]>,
    nbr: Vec<[u32; 3]>,
    hint: Vec<u32>,
}

impl Background {
    fn snapshot(m: &Mesh) -> Background {
        let mut index = vec![NONE; m.tris.len()];
        let mut tri = vec![];
        for (k, t) in m.tris.iter().enumerate() {
            if t.alive {
                index[k] = tri.len() as u32;
                tri.push(t.v);
            }
        }
        let nbr = m
            .tris
            .iter()
            .filter(|t| t.alive)
            .map(|t| t.n.map(|n| if n == NONE { NONE } else { index[n as usize] }))
            .collect();
        let hint = m.vtri.iter().map(|&t| if t == NONE { NONE } else { index[t as usize] }).collect();
        Background { tri, nbr, hint }
    }

    /// h at q (barycentric in the background triangle found by a walk from the background
    /// triangle of vertex `near`), and that triangle.
    fn eval(&self, m: &Mesh, q: P, near: u32) -> (f64, u32, bool) {
        let mut t = *self.hint.get(near as usize).unwrap_or(&NONE);
        if t == NONE {
            t = 0;
        }
        let mut scanned = false;
        let limit = 2 * self.tri.len() + 64;
        let mut found = false;
        for step in 0..limit {
            let v = self.tri[t as usize];
            let p = v.map(|x| m.p(x));
            let mut moved = false;
            let mut outside = false;
            for j in 0..3 {
                let i = (step + j) % 3;
                if orient(p[NEXT[i]], p[PREV[i]], q) < 0.0 {
                    outside = true;
                    let nb = self.nbr[t as usize][i];
                    if nb != NONE {
                        t = nb;
                        moved = true;
                        break;
                    }
                }
            }
            if !moved {
                found = !outside;
                break;
            }
        }
        if !found {
            // Beyond the boundary by rounding, or the walk was blocked by a hole: scan.
            for (k, v) in self.tri.iter().enumerate() {
                let p = v.map(|x| m.p(x));
                if orient(p[1], p[2], q) >= 0.0 && orient(p[2], p[0], q) >= 0.0 && orient(p[0], p[1], q) >= 0.0 {
                    t = k as u32;
                    break;
                }
            }
            scanned = true;
        }
        let v = self.tri[t as usize];
        let h = interpolate(m.p(v[0]), m.p(v[1]), m.p(v[2]), m.h[v[0] as usize], m.h[v[1] as usize], m.h[v[2] as usize], q);
        (h, t, scanned)
    }
}

/// A skinny triangle in the heap: worst ratio first, then first queued.
#[derive(PartialEq)]
struct Item {
    key: f64,
    seq: u64,
    t: u32,
    v: [u32; 3],
}
impl Eq for Item {}
impl PartialOrd for Item {
    fn partial_cmp(&self, o: &Self) -> Option<Ordering> {
        Some(self.cmp(o))
    }
}
impl Ord for Item {
    fn cmp(&self, o: &Self) -> Ordering {
        self.key.total_cmp(&o.key).then_with(|| o.seq.cmp(&self.seq))
    }
}

enum Walk {
    Inside(u32),
    Blocked(u32, u32),
    Fail,
}

struct Cluster {
    min_half_chord: f64, // min over neighbouring members of |u^ - w^| / 2 = sin(phi / 2)
    shortest: f64,
}

struct Refiner<'a> {
    m: &'a mut Mesh,
    cos2: f64,
    max_area: f64,
    h_max2: f64,
    rule: Rule,
    cap: usize,
    added: usize,
    floor2: f64,
    tiny: Vec<(u32, u32)>, // pairs of input segments meeting at a tiny angle, both orders, sorted
    qs: VecDeque<(u32, u32, bool)>, // a subsegment to split; true: a rejected circumcentre asked
    heap: BinaryHeap<Item>,
    fifo: VecDeque<(u32, [u32; 3])>,
    seq: u64,
}

impl Mesh {
    /// Refine to the angle bound and the uniform size bound.
    pub fn refine(&mut self, prm: &RefineParams) -> Status {
        self.bg = None;
        self.graded = None;
        for h in self.h.iter_mut() {
            *h = f64::INFINITY;
        }
        Refiner::new(self, prm).run()
    }

    /// Refine further to a size field given at the nodes of the last export (h[k] for
    /// exported node k): the current mesh is frozen as the background, h is linear in its
    /// triangles, and every new point takes its h by interpolation there.
    pub fn refine_sized(&mut self, h: &[f64], prm: &RefineParams) -> Status {
        assert_eq!(h.len(), self.export_map.len(), "one h per exported node");
        for v in self.h.iter_mut() {
            *v = f64::INFINITY;
        }
        for (k, &v) in self.export_map.iter().enumerate() {
            self.h[v as usize] = if h[k] > 0.0 { h[k] } else { f64::INFINITY };
        }
        self.graded = None;
        self.bg = Some(Background::snapshot(self));
        let st = Refiner::new(self, prm).run();
        self.bg = None;
        st
    }

    /// Refine further to KiPI's graded size field, evaluated exactly at each triangle's
    /// centroid (the protocol's limit: area <= min(sqrt(3)/4 h^2, max_area)).
    pub fn refine_graded(&mut self, field: crate::size::Graded, prm: &RefineParams) -> Status {
        self.bg = None;
        for v in self.h.iter_mut() {
            *v = f64::INFINITY;
        }
        self.graded = Some(field);
        let st = Refiner::new(self, prm).run();
        self.graded = None;
        st
    }

    fn size_at(&mut self, q: P, near: u32) -> f64 {
        match &self.bg {
            None => f64::INFINITY,
            Some(bg) => {
                let (h, t, scanned) = bg.eval(self, q, near);
                if scanned {
                    self.stats.bg_scans += 1;
                }
                let v = self.pts.len(); // the vertex about to be added
                if let Some(bg) = self.bg.as_mut() {
                    if bg.hint.len() <= v {
                        bg.hint.resize(v + 1, NONE);
                    }
                    bg.hint[v] = t;
                }
                h
            }
        }
    }
}

/// The pairs of input segments that share an end and meet there at less than `deg` degrees
/// (both orders, sorted): the sides of tiny wedges.
fn tiny_pairs(m: &Mesh, deg: f64) -> Vec<(u32, u32)> {
    let s2 = sin2_deg(deg);
    let mut out = vec![];
    for v in 0..m.on_segs.len() as u32 {
        let segs = &m.on_segs[v as usize];
        if segs.len() < 2 {
            continue;
        }
        let pv = m.p(v);
        let dir = |sid: u32| {
            let [x, y] = m.in_segs[sid as usize];
            if v == x {
                Some(sub(m.p(y), pv))
            } else if v == y {
                Some(sub(m.p(x), pv))
            } else {
                None // v lies inside that segment
            }
        };
        for i in 0..segs.len() {
            for j in i + 1..segs.len() {
                if let (Some(u), Some(w)) = (dir(segs[i]), dir(segs[j])) {
                    let (d, c) = (dot(u, w), u[0] * w[1] - u[1] * w[0]);
                    if d > 0.0 && c * c < s2 * dot(u, u) * dot(w, w) {
                        out.push((segs[i], segs[j]));
                        out.push((segs[j], segs[i]));
                    }
                }
            }
        }
    }
    out.sort_unstable();
    out.dedup();
    out
}

impl<'a> Refiner<'a> {
    fn partner(&self, s1: u32, s2: u32) -> bool {
        self.tiny.binary_search(&(s1, s2)).is_ok()
    }

    /// Whether vertex w lies on a segment that meets input segment `sid` at a tiny angle.
    fn on_partner(&self, w: u32, sid: u32) -> bool {
        self.m.segs_of(w).iter().any(|&s| self.partner(s, sid))
    }

    /// Whether every vertex of the triangle lies on the two sides of one tiny wedge.
    fn in_tiny_wedge(&self, v: [u32; 3]) -> bool {
        let m = &*self.m;
        for &s1 in m.segs_of(v[0]) {
            let lo = self.tiny.partition_point(|&(a, _)| a < s1);
            for &(_, s2) in self.tiny[lo..].iter().take_while(|&&(a, _)| a == s1) {
                if v[1..].iter().all(|&x| m.on_seg(x, s1) || m.on_seg(x, s2)) {
                    return true;
                }
            }
        }
        false
    }

    fn new(m: &'a mut Mesh, prm: &RefineParams) -> Self {
        let deg = prm.min_angle_deg.clamp(0.0, 60.0);
        let h_max = (4.0 * prm.max_area / 3f64.sqrt()).sqrt();
        let tiny = if prm.tiny_angle_deg > 0.0 { tiny_pairs(m, prm.tiny_angle_deg) } else { vec![] };
        Refiner {
            m,
            cos2: if deg > 0.0 { cos2_deg(deg) } else { 2.0 },
            max_area: if prm.max_area > 0.0 { prm.max_area } else { f64::INFINITY },
            h_max2: if prm.max_area > 0.0 { h_max * h_max } else { f64::INFINITY },
            rule: prm.rule,
            cap: prm.cap,
            added: 0,
            floor2: prm.min_edge * prm.min_edge,
            tiny,
            qs: VecDeque::new(),
            heap: BinaryHeap::new(),
            fifo: VecDeque::new(),
            seq: 0,
        }
    }

    /// (skinny, oversize, ratio^2 key) when the triangle is bad.
    fn classify(&self, t: u32) -> Option<(bool, bool, f64)> {
        let m = &*self.m;
        let tri = m.tris[t as usize];
        let p = tri.v.map(|v| m.p(v));
        let l2: [f64; 3] = [0, 1, 2].map(|i| dist2(p[NEXT[i]], p[PREV[i]]));
        let mut skinny = false;
        for i in 0..3 {
            if tri.seg & (1 << NEXT[i]) != 0 && tri.seg & (1 << PREV[i]) != 0 {
                continue; // an angle separating two subsegments is never fixed
            }
            let d = dot(sub(p[NEXT[i]], p[i]), sub(p[PREV[i]], p[i]));
            if d > 0.0 && d * d > self.cos2 * l2[PREV[i]] * l2[NEXT[i]] {
                skinny = true;
                break;
            }
        }
        let area2 = (p[1][0] - p[0][0]) * (p[2][1] - p[0][1]) - (p[2][0] - p[0][0]) * (p[1][1] - p[0][1]);
        let hc = match &m.graded {
            Some(g) => g.eval([(p[0][0] + p[1][0] + p[2][0]) / 3.0, (p[0][1] + p[1][1] + p[2][1]) / 3.0]),
            None => (m.h[tri.v[0] as usize] + m.h[tri.v[1] as usize] + m.h[tri.v[2] as usize]) / 3.0,
        };
        let oversize = match self.rule {
            Rule::Area => area2 / 2.0 > (SQRT3_4 * hc * hc).min(self.max_area),
            Rule::Edge => l2[0].max(l2[1]).max(l2[2]) > (hc * hc).min(self.h_max2),
        };
        if !skinny && !oversize {
            return None;
        }
        if skinny && !oversize && !self.tiny.is_empty() && self.in_tiny_wedge(tri.v) {
            return None; // inside a tiny wedge: accepted as it is
        }
        let lmin = l2[0].min(l2[1]).min(l2[2]);
        let key = l2[0] * l2[1] * l2[2] / (lmin * 4.0 * area2 * area2);
        Some((skinny, oversize, key))
    }

    fn enqueue(&mut self, t: u32) {
        if let Some((skinny, _, key)) = self.classify(t) {
            let v = self.m.tris[t as usize].v;
            if skinny {
                self.seq += 1;
                self.heap.push(Item { key, seq: self.seq, t, v });
            } else {
                self.fifo.push_back((t, v));
            }
        }
    }

    fn seg_encroached(&self, a: u32, b: u32) -> bool {
        let m = &*self.m;
        for (x, y) in [(a, b), (b, a)] {
            if let Some((t, i)) = m.find_edge(x, y) {
                let w = m.tris[t as usize].v[i];
                if encroaches(m.p(a), m.p(b), m.p(w)) {
                    return true;
                }
            }
        }
        false
    }

    /// NEWVERTEX (Fig. 39): queue the subsegments v encroaches and the bad triangles at v.
    fn new_vertex(&mut self, v: u32) {
        let (fan, _) = self.m.star(v);
        for t in fan {
            let k = self.m.local(t, v);
            if self.m.is_seg(t, k) {
                let (a, b) = self.m.edge(t, k);
                if encroaches(self.m.p(a), self.m.p(b), self.m.p(v)) {
                    self.qs.push_back((a, b, false));
                    continue;
                }
            }
            self.enqueue(t);
        }
    }

    fn hit_cap(&mut self) -> bool {
        self.added += 1;
        self.added >= self.cap
    }

    /// SPLITENCROACHEDSUBSEGMENTS (Fig. 39). Returns the number of splits, or Err on the cap.
    fn split_encroached(&mut self) -> Result<usize, ()> {
        let mut done = 0;
        while let Some((a, b, asked)) = self.qs.pop_front() {
            let m = &*self.m;
            let (t, i) = match m.find_edge(a, b).or_else(|| m.find_edge(b, a)) {
                Some(e) => e,
                None => continue, // no longer an edge: already split
            };
            if !m.is_seg(t, i) {
                continue;
            }
            let sid = m.seg_id(a, b);
            if !asked && !self.tiny.is_empty() && sid != NONE && self.only_partners_encroach(a, b, sid) {
                self.m.stats.tiny += 1; // encroached only from the other side of a tiny wedge
                continue;
            }
            let m = &*self.m;
            let (pa, pb) = (m.p(a), m.p(b));
            let len = dist2(pa, pb).sqrt();
            let shared = |v: u32| m.kind[v as usize] == VKind::Input && m.nseg[v as usize] >= 2;
            let q = if shared(a) {
                let f = shell_distance(len) / len;
                [pa[0] + (pb[0] - pa[0]) * f, pa[1] + (pb[1] - pa[1]) * f]
            } else if shared(b) {
                let f = shell_distance(len) / len;
                [pb[0] + (pa[0] - pb[0]) * f, pb[1] + (pa[1] - pb[1]) * f]
            } else {
                [0.5 * (pa[0] + pb[0]), 0.5 * (pa[1] + pb[1])]
            };
            if q == pa || q == pb {
                self.m.stats.dropped += 1;
                continue;
            }
            if self.floor2 > 0.0 && self.split_near2(q, t, i) < self.floor2 {
                self.m.stats.floored += 1; // the subsegment stays: below the floor
                continue;
            }
            let h = self.m.size_at(q, a);
            let v = self.m.add_vertex(q, VKind::Segment);
            self.m.h[v as usize] = h;
            if self.m.split_edge(t, i, v).is_none() {
                self.m.stats.dropped += 1;
                self.pop_vertex(v);
                continue;
            }
            self.m.vseg[v as usize] = sid;
            self.m.stats.segment_splits += 1;
            done += 1;
            if self.hit_cap() {
                return Err(());
            }
            self.new_vertex(v);
            for (x, y) in [(a, v), (v, b)] {
                if self.seg_encroached(x, y) {
                    self.qs.push_back((x, y, false));
                }
            }
        }
        Ok(done)
    }

    /// Undo add_vertex for a vertex that could not be inserted (it is the last one).
    fn pop_vertex(&mut self, v: u32) {
        let m = &mut *self.m;
        debug_assert_eq!(v as usize, m.pts.len() - 1);
        m.pts.pop();
        m.kind.pop();
        m.nseg.pop();
        m.vtri.pop();
        m.h.pop();
        m.vseg.pop();
    }

    /// Whether subsegment (a, b) of input segment `sid` is encroached by some mesh vertex and
    /// only by vertices on segments that meet `sid` at a tiny angle.
    fn only_partners_encroach(&self, a: u32, b: u32, sid: u32) -> bool {
        let m = &*self.m;
        let mut any = false;
        for (x, y) in [(a, b), (b, a)] {
            if let Some((t, i)) = m.find_edge(x, y) {
                let w = m.tris[t as usize].v[i];
                if encroaches(m.p(a), m.p(b), m.p(w)) {
                    if !self.on_partner(w, sid) {
                        return false;
                    }
                    any = true;
                }
            }
        }
        any
    }

    /// Straight walk from t's centroid toward c (Shewchuk 2002, App. A).
    fn walk(&self, t0: u32, c: P) -> Walk {
        let m = &*self.m;
        let q0 = m.tris[t0 as usize].v.map(|v| m.p(v));
        let s = [(q0[0][0] + q0[1][0] + q0[2][0]) / 3.0, (q0[0][1] + q0[1][1] + q0[2][1]) / 3.0];
        let mut t = t0;
        for _ in 0..(m.tris.len() + 16) {
            let tri = m.tris[t as usize];
            let q = tri.v.map(|v| m.p(v));
            let o = [orient(q[1], q[2], c), orient(q[2], q[0], c), orient(q[0], q[1], c)];
            if o[0] >= 0.0 && o[1] >= 0.0 && o[2] >= 0.0 {
                return Walk::Inside(t);
            }
            let mut exit = None;
            for i in 0..3 {
                if o[i] < 0.0 && orient(s, c, q[NEXT[i]]) <= 0.0 && orient(s, c, q[PREV[i]]) >= 0.0 {
                    exit = Some(i);
                    break;
                }
            }
            let i = match exit.or_else(|| (0..3).find(|&i| o[i] < 0.0)) {
                Some(i) => i,
                None => return Walk::Fail,
            };
            if tri.seg & (1 << i) != 0 || tri.n[i] == NONE {
                let (a, b) = m.edge(t, i);
                return Walk::Blocked(a, b);
            }
            t = tri.n[i];
        }
        Walk::Fail
    }

    /// The subsegments c would encroach if inserted: those on the boundary of its
    /// Bowyer-Watson cavity (triangles whose circumcircle holds c, reached without crossing a
    /// subsegment). None if c coincides with a vertex there.
    fn encroached_by(&mut self, c: P, tc: u32) -> Option<(Vec<(u32, u32)>, f64)> {
        let m = &mut *self.m;
        m.epoch += 1;
        let e = m.epoch;
        m.mark[tc as usize] = e;
        let mut stack = vec![tc];
        let mut out: Vec<(u32, u32)> = vec![];
        let mut near2 = f64::INFINITY; // the new vertex's shortest edge, squared
        while let Some(t) = stack.pop() {
            let tri = m.tris[t as usize];
            for &v in &tri.v {
                if m.pts[v as usize] == c {
                    return None;
                }
                near2 = near2.min(dist2(m.pts[v as usize], c));
            }
            for i in 0..3 {
                if tri.seg & (1 << i) != 0 {
                    let (a, b) = (tri.v[NEXT[i]], tri.v[PREV[i]]);
                    if encroaches(m.pts[a as usize], m.pts[b as usize], c)
                        && !out.iter().any(|&(x, y)| (x == a && y == b) || (x == b && y == a))
                    {
                        out.push((a, b));
                    }
                    continue;
                }
                let nb = tri.n[i];
                if nb == NONE || m.mark[nb as usize] == e {
                    continue;
                }
                let w = m.tris[nb as usize].v;
                if incircle(m.pts[w[0] as usize], m.pts[w[1] as usize], m.pts[w[2] as usize], c) > 0.0 {
                    m.mark[nb as usize] = e;
                    stack.push(nb);
                }
            }
        }
        Some((out, near2))
    }

    /// The squared distance from q, a split point of edge i of t, to the nearest vertex it
    /// would be joined to: the vertices of its Bowyer-Watson cavity, grown from the one or two
    /// triangles on that edge without crossing another subsegment.
    fn split_near2(&mut self, q: P, t: u32, i: usize) -> f64 {
        let m = &mut *self.m;
        m.epoch += 1;
        let e = m.epoch;
        let mut stack = vec![t];
        m.mark[t as usize] = e;
        let other = m.tris[t as usize].n[i];
        if other != NONE {
            m.mark[other as usize] = e;
            stack.push(other);
        }
        let mut near2 = f64::INFINITY;
        while let Some(t) = stack.pop() {
            let tri = m.tris[t as usize];
            for &v in &tri.v {
                near2 = near2.min(dist2(m.pts[v as usize], q));
            }
            for k in 0..3 {
                let nb = tri.n[k];
                if tri.seg & (1 << k) != 0 || nb == NONE || m.mark[nb as usize] == e {
                    continue;
                }
                let w = m.tris[nb as usize].v;
                if incircle(m.pts[w[0] as usize], m.pts[w[1] as usize], m.pts[w[2] as usize], q) > 0.0 {
                    m.mark[nb as usize] = e;
                    stack.push(nb);
                }
            }
        }
        near2
    }

    /// The subsegment cluster of s = (a, b) at input vertex a (Shewchuk 2002 §6.3): the run of
    /// subsegments at a, consecutive inside the domain, separated by 60 degrees or less.
    /// None when s is alone (no small input angle at a).
    fn cluster(&self, a: u32, b: u32) -> Option<Cluster> {
        let m = &*self.m;
        if m.kind[a as usize] != VKind::Input {
            return None;
        }
        let (fan, closed) = m.star(a);
        if fan.is_empty() {
            return None;
        }
        // Subsegment endpoints around a, counter-clockwise.
        let mut ends: Vec<u32> = vec![];
        let k0 = m.local(fan[0], a);
        if !closed && m.is_seg(fan[0], PREV[k0]) {
            ends.push(m.tris[fan[0] as usize].v[NEXT[k0]]);
        }
        for &t in &fan {
            let k = m.local(t, a);
            if m.is_seg(t, NEXT[k]) {
                ends.push(m.tris[t as usize].v[PREV[k]]);
            }
        }
        let n = ends.len();
        let idx = ends.iter().position(|&q| q == b)?;
        if n < 2 {
            return None;
        }
        let pa = m.p(a);
        let unit = |q: u32| {
            let d = sub(m.p(q), pa);
            let l = dot(d, d).sqrt();
            ([d[0] / l, d[1] / l], l)
        };
        // Whether the counter-clockwise angle from u to w is at most 60 degrees.
        let small = |u: P, w: P| u[0] * w[1] - u[1] * w[0] >= 0.0 && dot(u, w) >= 0.5;
        let (mut min_hc, mut shortest) = (f64::INFINITY, unit(b).1);
        let mut members = 1;
        let mut j = idx;
        loop {
            let nx = if j + 1 < n { j + 1 } else if closed { 0 } else { break };
            if nx == idx {
                break;
            }
            let (u, _) = unit(ends[j]);
            let (w, lw) = unit(ends[nx]);
            if !small(u, w) {
                break;
            }
            min_hc = min_hc.min(dist2(u, w).sqrt() / 2.0);
            shortest = shortest.min(lw);
            members += 1;
            j = nx;
        }
        let last_ccw = j;
        let mut j = idx;
        loop {
            let pv = if j > 0 { j - 1 } else if closed { n - 1 } else { break };
            if pv == idx || pv == last_ccw {
                break;
            }
            let (u, lu) = unit(ends[pv]);
            let (w, _) = unit(ends[j]);
            if !small(u, w) {
                break;
            }
            min_hc = min_hc.min(dist2(u, w).sqrt() / 2.0);
            shortest = shortest.min(lu);
            members += 1;
            j = pv;
        }
        if members < 2 {
            return None;
        }
        Some(Cluster { min_half_chord: min_hc, shortest })
    }

    /// SPLITPERMITTED(s, d) (Fig. 38), conditions A, C, D, E; d = shortest edge of the bad
    /// triangle (the §6.5 substitute for r_g).
    fn split_permitted(&self, a: u32, b: u32, d: f64) -> bool {
        let ca = self.cluster(a, b);
        let cb = self.cluster(b, a);
        let c = match (ca, cb) {
            (None, None) | (Some(_), Some(_)) => return true,
            (Some(c), None) | (None, Some(c)) => c,
        };
        let len = dist2(self.m.p(a), self.m.p(b)).sqrt();
        if !is_pow2(len, POW2_TOL) {
            return true;
        }
        if c.shortest < len * (1.0 - POW2_TOL) {
            return true;
        }
        len * c.min_half_chord >= d
    }

    fn pop_bad(&mut self) -> Option<(u32, [u32; 3])> {
        if let Some(it) = self.heap.pop() {
            return Some((it.t, it.v));
        }
        self.fifo.pop_front()
    }

    fn run(mut self) -> Status {
        // Initial encroached subsegments, then the bad triangles (Fig. 38, lines 3-9).
        let m = &*self.m;
        for (k, tri) in m.tris.iter().enumerate() {
            if !tri.alive {
                continue;
            }
            for i in 0..3 {
                if tri.seg & (1 << i) != 0 {
                    let (a, b) = m.edge(k as u32, i);
                    if encroaches(m.p(a), m.p(b), m.p(tri.v[i])) {
                        self.qs.push_back((a, b, false));
                    }
                }
            }
        }
        if self.split_encroached().is_err() {
            return Status::CapHit;
        }
        for k in 0..self.m.tris.len() {
            if self.m.tris[k].alive {
                self.enqueue(k as u32);
            }
        }
        while let Some((t, v)) = self.pop_bad() {
            let tri = self.m.tris[t as usize];
            if !tri.alive || tri.v != v {
                continue;
            }
            let (_, oversize, _) = match self.classify(t) {
                Some(c) => c,
                None => continue,
            };
            let p = v.map(|x| self.m.p(x));
            let c = circumcenter(p[0], p[1], p[2]);
            if !c[0].is_finite() || !c[1].is_finite() {
                self.m.stats.dropped += 1;
                continue;
            }
            let enc = match self.walk(t, c) {
                Walk::Fail => {
                    self.m.stats.dropped += 1;
                    continue;
                }
                Walk::Blocked(a, b) => vec![(a, b)],
                Walk::Inside(tc) => match self.encroached_by(c, tc) {
                    None => {
                        self.m.stats.dropped += 1;
                        continue;
                    }
                    Some((e, near2)) if e.is_empty() => {
                        if near2 < self.floor2 {
                            self.m.stats.floored += 1; // the triangle stays: below the floor
                            continue;
                        }
                        let h = self.m.size_at(c, v[0]);
                        let nv = self.m.add_vertex(c, VKind::Free);
                        self.m.h[nv as usize] = h;
                        if self.m.insert_in(tc, nv, false).is_none() {
                            self.pop_vertex(nv);
                            self.m.stats.dropped += 1;
                            continue;
                        }
                        self.m.stats.circumcenters += 1;
                        if self.hit_cap() {
                            return Status::CapHit;
                        }
                        self.new_vertex(nv);
                        if !self.qs.is_empty() && self.split_encroached().is_err() {
                            return Status::CapHit;
                        }
                        continue;
                    }
                    Some((e, _)) => e,
                },
            };
            // The circumcentre is rejected (Fig. 38, lines 18-25).
            self.m.stats.rejected += 1;
            let d = [0usize, 1, 2].map(|i| dist2(p[NEXT[i]], p[PREV[i]])).iter().cloned().fold(f64::INFINITY, f64::min).sqrt();
            for (a, b) in enc {
                if oversize || self.split_permitted(a, b, d) {
                    self.qs.push_back((a, b, true));
                } else {
                    self.m.stats.declined += 1;
                }
            }
            if !self.qs.is_empty() {
                match self.split_encroached() {
                    Err(()) => return Status::CapHit,
                    Ok(0) => {}
                    Ok(_) => self.enqueue(t), // put t back for another try
                }
            }
        }
        Status::Done
    }
}
