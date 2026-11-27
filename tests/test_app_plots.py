"""The app's plots follow its theme on screen; the files written to disk (the CLI's --out, the
app's Export CSV and PNG) stay light: the theme is applied inside theme.plotting() only, and
matplotlib's own settings are untouched outside it."""

import pytest

pytest.importorskip("PySide6")

from test_app_window import run_in_window  # noqa: E402

from coppernicus.app import theme  # noqa: E402


@pytest.mark.parametrize("name", theme.THEMES)
def test_the_plot_settings_are_the_themes_and_the_series_read_on_its_panel(name):
    from test_app_theme import contrast
    rc = theme.mpl_rc(name)
    t = theme.tokens(name)
    assert rc["axes.facecolor"] == t["panel"] and rc["text.color"] == t["fg"]
    series = rc["axes.prop_cycle"].by_key()["color"]
    assert series == [t[f"series-{i}"] for i in range(1, 5)]
    # Series 1 (the current result, the first curve) reads at 3:1 (WCAG's graphics minimum);
    # the others at 2:1 at least: in light, orange and aqua sit below 3:1, as the palette's
    # validation found, so the plots label those curves directly.
    assert contrast(series[0], t["panel"]) >= 3.0, series[0]
    assert all(contrast(c, t["panel"]) >= 2.0 for c in series), series


def test_plotting_leaves_matplotlibs_own_settings_as_they_were():
    import matplotlib
    before = dict(matplotlib.rcParams)
    with theme.plotting():
        assert matplotlib.rcParams["axes.facecolor"] == theme.color("panel")
    assert dict(matplotlib.rcParams) == before


def test_the_loss_chart_and_the_empty_plots_follow_a_switch():
    run_in_window('''
        from matplotlib.colors import to_hex
        from coppernicus.app import theme
        figs = [window.losses.figure, window.ac_panel.figure]
        figs += [f for f, _c in window.noise_panel.canvases.values()]
        for fig in figs:
            assert to_hex(fig.get_facecolor()) == theme.color("panel"), fig
        window.toggle_theme()
        for fig in figs:
            assert to_hex(fig.get_facecolor()) == theme.color("panel"), fig
        text = window.ac_panel.figure.axes[0].texts[0]
        assert to_hex(text.get_color()) == theme.color("muted")
    ''')
