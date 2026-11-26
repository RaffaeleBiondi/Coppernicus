//! The triangulation: an array of counter-clockwise triangles with neighbour links and a
//! subsegment bit per edge. Construction of the constrained Delaunay triangulation (CDT):
//! vertices by Lawson insertion in Hilbert order inside a super-triangle (de Berg et al.,
//! ch. 9), segments by Sloan's flips (Sloan 1993), then carving by flood fill from the
//! super-triangle and the hole seeds, never across a segment.
//!
//! Conventions: edge i of a triangle joins v[(i+1)%3] -> v[(i+2)%3] (it is opposite v[i]);
//! n[i] is the neighbour across it (NONE on the domain boundary); bit i of `seg` marks it as a
//! subsegment.

use crate::geom::*;
use std::collections::{HashMap, VecDeque};

pub const NONE: u32 = u32::MAX;
pub(crate) const NEXT: [usize; 3] = [1, 2, 0];
pub(crate) const PREV: [usize; 3] = [2, 0, 1];
const N_SUPER: usize = 3;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum VKind {
    Super,
    Input,
    Segment,
    Free,
}

#[derive(Clone, Copy, Debug)]
pub(crate) struct Tri {
    pub v: [u32; 3],
    pub n: [u32; 3],
    pub seg: u8,
    pub alive: bool,
}

/// Counters for the benchmark records.
#[derive(Clone, Debug, Default)]
pub struct Stats {
    pub input_vertices: usize,
    pub duplicates: usize,
    pub segment_splits: usize,
    pub circumcenters: usize,
    pub rejected: usize,
    pub declined: usize,
    pub dropped: usize,
    pub floored: usize,
    pub tiny: usize,
    pub bg_scans: usize,
}

pub struct Mesh {
    pub(crate) pts: Vec<P>,
    pub(crate) kind: Vec<VKind>,
    pub(crate) nseg: Vec<u32>,
    pub(crate) vtri: Vec<u32>,
    pub(crate) h: Vec<f64>,
    pub(crate) tris: Vec<Tri>,
    pub(crate) free: Vec<u32>,
    pub(crate) mark: Vec<u32>,
    pub(crate) epoch: u32,
    pub(crate) export_map: Vec<u32>,
    pub(crate) export_tris: Vec<u32>,
    pub(crate) bg: Option<crate::refine::Background>,
    pub(crate) graded: Option<crate::size::Graded>,
    /// The input segment a Segment vertex lies on (NONE for other vertices).
    pub(crate) vseg: Vec<u32>,
    /// The input segments (after aliasing), the input segment of each piece between two input
    /// vertices, and the input segments each input vertex is an end of a piece of.
    pub(crate) in_segs: Vec<[u32; 2]>,
    pub(crate) piece: HashMap<(u32, u32), u32>,
    pub(crate) on_segs: Vec<Vec<u32>>,
    pub stats: Stats,
}

/// Hilbert index of (x, y) on a 2^16 grid (the classic iterative mapping).
fn hilbert(mut x: u32, mut y: u32) -> u64 {
    let n: u32 = 1 << 16;
    let mut d: u64 = 0;
    let mut s = n >> 1;
    while s > 0 {
        let rx = ((x & s) > 0) as u32;
        let ry = ((y & s) > 0) as u32;
        d += (s as u64) * (s as u64) * (((3 * rx) ^ ry) as u64);
        if ry == 0 {
            if rx == 1 {
                x = n - 1 - x;
                y = n - 1 - y;
            }
            std::mem::swap(&mut x, &mut y);
        }
        s >>= 1;
    }
    d
}

#[inline]
fn sgn(x: f64) -> i8 {
    if x > 0.0 {
        1
    } else if x < 0.0 {
        -1
    } else {
        0
    }
}

impl Mesh {
    // ------------------------------------------------------------------ construction

