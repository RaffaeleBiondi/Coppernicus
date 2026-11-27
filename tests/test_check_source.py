"""tools/mesher_rs/check_source.py tells the source build of coppermesh from PyPI's wheel by the
install record (direct_url.json), the only thing that differs: both are coppermesh 0.1.0."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CRATE = ROOT / "tools" / "mesher_rs"
_spec = importlib.util.spec_from_file_location("check_source", CRATE / "check_source.py")
check_source = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_source)


class Dist:
    """A distribution whose direct_url.json is ``record`` (None: an install from an index)."""

    def __init__(self, record=None):
        self.text = None if record is None else json.dumps(record)

    def read_text(self, name):
        return self.text if name == "direct_url.json" else None


def test_the_source_build_passes_and_the_wheel_does_not(tmp_path):
    built = Dist({"url": CRATE.as_uri(), "dir_info": {}})
    wheel = Dist()
    assert check_source.origin(built) == CRATE and check_source.origin(wheel) is None
    assert check_source.problem(built) is None
    assert "PyPI's wheel" in check_source.problem(wheel) and "uv run --no-sync" in check_source.problem(wheel)
    other = Dist({"url": tmp_path.as_uri(), "dir_info": {}})                  # another checkout
    assert "not from" in check_source.problem(other) and check_source.problem(other, crate=tmp_path) is None


def test_pypi_expects_an_index_install_and_nothing_else():
    built = Dist({"url": CRATE.as_uri(), "dir_info": {}})
    assert check_source.problem(Dist(), pypi=True) is None
    assert "not PyPI's wheel" in check_source.problem(built, pypi=True)
    git = Dist({"url": "https://github.com/RaffaeleBiondi/Coppernicus", "subdirectory": "tools/mesher_rs",
                "vcs_info": {"vcs": "git", "commit_id": "0" * 40}})
    for pypi in (False, True):
        assert "not from a local folder" in check_source.problem(git, pypi=pypi)


def test_main_reads_the_installed_distribution(monkeypatch, capsys):
    from importlib import metadata

    class Installed(Dist):
        version = "0.1.0"

    monkeypatch.setattr(metadata, "distribution", lambda name: Installed())
    assert check_source.main(["--pypi"]) == 0 and "PyPI's wheel" in capsys.readouterr().out
    assert check_source.main([]) == 1 and "uv pip install ./tools/mesher_rs" in capsys.readouterr().err

    def missing(name):
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(metadata, "distribution", missing)
    assert check_source.main([]) == 1 and "not installed" in capsys.readouterr().err
