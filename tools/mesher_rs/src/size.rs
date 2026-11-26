//! KiPI's graded size field, evaluated natively: h = clip(h_min + grading * max(|q - c_i| - r_i, 0),
//! h_min, h_max) with c_i the nearest hole centre (KiPI's `graded_size` queries a k-d tree of the
//! centres the same way). A static 2-d tree, nearest neighbour by exact squared distance; ties go
//! to the lower index.

use crate::geom::P;

pub struct Graded {
    pts: Vec<P>,
    r: Vec<f64>,
    tree: Vec<u32>, // indices in implicit tree order: node = median of its range
    pub h_min: f64,
    pub h_max: f64,
    pub grading: f64,
}

impl Graded {
    pub fn new(centres: &[P], radii: &[f64], h_min: f64, h_max: f64, grading: f64) -> Graded {
        assert_eq!(centres.len(), radii.len());
        let mut tree: Vec<u32> = (0..centres.len() as u32).collect();
        Self::build(centres, &mut tree, 0);
        Graded { pts: centres.to_vec(), r: radii.to_vec(), tree, h_min, h_max, grading }
    }

    fn build(p: &[P], idx: &mut [u32], depth: usize) {
        if idx.len() <= 1 {
            return;
        }
        let ax = depth % 2;
        idx.sort_by(|&a, &b| p[a as usize][ax].total_cmp(&p[b as usize][ax]).then(a.cmp(&b)));
        let mid = idx.len() / 2;
        let (lo, hi) = idx.split_at_mut(mid);
        Self::build(p, lo, depth + 1);
        Self::build(p, &mut hi[1..], depth + 1);
    }

    fn nearest_in(&self, q: P, lo: usize, hi: usize, depth: usize, best: &mut (f64, u32)) {
        if lo >= hi {
            return;
        }
        let mid = lo + (hi - lo) / 2;
        let i = self.tree[mid];
        let c = self.pts[i as usize];
        let (dx, dy) = (q[0] - c[0], q[1] - c[1]);
        let d2 = dx * dx + dy * dy;
        if d2 < best.0 || (d2 == best.0 && i < best.1) {
            *best = (d2, i);
        }
        let ax = depth % 2;
        let diff = q[ax] - c[ax];
        let (first, second) = if diff < 0.0 { ((lo, mid), (mid + 1, hi)) } else { ((mid + 1, hi), (lo, mid)) };
        self.nearest_in(q, first.0, first.1, depth + 1, best);
        if diff * diff <= best.0 {
            self.nearest_in(q, second.0, second.1, depth + 1, best);
        }
    }

    /// h at q; +inf without centres.
    pub fn eval(&self, q: P) -> f64 {
        if self.pts.is_empty() {
            return f64::INFINITY;
        }
        let mut best = (f64::INFINITY, u32::MAX);
        self.nearest_in(q, 0, self.tree.len(), 0, &mut best);
        let d = (best.0.sqrt() - self.r[best.1 as usize]).max(0.0);
        (self.h_min + self.grading * d).max(self.h_min).min(self.h_max)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn nearest_centre_matches_a_brute_force_scan() {
        let mut c = vec![];
        let mut r = vec![];
        let mut s: u64 = 12345;
        let mut rnd = || {
            s = s.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
            ((s >> 11) as f64) / ((1u64 << 53) as f64)
        };
        for _ in 0..300 {
            c.push([120.0 + 30.0 * rnd(), 80.0 + 20.0 * rnd()]);
            r.push(0.1 + 0.2 * rnd());
        }
        let g = Graded::new(&c, &r, 0.02, 0.5, 0.2);
        for _ in 0..2000 {
            let q = [118.0 + 34.0 * rnd(), 78.0 + 24.0 * rnd()];
            let mut best = (f64::INFINITY, 0usize);
            for (k, p) in c.iter().enumerate() {
                let (dx, dy) = (q[0] - p[0], q[1] - p[1]);
                let d2 = dx * dx + dy * dy;
                if d2 < best.0 {
                    best = (d2, k);
                }
            }
            let want = (0.02 + 0.2 * (best.0.sqrt() - r[best.1]).max(0.0)).max(0.02).min(0.5);
            assert_eq!(g.eval(q), want);
        }
    }
}