    /// The CDT of `verts` and `segs` (indices into `verts`), less what the flood fill from
    /// outside and from each of `holes` reaches without crossing a segment.
    pub fn build(verts: &[P], segs: &[[u32; 2]], holes: &[P]) -> Result<Mesh, String> {
        if verts.len() < 3 {
            return Err("fewer than 3 vertices".into());
        }
        if verts.iter().any(|p| !p[0].is_finite() || !p[1].is_finite()) {
            return Err("non-finite coordinate".into());
        }
        let (mut x0, mut y0, mut x1, mut y1) = (f64::MAX, f64::MAX, f64::MIN, f64::MIN);
        for p in verts {
            x0 = x0.min(p[0]);
            y0 = y0.min(p[1]);
            x1 = x1.max(p[0]);
            y1 = y1.max(p[1]);
        }
        let size = (x1 - x0).max(y1 - y0).max(1e-3);
        let (cx, cy) = ((x0 + x1) / 2.0, (y0 + y1) / 2.0);
        let k = 100.0 * size;
        let mut m = Mesh {
            pts: vec![[cx - 2.0 * k, cy - k], [cx + 2.0 * k, cy - k], [cx, cy + 2.0 * k]],
            kind: vec![VKind::Super; N_SUPER],
            nseg: vec![0; N_SUPER],
            vtri: vec![0; N_SUPER],
            h: vec![f64::INFINITY; N_SUPER],
            tris: vec![Tri { v: [0, 1, 2], n: [NONE; 3], seg: 0, alive: true }],
            free: vec![],
            mark: vec![0],
            epoch: 0,
            export_map: vec![],
            export_tris: vec![],
            bg: None,
            graded: None,
            vseg: vec![NONE; N_SUPER],
            in_segs: vec![],
            piece: HashMap::new(),
            on_segs: vec![vec![]; N_SUPER + verts.len()],
            stats: Stats { input_vertices: verts.len(), ..Default::default() },
        };
        for p in verts {
            m.pts.push(*p);
            m.kind.push(VKind::Input);
            m.nseg.push(0);
            m.vtri.push(NONE);
            m.h.push(f64::INFINITY);
            m.vseg.push(NONE);
        }
        // Insertion in Hilbert order (ties by input index): each point is near the last.
        let (w, hh) = ((x1 - x0).max(1e-300), (y1 - y0).max(1e-300));
        let mut order: Vec<(u64, u32)> = verts
            .iter()
            .enumerate()
            .map(|(i, p)| {
                let gx = (((p[0] - x0) / w) * 65535.0) as u32;
                let gy = (((p[1] - y0) / hh) * 65535.0) as u32;
                (hilbert(gx.min(65535), gy.min(65535)), i as u32)
            })
            .collect();
        order.sort_unstable();
        let mut alias: Vec<u32> = (0..verts.len() as u32).map(|i| i + N_SUPER as u32).collect();
        let mut last = 0u32;
        for &(_, i) in &order {
            let id = i + N_SUPER as u32;
            let p = m.pts[id as usize];
            let (t, at) = m.locate(p, last).ok_or("point location failed")?;
            if let Some(v) = at {
                alias[i as usize] = v;
                m.stats.duplicates += 1;
                m.kind[id as usize] = VKind::Free; // never referenced
                last = t;
                continue;
            }
            m.insert_in(t, id, false).ok_or("degenerate vertex insertion")?;
            last = m.vtri[id as usize];
        }
        // Segments, in input order.
        for s in segs {
            if s[0] as usize >= verts.len() || s[1] as usize >= verts.len() {
                return Err("segment index out of range".into());
            }
            let (a, b) = (alias[s[0] as usize], alias[s[1] as usize]);
            if a == b {
                continue;
            }
            m.nseg[a as usize] += 1;
            m.nseg[b as usize] += 1;
            let sid = m.in_segs.len() as u32;
            m.in_segs.push([a, b]);
            m.insert_segment(a, b, sid)?;
        }
        m.carve(holes);
        Ok(m)
    }

    // ------------------------------------------------------------------ topology helpers

    #[inline]
    pub(crate) fn p(&self, v: u32) -> P {
        self.pts[v as usize]
    }

    #[inline]
    pub(crate) fn edge(&self, t: u32, i: usize) -> (u32, u32) {
        let v = self.tris[t as usize].v;
        (v[NEXT[i]], v[PREV[i]])
    }

    #[inline]
    pub(crate) fn is_seg(&self, t: u32, i: usize) -> bool {
        self.tris[t as usize].seg & (1 << i) != 0
    }

    #[inline]
    pub(crate) fn local(&self, t: u32, v: u32) -> usize {
        let tv = self.tris[t as usize].v;
        if tv[0] == v {
            0
        } else if tv[1] == v {
            1
        } else {
            debug_assert!(tv[2] == v);
            2
        }
    }

    /// Index of the edge {a, b} in triangle t.
    #[inline]
    fn edge_index(&self, t: u32, a: u32, b: u32) -> usize {
        let v = self.tris[t as usize].v;
        for i in 0..3 {
            let (x, y) = (v[NEXT[i]], v[PREV[i]]);
            if (x == a && y == b) || (x == b && y == a) {
                return i;
            }
        }
        panic!("edge not in triangle");
    }

    fn alloc(&mut self, t: Tri) -> u32 {
        if let Some(k) = self.free.pop() {
            self.tris[k as usize] = t;
            k
        } else {
            self.tris.push(t);
            self.mark.push(0);
            (self.tris.len() - 1) as u32
        }
    }

