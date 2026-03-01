"""Tests for exposure conversion utilities."""
import pytest
from pychdk.util import (
    shutter_to_tv96,
    iso_to_sv96,
    aperture_to_av96,
    to_camerapath,
)


class TestShutterToTV96:
    def test_one_second(self):
        assert shutter_to_tv96(1.0) == 0

    def test_half_second(self):
        assert shutter_to_tv96(0.5) == 96

    def test_quarter_second(self):
        assert shutter_to_tv96(0.25) == 192

    def test_1_over_100(self):
        result = shutter_to_tv96(1 / 100)
        assert 630 <= result <= 650


class TestISOToSV96:
    def test_iso_100(self):
        result = iso_to_sv96(100)
        assert 480 <= result <= 500

    def test_iso_200(self):
        result = iso_to_sv96(200)
        assert 570 <= result <= 600


class TestApertureToAV96:
    def test_f2_8(self):
        result = aperture_to_av96(2.8)
        assert 280 <= result <= 300


class TestToCamerapath:
    def test_adds_prefix(self):
        assert to_camerapath("DCIM") == "A/DCIM"

    def test_already_prefixed(self):
        assert to_camerapath("A/DCIM") == "A/DCIM"

    def test_own_txt(self):
        assert to_camerapath("OWN.TXT") == "A/OWN.TXT"
