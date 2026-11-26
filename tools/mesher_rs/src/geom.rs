//! Geometric kernel: exact predicates (Shewchuk's, through the `robust` crate), circumcentres,
//! and the few scalar helpers the refiner needs, all without libm so that results are
//! bit-identical on every IEEE-754 platform.

use robust::Coord;

pub type P = [f64; 2];

#[inline]
fn c(p: P) -> Coord<f64> {
    Coord { x: p[0], y: p[1] }
}

/// Positive if a, b, c are counter-clockwise, negative if clockwise, zero if collinear (exact
/// sign; Shewchuk's adaptive orient2d).
#[inline]
pub fn orient(a: P, b: P, cc: P) -> f64 {
    robust::orient2d(c(a), c(b), c(cc))
}

/// Positive if d lies strictly inside the circle through a, b, c (counter-clockwise), negative
/// outside, zero on it (exact sign; Shewchuk's adaptive incircle).
#[inline]
pub fn incircle(a: P, b: P, cc: P, d: P) -> f64 {
    robust::incircle(c(a), c(b), c(cc), c(d))
}

#[inline]
pub fn sub(a: P, b: P) -> P {
    [a[0] - b[0], a[1] - b[1]]
}

#[inline]
pub fn dot(a: P, b: P) -> f64 {
    a[0] * b[0] + a[1] * b[1]
}

#[inline]
pub fn dist2(a: P, b: P) -> f64 {
    let d = sub(a, b);
    dot(d, d)
}

/// Whether w lies on or inside the diametral circle of segment xy (Shewchuk 2002, App. A:
/// the angle at w is at least 90 degrees iff (x - w) . (y - w) <= 0).
#[inline]
pub fn encroaches(x: P, y: P, w: P) -> bool {
    dot(sub(x, w), sub(y, w)) <= 0.0
}

/// Circumcentre of the counter-clockwise triangle abc, relative to a; the denominator's sign
/// comes from the exact orientation.
pub fn circumcenter(a: P, b: P, cc: P) -> P {
    let (bx, by) = (b[0] - a[0], b[1] - a[1]);
    let (cx, cy) = (cc[0] - a[0], cc[1] - a[1]);
    let d = 2.0 * orient(a, b, cc);
    let b2 = bx * bx + by * by;
    let c2 = cx * cx + cy * cy;
    [a[0] + (cy * b2 - by * c2) / d, a[1] + (bx * c2 - cx * b2) / d]
}

/// cos(x) for |x| <= pi/2 by a fixed Taylor series (no libm, so the same bits everywhere).
pub fn cos_series(x: f64) -> f64 {
    let x2 = x * x;
    let mut term = 1.0;
    let mut sum = 1.0;
    for k in 1..=24u32 {
        term *= -x2 / (((2 * k - 1) * (2 * k)) as f64);
        sum += term;
    }
    sum
}

/// sin(x) for |x| <= pi/2 by a fixed Taylor series (no libm).
pub fn sin_series(x: f64) -> f64 {
    let x2 = x * x;
    let mut term = x;
    let mut sum = x;
    for k in 1..=24u32 {
        term *= -x2 / (((2 * k) * (2 * k + 1)) as f64);
        sum += term;
    }
    sum
}

/// sin^2 of an angle given in degrees (0 <= deg <= 90).
pub fn sin2_deg(deg: f64) -> f64 {
    let sv = sin_series(deg * (std::f64::consts::PI / 180.0));
    sv * sv
}

/// cos^2 of an angle given in degrees (0 < deg <= 90).
pub fn cos2_deg(deg: f64) -> f64 {
    let x = deg * (std::f64::consts::PI / 180.0);
    let cv = cos_series(x);
    cv * cv
}

/// 2^floor(log2 x) for a positive normal x, by clearing the mantissa bits.
#[inline]
pub fn pow2_floor(x: f64) -> f64 {
    f64::from_bits(x.to_bits() & 0xFFF0_0000_0000_0000)
}