    pub(crate) fn add_vertex(&mut self, p: P, kind: VKind) -> u32 {
        self.pts.push(p);
        self.kind.push(kind);
        self.nseg.push(0);
        self.vtri.push(NONE);
        self.h.push(f64::INFINITY);
        self.vseg.push(NONE);
        (self.pts.len() - 1) as u32
    }

    /// The input segment subsegment (a, b) is a piece of (NONE for an edge made a subsegment by
    /// carving).
    pub(crate) fn seg_id(&self, a: u32, b: u32) -> u32 {
        if self.kind[a as usize] == VKind::Segment {
            return self.vseg[a as usize];
        }
        if self.kind[b as usize] == VKind::Segment {
            return self.vseg[b as usize];
        }
        *self.piece.get(&(a.min(b), a.max(b))).unwrap_or(&NONE)
    }

    /// Whether vertex v lies on input segment `sid`.
    pub(crate) fn on_seg(&self, v: u32, sid: u32) -> bool {
        match self.kind[v as usize] {
            VKind::Segment => self.vseg[v as usize] == sid,
            VKind::Input => self.on_segs.get(v as usize).is_some_and(|l| l.contains(&sid)),
            _ => false,
        }
    }

    /// The input segments vertex v lies on.
    pub(crate) fn segs_of(&self, v: u32) -> &[u32] {
        match self.kind[v as usize] {
            VKind::Segment => std::slice::from_ref(&self.vseg[v as usize]),
            VKind::Input => self.on_segs.get(v as usize).map(|l| l.as_slice()).unwrap_or(&[]),
            _ => &[],
        }
    }

    /// Point a neighbour's link that pointed at `old` across edge {a, b} to `new`.
    fn relink(&mut self, nb: u32, a: u32, b: u32, new: u32) {
        if nb != NONE {
            let j = self.edge_index(nb, a, b);
            self.tris[nb as usize].n[j] = new;
        }
    }

    /// The triangle on the left of the directed edge a -> b and the edge's index in it.
    pub(crate) fn find_edge(&self, a: u32, b: u32) -> Option<(u32, usize)> {
        let t0 = self.vtri[a as usize];
        if t0 == NONE {
            return None;
        }
        let mut t = t0;
        loop {
            let k = self.local(t, a);
            let tv = self.tris[t as usize].v;
            if tv[NEXT[k]] == b {
                return Some((t, PREV[k]));
            }
            t = self.tris[t as usize].n[NEXT[k]]; // counter-clockwise around a
            if t == NONE {
                break;
            }
            if t == t0 {
                return None;
            }
        }
        let mut t = t0;
        loop {
            let k = self.local(t, a);
            let tv = self.tris[t as usize].v;
            if tv[NEXT[k]] == b {
                return Some((t, PREV[k]));
            }
            t = self.tris[t as usize].n[PREV[k]]; // clockwise around a
            if t == NONE || t == t0 {
                return None;
            }
        }
    }

    /// The triangles around vertex a in counter-clockwise order, and whether they close a
    /// full circle (an interior vertex) or form an open fan (a boundary vertex).
    pub(crate) fn star(&self, a: u32) -> (Vec<u32>, bool) {
        let t0 = self.vtri[a as usize];
        let mut out = vec![];
        if t0 == NONE {
            return (out, false);
        }
        // Walk clockwise to the start of an open fan, if there is one.
        let mut start = t0;
        loop {
            let k = self.local(start, a);
            let nb = self.tris[start as usize].n[PREV[k]];
            if nb == NONE {
                break;
            }
            if nb == t0 {
                start = t0;
                break;
            }
            start = nb;
        }
        let mut t = start;
        loop {
            out.push(t);
            let k = self.local(t, a);
            let nb = self.tris[t as usize].n[NEXT[k]];
            if nb == NONE {
                return (out, false);
            }
            if nb == start {
                return (out, true);
            }
            t = nb;
        }
    }

    // ------------------------------------------------------------------ point location

    /// Visibility walk from triangle `start` to the triangle containing p; also returns the
    /// vertex p coincides with, if any. With boundary edges (after carving) it stops at the
    /// last triangle when p lies beyond the boundary.
    pub(crate) fn locate(&self, p: P, start: u32) -> Option<(u32, Option<u32>)> {
        let mut t = if start != NONE && (start as usize) < self.tris.len() && self.tris[start as usize].alive {
            start
        } else {
            self.tris.iter().position(|t| t.alive)? as u32
        };
        let limit = 3 * self.tris.len() + 64;
        for step in 0..limit {
            let tri = self.tris[t as usize];
            for &v in &tri.v {
                if self.p(v) == p {
                    return Some((t, Some(v)));
                }
            }
            let mut moved = false;
            for j in 0..3 {
                let i = (step + j) % 3;
                let (a, b) = (tri.v[NEXT[i]], tri.v[PREV[i]]);
                if orient(self.p(a), self.p(b), p) < 0.0 && tri.n[i] != NONE {
                    t = tri.n[i];
                    moved = true;
                    break;
                }
            }
            if !moved {
                return Some((t, None));
            }
        }
        self.locate_scan(p).map(|t| (t, None))
    }

