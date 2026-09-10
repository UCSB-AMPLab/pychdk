"""The package version and pyproject.toml must not drift apart."""
import tomllib
from pathlib import Path

import pychdk


def _pyproject_version():
    """Read project.version from the pyproject.toml beside this checkout."""
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    with pyproject.open("rb") as f:
        return tomllib.load(f)["project"]["version"]


class TestVersion:
    def test_package_version_matches_pyproject(self):
        assert pychdk.__version__ == _pyproject_version()
