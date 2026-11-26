"""Set-up shared by every test."""

import pytest


@pytest.fixture(scope="session", autouse=True)
def kipi_factorizations():
    """KiPI's own SuperLU functions (``kipi.core.solver.direct._superlu`` and
    ``_superlu_complex``), recorded before any test runs, with Coppernicus's opt-in symmetric
    mode (``COPPERNICUS_SUPERLU_ORDER``, :mod:`coppernicus.engine.superlu_order`) taken out for
    the session: every test runs KiPI's path whatever the environment says, and the
    superlu_order tests turn parts on themselves."""
    import coppernicus.engine.superlu_order as so
    originals = {name: (so._kipi or {}).get(name, getattr(so.direct, name)) for name in so._SEAMS}
    so.uninstall()
    yield originals
    so.uninstall()