    /// The first live triangle containing p (closed), by a full scan.
    pub(crate) fn locate_scan(&self, p: P) -> Option<u32> {
        for (k, tri) in self.tris.iter().enumerate() {
            if !tri.alive {
                continue;
            }
            let q = tri.v.map(|v| self.p(v));
            if orient(q[1], q[2], p) >= 0.0 && orient(q[2], q[0], p) >= 0.0 && orient(q[0], q[1], p) >= 0.0 {
                return Some(k as u32);
            }
        }
        None
    }

    // ------------------------------------------------------------------ insertion

    /// Insert existing vertex `v` (its coordinates already in `pts`) into triangle t, which
    /// contains it (closed): a 1-to-3 split, or a split of the edge it lies on; then restore the
    /// constrained Delaunay property with flips (Lawson). Returns None, changing nothing, when
    /// the insertion would make a degenerate triangle. With `on_seg`, an edge it lies on that
    /// is a subsegment is split too (both halves stay subsegments).
    pub(crate) fn insert_in(&mut self, t: u32, v: u32, on_seg: bool) -> Option<()> {
        let tri = self.tris[t as usize];
        let p = self.p(v);
        let q = tri.v.map(|x| self.p(x));
        let o = [orient(q[1], q[2], p), orient(q[2], q[0], p), orient(q[0], q[1], p)];
        let zeros: Vec<usize> = (0..3).filter(|&i| o[i] == 0.0).collect();
        if o.iter().any(|&x| x < 0.0) || zeros.len() > 1 {
            return None;
        }
        if let Some(&i) = zeros.first() {
            if self.is_seg(t, i) && !on_seg {
                return None;
            }
            return self.split_edge(t, i, v);
        }
        self.split_tri(t, v);
        Some(())
    }

    fn split_tri(&mut self, t: u32, v: u32) {
        let tri = self.tris[t as usize];
        let [a, b, c] = tri.v;
        let [na, nb, nc] = tri.n;
        let s = |i: usize| (tri.seg >> i) & 1;
        let t0 = t;
        let t1 = self.alloc(Tri { v: [v, c, a], n: [nb, NONE, t0], seg: s(1), alive: true });
        let t2 = self.alloc(Tri { v: [v, a, b], n: [nc, t0, t1], seg: s(2), alive: true });
        self.tris[t1 as usize].n[1] = t2;
        self.tris[t0 as usize] = Tri { v: [v, b, c], n: [na, t1, t2], seg: s(0), alive: true };
        self.relink(nb, c, a, t1);
        self.relink(nc, a, b, t2);
        self.vtri[v as usize] = t0;
        self.vtri[a as usize] = t1;
        self.vtri[b as usize] = t0;
        self.vtri[c as usize] = t0;
        self.legalize(v, vec![(t0, 0), (t1, 0), (t2, 0)]);
    }

    /// Split edge i of t at vertex v (which lies on it, up to rounding for subsegment split
    /// points). Checks that every new triangle is counter-clockwise first.
    pub(crate) fn split_edge(&mut self, t: u32, i: usize, v: u32) -> Option<()> {
        let tri = self.tris[t as usize];
        let x = tri.v[i];
        let (u, w) = (tri.v[NEXT[i]], tri.v[PREV[i]]);
        let keep = (tri.seg >> i) & 1;
        let t2 = tri.n[i];
        let p = self.p(v);
        if orient(self.p(x), self.p(u), p) <= 0.0 || orient(self.p(x), p, self.p(w)) <= 0.0 {
            return None;
        }
        let mut y = NONE;
        let mut j = 0;
        if t2 != NONE {
            j = self.edge_index(t2, u, w);
            y = self.tris[t2 as usize].v[j];
            if orient(self.p(y), self.p(w), p) <= 0.0 || orient(self.p(y), p, self.p(u)) <= 0.0 {
                return None;
            }
        }
        let s1 = |k: usize| (tri.seg >> k) & 1;
        let (a_n, b_n) = (tri.n[NEXT[i]], tri.n[PREV[i]]); // across (w,x) and (x,u)
        let ta = t;
        let tb = self.alloc(Tri {
            v: [x, v, w],
            n: [NONE, a_n, ta],
            seg: keep | (s1(NEXT[i]) << 1),
            alive: true,
        });
        self.tris[ta as usize] =
            Tri { v: [x, u, v], n: [NONE, tb, b_n], seg: keep | (s1(PREV[i]) << 2), alive: true };
        self.relink(a_n, w, x, tb);
        self.vtri[v as usize] = ta;
        self.vtri[x as usize] = ta;
        self.vtri[u as usize] = ta;
        self.vtri[w as usize] = tb;
        let mut stack = vec![(ta, 2), (tb, 1)];
        if t2 != NONE {
            let tri2 = self.tris[t2 as usize];
            let s2 = |k: usize| (tri2.seg >> k) & 1;
            let (c_n, d_n) = (tri2.n[NEXT[j]], tri2.n[PREV[j]]); // across (u,y) and (y,w)
            let sb = t2;
            let sa = self.alloc(Tri {
                v: [y, v, u],
                n: [ta, c_n, sb],
                seg: keep | (s2(NEXT[j]) << 1),
                alive: true,
            });
            self.tris[sb as usize] =
                Tri { v: [y, w, v], n: [tb, sa, d_n], seg: keep | (s2(PREV[j]) << 2), alive: true };
            self.relink(c_n, u, y, sa);
            self.tris[ta as usize].n[0] = sa;
            self.tris[tb as usize].n[0] = sb;
            self.vtri[y as usize] = sb;
            stack.push((sb, 2));
            stack.push((sa, 1));
        }
        self.legalize(v, stack);
        Some(())
    }

