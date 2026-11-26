//! The Python binding (PyO3): one persistent mesh object per polygon.
//!
//!     m = coppermesh.Mesher(verts (N,2) float64, segs (S,2) int64, holes (H,2) float64)
//!     ok = m.refine(min_angle, max_area, cap, rule="area")      # False when the cap is hit
//!     pts, tris = m.export()                                    # (N,2) float64, (M,3) int64
//!     ok = m.refine_sized(h, min_angle, max_area, cap, rule)    # h: one per exported node
//!     m.stats() -> dict; m.check() raises ValueError if an invariant is broken

use crate::{Graded, Mesh, RefineParams, Rule, Status};
use numpy::{PyArray1, PyArray2, PyArrayMethods, PyReadonlyArray1, PyReadonlyArray2};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyDict;

fn rule_of(s: &str) -> PyResult<Rule> {
    match s {
        "area" => Ok(Rule::Area),
        "edge" => Ok(Rule::Edge),
        _ => Err(PyValueError::new_err("rule must be 'area' or 'edge'")),
    }
}

fn points(a: &PyReadonlyArray2<'_, f64>, what: &str) -> PyResult<Vec<[f64; 2]>> {
    let v = a.as_array();
    if v.ncols() != 2 && v.nrows() > 0 {
        return Err(PyValueError::new_err(format!("{} must be (N, 2)", what)));
    }
    Ok(v.rows().into_iter().map(|r| [r[0], r[1]]).collect())
}

#[pyclass(unsendable, module = "coppermesh")]
struct Mesher {
    m: Mesh,
}

#[pymethods]
impl Mesher {
    #[new]
    #[pyo3(signature = (verts, segs, holes=None))]
    fn new(
        py: Python<'_>,
        verts: PyReadonlyArray2<'_, f64>,
        segs: PyReadonlyArray2<'_, i64>,
        holes: Option<PyReadonlyArray2<'_, f64>>,
    ) -> PyResult<Self> {
        let pts = points(&verts, "verts")?;
        let s = segs.as_array();
        if s.ncols() != 2 && s.nrows() > 0 {
            return Err(PyValueError::new_err("segs must be (S, 2)"));
        }
        let mut sg = Vec::with_capacity(s.nrows());
        for r in s.rows() {
            if r[0] < 0 || r[1] < 0 || r[0] as usize >= pts.len() || r[1] as usize >= pts.len() {
                return Err(PyValueError::new_err("segment index out of range"));
            }
            sg.push([r[0] as u32, r[1] as u32]);
        }
        let hl = match &holes {
            Some(h) => points(h, "holes")?,
            None => vec![],
        };
        let m = py.detach(|| Mesh::build(&pts, &sg, &hl)).map_err(PyValueError::new_err)?;
        Ok(Mesher { m })
    }

    /// Refine to min_angle (degrees) and max_area; False when `cap` points were added.
    #[pyo3(signature = (min_angle, max_area, cap, rule="area", min_edge=0.0, tiny_angle=0.0))]
    fn refine(
        &mut self,
        py: Python<'_>,
        min_angle: f64,
        max_area: f64,
        cap: usize,
        rule: &str,
        min_edge: f64,
        tiny_angle: f64,
    ) -> PyResult<bool> {
        let mut prm = RefineParams::new(min_angle, max_area, cap, rule_of(rule)?);
        prm.min_edge = min_edge;
        prm.tiny_angle_deg = tiny_angle;
        let m = &mut self.m;
        Ok(py.detach(|| m.refine(&prm)) == Status::Done)
    }

    /// Refine further to a size field h given at the nodes of the last export.
    #[pyo3(signature = (h, min_angle, max_area, cap, rule="area", min_edge=0.0, tiny_angle=0.0))]
    #[allow(clippy::too_many_arguments)]
    fn refine_sized(
        &mut self,
        py: Python<'_>,
        h: PyReadonlyArray1<'_, f64>,
        min_angle: f64,
        max_area: f64,
        cap: usize,
        rule: &str,
        min_edge: f64,
        tiny_angle: f64,
    ) -> PyResult<bool> {
        let hv: Vec<f64> = h.as_array().iter().copied().collect();
        if hv.len() != self.m.n_exported() {
            return Err(PyValueError::new_err("h needs one value per node of the last export"));
        }
        let mut prm = RefineParams::new(min_angle, max_area, cap, rule_of(rule)?);
        prm.min_edge = min_edge;
        prm.tiny_angle_deg = tiny_angle;
        let m = &mut self.m;
        Ok(py.detach(|| m.refine_sized(&hv, &prm)) == Status::Done)
    }

