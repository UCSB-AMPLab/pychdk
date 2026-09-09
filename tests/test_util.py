"""Tests for exposure conversion utilities."""
import pytest
from pychdk.util import (
    shutter_to_tv96,
    iso_to_sv96,
    aperture_to_av96,
    to_camerapath,
    parse_own_txt,
    format_own_txt,
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


class TestParseOwnTxt:
    def test_parity_only(self):
        assert parse_own_txt(b"EVEN\n") == ("EVEN", None)

    def test_parity_and_id(self):
        assert parse_own_txt(b"EVEN\nid=3f9a1c2b7d4e\n") == (
            "EVEN", "3f9a1c2b7d4e",
        )

    def test_accepts_str(self):
        assert parse_own_txt("ODD\nid=abc123\n") == ("ODD", "abc123")

    def test_crlf(self):
        assert parse_own_txt(b"ODD\r\nid=abc123\r\n") == ("ODD", "abc123")

    def test_byte_order_mark(self):
        assert parse_own_txt(b"\xef\xbb\xbfEVEN\nid=abc123\n") == (
            "EVEN", "abc123",
        )

    def test_trailing_whitespace(self):
        assert parse_own_txt(b"  EVEN  \n  id=abc123  \n") == (
            "EVEN", "abc123",
        )

    def test_blank_lines(self):
        assert parse_own_txt(b"\n\nODD\n\n\nid=abc123\n\n") == (
            "ODD", "abc123",
        )

    def test_lowercase(self):
        assert parse_own_txt(b"odd\nId=ABC123\n") == ("ODD", "ABC123")

    def test_missing_id_line(self):
        assert parse_own_txt(b"ODD") == ("ODD", None)

    def test_unknown_lines_are_ignored(self):
        data = b"# written by flash_chdk\nEVEN\nnotes=whatever\nid=abc123\n"
        assert parse_own_txt(data) == ("EVEN", "abc123")

    def test_rubbish_gives_nothing(self):
        assert parse_own_txt(b"\xff\xfe not a parity file at all") == (
            None, None,
        )

    def test_empty(self):
        assert parse_own_txt(b"") == (None, None)

    def test_does_not_raise_on_a_wrong_type(self):
        assert parse_own_txt(None) == (None, None)


class TestFormatOwnTxt:
    def test_parity_and_id(self):
        assert format_own_txt("EVEN", "3f9a1c2b7d4e") == (
            "EVEN\nid=3f9a1c2b7d4e\n"
        )

    def test_id_line_omitted_without_an_id(self):
        assert format_own_txt("ODD") == "ODD\n"

    def test_parity_is_uppercased(self):
        assert format_own_txt("even", "abc123") == "EVEN\nid=abc123\n"

    def test_round_trip(self):
        text = format_own_txt("odd", "3f9a1c2b7d4e")
        assert parse_own_txt(text) == ("ODD", "3f9a1c2b7d4e")