    /// Flip edge i of t1 (shared with its neighbour). With t1 = (x, u, w) and the neighbour
    /// (y, w, u), the new triangles are (x, u, y) in slot t1 and (y, w, x) in the neighbour's.
    pub(crate) fn flip(&mut self, t1: u32, i: usize) -> (u32, u32) {
        let a = self.tris[t1 as usize];
        let t2 = a.n[i];
        let x = a.v[i];
        let (u, w) = (a.v[NEXT[i]], a.v[PREV[i]]);
        let j = self.edge_index(t2, u, w);
        let b = self.tris[t2 as usize];
        let y = b.v[j];
        let sa = |k: usize| (a.seg >> k) & 1;
        let sb = |k: usize| (b.seg >> k) & 1;
        let (an, bn) = (a.n[NEXT[i]], a.n[PREV[i]]); // across (w,x), (x,u)
        let (cn, dn) = (b.n[NEXT[j]], b.n[PREV[j]]); // across (u,y), (y,w)
        self.tris[t1 as usize] =
            Tri { v: [x, u, y], n: [cn, t2, bn], seg: sb(NEXT[j]) | (sa(PREV[i]) << 2), alive: true };
        self.tris[t2 as usize] =
            Tri { v: [y, w, x], n: [an, t1, dn], seg: sa(NEXT[i]) | (sb(PREV[j]) << 2), alive: true };
        self.relink(cn, u, y, t1);
        self.relink(an, w, x, t2);
        self.vtri[x as usize] = t1;
        self.vtri[u as usize] = t1;
        self.vtri[y as usize] = t2;
        self.vtri[w as usize] = t2;
        (t1, t2)
    }

    /// Lawson's edge legalisation around a new vertex v: every edge opposite v that is not a
    /// subsegment and not locally Delaunay is flipped.
    fn legalize(&mut self, v: u32, mut stack: Vec<(u32, usize)>) {
        while let Some((t, i)) = stack.pop() {
            let tri = self.tris[t as usize];
            if !tri.alive || tri.v[i] != v || tri.seg & (1 << i) != 0 || tri.n[i] == NONE {
                continue;
            }
            let nb = tri.n[i];
            let (u, w) = (tri.v[NEXT[i]], tri.v[PREV[i]]);
            let j = self.edge_index(nb, u, w);
            let y = self.tris[nb as usize].v[j];
            let q = tri.v.map(|x| self.p(x));
            if incircle(q[0], q[1], q[2], self.p(y)) > 0.0 {
                let (t1, t2) = self.flip(t, i);
                stack.push((t1, 0));
                stack.push((t2, 2));
            }
        }
    }

    // ------------------------------------------------------------------ segments

    fn mark_seg(&mut self, a: u32, b: u32) {
        if let Some((t, i)) = self.find_edge(a, b) {
            self.tris[t as usize].seg |= 1 << i;
        }
        if let Some((t, i)) = self.find_edge(b, a) {
            self.tris[t as usize].seg |= 1 << i;
        }
    }

    /// Make segment a-b a chain of subsegments: vertices lying exactly on it split it; the
    /// edges it crosses are removed by Sloan's flips, then the new edges are made Delaunay.
    /// Mark a piece of input segment `sid` as a subsegment and remember where it came from.
    fn mark_piece(&mut self, a: u32, b: u32, sid: u32) {
        self.mark_seg(a, b);
        self.piece.insert((a.min(b), a.max(b)), sid);
        for v in [a, b] {
            if let Some(l) = self.on_segs.get_mut(v as usize) {
                if !l.contains(&sid) {
                    l.push(sid);
                }
            }
        }
    }

