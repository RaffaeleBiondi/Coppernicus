"""Set-up shared by every test."""

import pytest

from coppernicus.triangle_standin import ensure_triangle_importable

# As the engine package does, before any test module imports KiPI (whose mesher imports triangle):
# without Triangle, the stand-in takes its place.
ensure_triangle_importable()


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


@pytest.fixture(scope="session", autouse=True)
def app_settings_kept_apart(tmp_path_factory):
    """The app's settings (theme, hover label, solve times) go into a folder of the run, never
    the user's: COPPERNICUS_SETTINGS_DIR for this process and every child it starts (the window
    tests run in children, with this environment), and QSettings' own default for anything that
    asks Qt directly."""
    import os

    from coppernicus.app import SETTINGS_DIR_ENV
    folder = tmp_path_factory.mktemp("app-settings")
    before = os.environ.get(SETTINGS_DIR_ENV)
    os.environ[SETTINGS_DIR_ENV] = str(folder)
    try:
        from PySide6.QtCore import QSettings
        QSettings.setDefaultFormat(QSettings.IniFormat)
        QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(folder))
    except ImportError:                     # no Qt: the core tests still run
        pass
    yield folder
    if before is None:
        os.environ.pop(SETTINGS_DIR_ENV, None)
    else:
        os.environ[SETTINGS_DIR_ENV] = before
