"""What importing the package is allowed to drag in with it."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

import pychdk

SRC = Path(__file__).resolve().parent.parent / "src"


def _run_import(code):
    """Run a snippet against this checkout in a fresh interpreter."""
    env = dict(os.environ, PYTHONPATH=str(SRC))
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env,
    )


class TestFlasherStaysStdlibOnly:
    """tools/flash_chdk.py must run on a fresh macOS, pyusb or not."""

    def test_importing_pychdk_util_does_not_pull_in_pyusb(self):
        result = _run_import(
            "import pychdk.util, sys; print('usb' in sys.modules)"
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "False"


class TestExports:
    def test_every_name_in_all_is_reachable(self):
        for name in pychdk.__all__:
            assert getattr(pychdk, name) is not None

    def test_util_is_the_submodule(self):
        import pychdk.util
        assert pychdk.util is sys.modules["pychdk.util"]

    def test_unknown_name_raises_attribute_error(self):
        with pytest.raises(AttributeError, match="has no attribute 'nope'"):
            pychdk.nope