    fn insert_segment(&mut self, a0: u32, b: u32, sid: u32) -> Result<(), String> {
        let mut a = a0;
        let pb = self.p(b);
        let mut guard = 0usize;
        while a != b {
            guard += 1;
            if guard > self.pts.len() + 8 {
                return Err("segment recovery did not progress".into());
            }
            if self.find_edge(a, b).is_some() {
                self.mark_piece(a, b, sid);
                return Ok(());
            }
            let pa = self.p(a);
            let (fan, _) = self.star(a);
            let mut next_a = NONE;
            let mut first = None;
            for &t in &fan {
                let k = self.local(t, a);
                let tv = self.tris[t as usize].v;
                let (p1, p2) = (tv[NEXT[k]], tv[PREV[k]]);
                let o1 = orient(pa, self.p(p1), pb);
                if o1 == 0.0 && dot(sub(self.p(p1), pa), sub(pb, pa)) > 0.0 {
                    next_a = p1; // a vertex exactly on the segment
                    break;
                }
                let o2 = orient(pa, self.p(p2), pb);
                if o1 > 0.0 && o2 < 0.0 {
                    first = Some((t, k, p1, p2));
                    break;
                }
            }
            if next_a != NONE {
                self.mark_piece(a, next_a, sid);
                a = next_a;
                continue;
            }
            let (mut t, k, mut p1, mut p2) = first.ok_or("segment direction not found around its vertex")?;
            // Walk the triangles the segment crosses; p1 stays right of a->b, p2 left.
            let mut crossing: VecDeque<(u32, u32)> = VecDeque::new();
            let mut i = k;
            let mut stop = b;
            loop {
                if self.is_seg(t, i) {
                    return Err("segments cross".into());
                }
                let nb = self.tris[t as usize].n[i];
                if nb == NONE {
                    return Err("segment leaves the triangulation".into());
                }
                crossing.push_back((p1, p2));
                let j = self.edge_index(nb, p1, p2);
                let q = self.tris[nb as usize].v[j];
                if q == b {
                    break;
                }
                let oq = orient(pa, pb, self.p(q));
                if oq == 0.0 {
                    stop = q;
                    break;
                }
                if oq < 0.0 {
                    p1 = q;
                } else {
                    p2 = q;
                }
                t = nb;
                i = self.edge_index(nb, p1, p2);
            }
            self.recover(a, stop, crossing)?;
            self.mark_piece(a, stop, sid);
            a = stop;
        }
        Ok(())
    }

    fn recover(&mut self, a: u32, c: u32, mut queue: VecDeque<(u32, u32)>) -> Result<(), String> {
        let (pa, pc) = (self.p(a), self.p(c));
        let mut new_edges: Vec<(u32, u32)> = vec![];
        let mut stall = 0usize;
        while let Some((u, w)) = queue.pop_front() {
            let (t1, i1) = self.find_edge(u, w).ok_or("lost a crossing edge")?;
            let x = self.tris[t1 as usize].v[i1];
            let t2 = self.tris[t1 as usize].n[i1];
            let j = self.edge_index(t2, u, w);
            let y = self.tris[t2 as usize].v[j];
            let (px, py) = (self.p(x), self.p(y));
            let convex = sgn(orient(px, py, self.p(u))) * sgn(orient(px, py, self.p(w))) < 0;
            if convex {
                self.flip(t1, i1);
                stall = 0;
                let crosses = x != a
                    && x != c
                    && y != a
                    && y != c
                    && sgn(orient(pa, pc, px)) * sgn(orient(pa, pc, py)) < 0;
                if crosses {
                    queue.push_back((x, y));
                } else {
                    new_edges.push((x, y));
                }
            } else {
                queue.push_back((u, w));
                stall += 1;
                if stall > queue.len() + 1 {
                    return Err("segment recovery stalled".into());
                }
            }
        }
        // Restore the Delaunay property on the new edges (the segment itself excepted).
        let mut rounds = 0;
        loop {
            let mut swapped = false;
            for k in 0..new_edges.len() {
                let (u, w) = new_edges[k];
                if (u == a && w == c) || (u == c && w == a) {
                    continue;
                }
                let (t1, i1) = match self.find_edge(u, w) {
                    Some(e) => e,
                    None => continue,
                };
                if self.is_seg(t1, i1) || self.tris[t1 as usize].n[i1] == NONE {
                    continue;
                }
                let t2 = self.tris[t1 as usize].n[i1];
                let j = self.edge_index(t2, u, w);
                let y = self.tris[t2 as usize].v[j];
                let q = self.tris[t1 as usize].v.map(|v| self.p(v));
                if incircle(q[0], q[1], q[2], self.p(y)) > 0.0 {
                    let x = self.tris[t1 as usize].v[i1];
                    self.flip(t1, i1);
                    new_edges[k] = (x, y);
                    swapped = true;
                }
            }
            rounds += 1;
            if !swapped || rounds > 10_000 {
                break;
            }
        }
        Ok(())
    }