    /// Refine further to KiPI's graded size field (hole centres (K,2), radii (K,), h_min, h_max,
    /// grading), evaluated exactly at each triangle's centroid.
    #[pyo3(signature = (centres, radii, h_min, h_max, grading, min_angle, max_area, cap, rule="area", min_edge=0.0, tiny_angle=0.0))]
    #[allow(clippy::too_many_arguments)]
    fn refine_graded(
        &mut self,
        py: Python<'_>,
        centres: PyReadonlyArray2<'_, f64>,
        radii: PyReadonlyArray1<'_, f64>,
        h_min: f64,
        h_max: f64,
        grading: f64,
        min_angle: f64,
        max_area: f64,
        cap: usize,
        rule: &str,
        min_edge: f64,
        tiny_angle: f64,
    ) -> PyResult<bool> {
        let field = graded_field(&centres, &radii, h_min, h_max, grading)?;
        let mut prm = RefineParams::new(min_angle, max_area, cap, rule_of(rule)?);
        prm.min_edge = min_edge;
        prm.tiny_angle_deg = tiny_angle;
        let m = &mut self.m;
        Ok(py.detach(|| m.refine_graded(field, &prm)) == Status::Done)
    }

    /// Keep only the triangles of the last export flagged in `keep` (carving by centroid).
    fn retain(&mut self, keep: PyReadonlyArray1<'_, bool>) -> PyResult<()> {
        let k: Vec<bool> = keep.as_array().iter().copied().collect();
        if k.len() != self.m.n_exported_triangles() {
            return Err(PyValueError::new_err("keep needs one flag per triangle of the last export"));
        }
        self.m.retain(&k);
        Ok(())
    }

    /// (pts (N,2) float64, tris (M,3) int64): the domain's triangles, counter-clockwise, and
    /// the nodes they use (input vertices first, bit-exact).
    fn export<'py>(&mut self, py: Python<'py>) -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray2<i64>>)> {
        let (p, t) = self.m.export();
        let flat: Vec<f64> = p.iter().flat_map(|q| [q[0], q[1]]).collect();
        let pa = PyArray1::from_vec(py, flat).reshape([p.len(), 2])?;
        let ti: Vec<i64> = t.iter().flat_map(|x| x.map(|v| v as i64)).collect();
        let ta = PyArray1::from_vec(py, ti).reshape([t.len(), 3])?;
        Ok((pa, ta))
    }

    /// The subsegments as (S, 2) int64 node indices of the last export.
    fn subsegments<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyArray2<i64>>> {
        let s = self.m.subsegments();
        let flat: Vec<i64> = s.iter().flat_map(|e| [e[0] as i64, e[1] as i64]).collect();
        PyArray1::from_vec(py, flat).reshape([s.len(), 2])
    }

    fn stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let s = &self.m.stats;
        let d = PyDict::new(py);
        d.set_item("input_vertices", s.input_vertices)?;
        d.set_item("duplicates", s.duplicates)?;
        d.set_item("segment_splits", s.segment_splits)?;
        d.set_item("circumcenters", s.circumcenters)?;
        d.set_item("rejected", s.rejected)?;
        d.set_item("declined", s.declined)?;
        d.set_item("dropped", s.dropped)?;
        d.set_item("floored", s.floored)?;
        d.set_item("tiny", s.tiny)?;
        d.set_item("bg_scans", s.bg_scans)?;
        d.set_item("triangles", self.m.n_triangles())?;
        Ok(d)
    }

    /// Area of the domain (after carving), mm².
    fn area(&self) -> f64 {
        self.m.area()
    }

    fn check(&self) -> PyResult<()> {
        self.m.check().map_err(PyValueError::new_err)
    }
}

fn graded_field(
    centres: &PyReadonlyArray2<'_, f64>,
    radii: &PyReadonlyArray1<'_, f64>,
    h_min: f64,
    h_max: f64,
    grading: f64,
) -> PyResult<Graded> {
    let c = points(centres, "centres")?;
    let r: Vec<f64> = radii.as_array().iter().copied().collect();
    if c.len() != r.len() {
        return Err(PyValueError::new_err("one radius per centre"));
    }
    Ok(Graded::new(&c, &r, h_min, h_max, grading))
}

/// KiPI's graded size field evaluated natively at xy (N,2): the h the core refines to.
#[pyfunction]
fn graded_size<'py>(
    py: Python<'py>,
    centres: PyReadonlyArray2<'_, f64>,
    radii: PyReadonlyArray1<'_, f64>,
    h_min: f64,
    h_max: f64,
    grading: f64,
    xy: PyReadonlyArray2<'_, f64>,
) -> PyResult<Bound<'py, PyArray1<f64>>> {
    let g = graded_field(&centres, &radii, h_min, h_max, grading)?;
    let q = points(&xy, "xy")?;
    Ok(PyArray1::from_vec(py, q.iter().map(|&p| g.eval(p)).collect()))
}

#[pymodule]
fn coppermesh(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Mesher>()?;
    m.add_function(wrap_pyfunction!(graded_size, m)?)?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
