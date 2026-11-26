//! coppermesh: constrained Delaunay triangulation and Delaunay refinement, an experimental
//! drop-in for KiPI's `mesh_polygon` in Coppernicus. See DESIGN.md for the algorithms and
//! their sources.

pub mod geom;
pub mod mesh;
pub mod refine;
pub mod size;
#[cfg(feature = "python")]
mod python;

pub use geom::P;
pub use mesh::{Mesh, Stats};
pub use refine::{RefineParams, Rule, Status};
pub use size::Graded;