    // ------------------------------------------------------------------ carving

    /// Delete every triangle reachable, without crossing a subsegment, from the super-
    /// triangle's vertices or from a hole seed.
    pub(crate) fn carve(&mut self, holes: &[P]) {
        self.epoch += 1;
        let e = self.epoch;
        let mut stack: Vec<u32> = vec![];
        for (k, tri) in self.tris.iter().enumerate() {
            if tri.alive && tri.v.iter().any(|&v| (v as usize) < N_SUPER) {
                stack.push(k as u32);
            }
        }
        for &k in &stack {
            self.mark[k as usize] = e;
        }
        let mut hint = 0u32;
        for &h in holes {
            if !h[0].is_finite() || !h[1].is_finite() {
                continue;
            }
            if let Some((t, _)) = self.locate(h, hint) {
                hint = t;
                let q = self.tris[t as usize].v.map(|v| self.p(v));
                let inside = orient(q[1], q[2], h) >= 0.0 && orient(q[2], q[0], h) >= 0.0 && orient(q[0], q[1], h) >= 0.0;
                if inside && self.mark[t as usize] != e {
                    self.mark[t as usize] = e;
                    stack.push(t);
                }
            }
        }
        while let Some(t) = stack.pop() {
            for i in 0..3 {
                if self.is_seg(t, i) {
                    continue;
                }
                let nb = self.tris[t as usize].n[i];
                if nb != NONE && self.mark[nb as usize] != e {
                    self.mark[nb as usize] = e;
                    stack.push(nb);
                }
            }
        }
        for k in 0..self.tris.len() {
            if self.tris[k].alive && self.mark[k] == e {
                self.tris[k].alive = false;
                self.free.push(k as u32);
            }
        }
        for k in 0..self.tris.len() {
            if !self.tris[k].alive {
                continue;
            }
            for i in 0..3 {
                let nb = self.tris[k].n[i];
                if nb != NONE && !self.tris[nb as usize].alive {
                    self.tris[k].n[i] = NONE;
                }
            }
        }
        self.free.sort_unstable_by(|a, b| b.cmp(a)); // reuse low slots first, deterministically
        self.reset_vtri();
    }

    pub(crate) fn reset_vtri(&mut self) {
        for v in self.vtri.iter_mut() {
            *v = NONE;
        }
        for k in 0..self.tris.len() {
            if self.tris[k].alive {
                for &v in &self.tris[k].v {
                    self.vtri[v as usize] = k as u32;
                }
            }
        }
    }

    // ------------------------------------------------------------------ output

    /// The live triangles and the vertices they use (compacted, in vertex order: input
    /// vertices first, bit-exact). Remembers the compact-to-internal map for refine_sized.
    pub fn export(&mut self) -> (Vec<P>, Vec<[u32; 3]>) {
        let mut id = vec![NONE; self.pts.len()];
        for tri in self.tris.iter().filter(|t| t.alive) {
            for &v in &tri.v {
                id[v as usize] = 0;
            }
        }
        let mut pts = vec![];
        self.export_map.clear();
        for v in 0..self.pts.len() {
            if id[v] != NONE {
                id[v] = pts.len() as u32;
                pts.push(self.pts[v]);
                self.export_map.push(v as u32);
            }
        }
        self.export_tris = (0..self.tris.len() as u32).filter(|&k| self.tris[k as usize].alive).collect();
        let tris = self.export_tris.iter().map(|&k| self.tris[k as usize].v.map(|v| id[v as usize])).collect();
        (pts, tris)
    }

    /// Keep only the triangles of the last export whose `keep` flag is set (KiPI's carving by
    /// centroid); the edges that become boundary are made subsegments.
    pub fn retain(&mut self, keep: &[bool]) {
        assert_eq!(keep.len(), self.export_tris.len(), "one flag per exported triangle");
        let mut gone = false;
        for (k, &t) in self.export_tris.iter().enumerate() {
            if !keep[k] && self.tris[t as usize].alive {
                self.tris[t as usize].alive = false;
                self.free.push(t);
                gone = true;
            }
        }
        if !gone {
            return;
        }
        for k in 0..self.tris.len() {
            if !self.tris[k].alive {
                continue;
            }
            for i in 0..3 {
                let nb = self.tris[k].n[i];
                if nb != NONE && !self.tris[nb as usize].alive {
                    self.tris[k].n[i] = NONE;
                    self.tris[k].seg |= 1 << i;
                }
            }
        }
        self.free.sort_unstable_by(|a, b| b.cmp(a));
        self.reset_vtri();
        self.export_tris.clear();
        self.export_map.clear();
    }