/// The power of two closest to len / 2 (Ruppert's concentric shells, Shewchuk 2002 §6.2):
/// it always lies in [len/3, 2 len/3]. Ties go to the smaller one.
pub fn shell_distance(len: f64) -> f64 {
    let half = len / 2.0;
    let lo = pow2_floor(half);
    let hi = 2.0 * lo;
    if (half - lo) <= (hi - half) {
        lo
    } else {
        hi
    }
}

/// Whether len is a power of two within a relative tolerance.
pub fn is_pow2(len: f64, rel_tol: f64) -> bool {
    let p = pow2_floor(len);
    let r = len / p; // in [1, 2)
    r <= 1.0 + rel_tol || r >= 2.0 * (1.0 - rel_tol)
}

/// Barycentric interpolation of (ha, hb, hc) at q in triangle abc, with the coordinates
/// clamped to [0, 1] (q may lie a rounding error outside).
pub fn interpolate(a: P, b: P, cc: P, ha: f64, hb: f64, hc: f64, q: P) -> f64 {
    let area = (b[0] - a[0]) * (cc[1] - a[1]) - (cc[0] - a[0]) * (b[1] - a[1]);
    if area == 0.0 {
        return ha.min(hb).min(hc);
    }
    let la = ((b[0] - q[0]) * (cc[1] - q[1]) - (cc[0] - q[0]) * (b[1] - q[1])) / area;
    let lb = ((cc[0] - q[0]) * (a[1] - q[1]) - (a[0] - q[0]) * (cc[1] - q[1])) / area;
    let lc = 1.0 - la - lb;
    let (la, lb, lc) = (la.clamp(0.0, 1.0), lb.clamp(0.0, 1.0), lc.clamp(0.0, 1.0));
    let s = la + lb + lc;
    (la * ha + lb * hb + lc * hc) / s
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sin_series_matches_libm_to_rounding() {
        for d in [0.001, 0.5, 1.0, 5.0, 30.0, 60.0, 89.0] {
            let x: f64 = (d as f64).to_radians();
            assert!((sin_series(x) - x.sin()).abs() <= 2e-16 * x.sin().max(1e-300).max(1.0), "{}", d);
        }
    }

    #[test]
    fn cos_series_matches_libm_to_rounding() {
        for d in [1.0, 20.0, 28.0, 30.0, 33.0, 45.0, 60.0, 89.0] {
            let x: f64 = (d as f64).to_radians();
            assert!((cos_series(x) - x.cos()).abs() < 4e-16, "{}", d);
        }
    }

    #[test]
    fn shells_split_in_the_middle_third_at_a_power_of_two() {
        for len in [0.01, 0.0137, 0.5, 0.75, 1.0, 1.4999, 3.0, 7.9, 100.0] {
            let d = shell_distance(len);
            assert!(d >= len / 3.0 - 1e-15 && d <= 2.0 * len / 3.0 + 1e-15, "{} -> {}", len, d);
            assert!(is_pow2(d, 0.0));
        }
        assert_eq!(shell_distance(1.0), 0.5);
        assert!(is_pow2(0.25, 1e-9) && is_pow2(0.25 * (1.0 + 1e-12), 1e-9) && !is_pow2(0.3, 1e-9));
        assert!(is_pow2(0.5 * (1.0 - 1e-12), 1e-9));
    }

    #[test]
    fn circumcenter_is_equidistant() {
        let (a, b, cc) = ([120.0, 80.0], [121.3, 80.2], [120.4, 81.7]);
        let o = circumcenter(a, b, cc);
        let (ra, rb, rc) = (dist2(o, a), dist2(o, b), dist2(o, cc));
        assert!((ra - rb).abs() < 1e-12 && (ra - rc).abs() < 1e-12);
    }

    #[test]
    fn predicates_signs() {
        let (a, b, cc) = ([0.0, 0.0], [1.0, 0.0], [0.0, 1.0]);
        assert!(orient(a, b, cc) > 0.0);
        assert!(incircle(a, b, cc, [0.5, 0.5]) > 0.0);
        assert!(incircle(a, b, cc, [1.0, 1.0]) == 0.0);
        assert!(encroaches(a, b, [0.5, 0.5]) && encroaches(a, b, [0.5, -0.5]) && !encroaches(a, b, [0.5, 0.6]));
    }
}
