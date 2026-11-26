"""Set-up shared by every test."""

import pytest


@pytest.fixture(scope="session")
def kipi_factorizations():
    """KiPI's own SuperLU functions (``kipi.core.solver.direct._superlu`` and
    ``_superlu_complex``), whatever :mod:`coppernicus.engine.superlu_order` has installed. The
    suite runs with the engine's default in place (thermal and dc in SuperLU's symmetric order,
    unless ``COPPERNICUS_SUPERLU_ORDER`` says otherwise)."""
    import coppernicus.engine.superlu_order as so
    return {name: (so._kipi or {}).get(name, getattr(so.direct, name)) for name in so._SEAMS}


@pytest.fixture
def kipis_method():
    """KiPI's own factorizations for one test (every superlu_order part out), for a comparison
    with KiPI to the last bit; the parts that were on, and the DC direct ceiling, are put back
    after it."""
    import coppernicus.engine.superlu_order as so
    before, ceiling = so.enabled(), so.dc_direct_max()
    so.uninstall()
    yield
    so.uninstall()
    if before:
        so.enable(*sorted(before))
    if ceiling is not None:
        so.set_dc_direct_max(ceiling)