    /// Area of the domain (the live triangles).
    pub fn area(&self) -> f64 {
        let mut a = 0.0;
        for t in self.tris.iter().filter(|t| t.alive) {
            let q = t.v.map(|v| self.p(v));
            a += ((q[1][0] - q[0][0]) * (q[2][1] - q[0][1]) - (q[2][0] - q[0][0]) * (q[1][1] - q[0][1])).abs() / 2.0;
        }
        a
    }

    /// The subsegments (pieces of input segments) as pairs of node indices of the last export,
    /// each once, in triangle order.
    pub fn subsegments(&self) -> Vec<[u32; 2]> {
        let mut id = vec![NONE; self.pts.len()];
        for (k, &v) in self.export_map.iter().enumerate() {
            id[v as usize] = k as u32;
        }
        let mut out = vec![];
        for &t in &self.export_tris {
            let tri = self.tris[t as usize];
            for i in 0..3 {
                if tri.seg & (1 << i) != 0 && (tri.n[i] == NONE || tri.n[i] > t) {
                    let (a, b) = self.edge(t, i);
                    out.push([id[a as usize], id[b as usize]]);
                }
            }
        }
        out
    }

    /// Triangles in the last export (retain takes one flag per triangle).
    pub fn n_exported_triangles(&self) -> usize {
        self.export_tris.len()
    }

    /// Nodes in the last export (refine_sized takes one h per node).
    pub fn n_exported(&self) -> usize {
        self.export_map.len()
    }

    pub fn n_triangles(&self) -> usize {
        self.tris.iter().filter(|t| t.alive).count()
    }

    /// Invariants: counter-clockwise non-degenerate triangles (exact), symmetric neighbour
    /// links, subsegment bits equal on both sides, vertex-to-triangle links, and the
    /// constrained Delaunay property of every edge that is not a subsegment.
    pub fn check(&self) -> Result<(), String> {
        for (k, tri) in self.tris.iter().enumerate() {
            if !tri.alive {
                continue;
            }
            let t = k as u32;
            let q = tri.v.map(|v| self.p(v));
            if orient(q[0], q[1], q[2]) <= 0.0 {
                return Err(format!("triangle {} not counter-clockwise", k));
            }
            for i in 0..3 {
                let nb = tri.n[i];
                let (a, b) = (tri.v[NEXT[i]], tri.v[PREV[i]]);
                if nb == NONE {
                    if tri.seg & (1 << i) == 0 {
                        return Err(format!("boundary edge of triangle {} is not a subsegment", k));
                    }
                    continue;
                }
                let o = &self.tris[nb as usize];
                if !o.alive {
                    return Err(format!("triangle {} links a dead triangle", k));
                }
                let j = (0..3)
                    .find(|&j| o.v[NEXT[j]] == b && o.v[PREV[j]] == a)
                    .ok_or(format!("neighbour of {} does not share edge", k))?;
                if o.n[j] != t {
                    return Err(format!("asymmetric link at triangle {}", k));
                }
                if ((o.seg >> j) & 1) != ((tri.seg >> i) & 1) {
                    return Err(format!("subsegment bit differs across edge of {}", k));
                }
                if tri.seg & (1 << i) == 0 && incircle(q[0], q[1], q[2], self.p(o.v[j])) > 0.0 {
                    return Err(format!("edge {} of triangle {} is not locally Delaunay", i, k));
                }
            }
        }
        for (v, &t) in self.vtri.iter().enumerate() {
            if t != NONE && (!self.tris[t as usize].alive || !self.tris[t as usize].v.contains(&(v as u32))) {
                return Err(format!("vertex {} links a triangle without it", v));
            }
        }
        Ok(())
    }

    /// Subsegments (counted once per side) whose opposite apex lies on or inside their
    /// diametral circle.
    pub fn encroached_subsegments(&self) -> usize {
        let mut n = 0;
        for (k, tri) in self.tris.iter().enumerate() {
            if !tri.alive {
                continue;
            }
            for i in 0..3 {
                if tri.seg & (1 << i) != 0 {
                    let (a, b) = self.edge(k as u32, i);
                    if encroaches(self.p(a), self.p(b), self.p(tri.v[i])) {
                        n += 1;
                    }
                }
            }
        }
        n
    }
}
