"""Tests for DNG assembly from a remote capture's header and raw data."""
import struct
import time
from array import array
from pathlib import Path

import pytest

from pychdk.dng import DngError, assemble_dng, read_dng_header

HEADER_PATH = Path(__file__).parent / "data" / "a2500_dng_header.bin"

# The A2500's numbers, as its header states them. The code reads them
# from the header; the tests only use them to check what it read.
WIDTH, HEIGHT = 4704, 3504
ROW_BYTES = WIDTH * 12 // 8
RAW_BYTES = ROW_BYTES * HEIGHT          # 24,724,224
THUMB_OFFSET, THUMB_BYTES = 1344, 128 * 96 * 3
RAW_OFFSET = THUMB_OFFSET + THUMB_BYTES  # 38,208

# Levels for the synthetic picture, per CFA colour.
RED, GREEN, BLUE = 3000, 1000, 127


@pytest.fixture(scope="module")
def header():
    return HEADER_PATH.read_bytes()


def _pack_pair(first, second):
    """Two 12-bit samples, MSB first, in three bytes."""
    return bytes([
        first >> 4,
        ((first & 0x0F) << 4) | (second >> 8),
        second & 0xFF,
    ])


def _swap_pairs(data):
    swapped = array("H")
    swapped.frombytes(data)
    swapped.byteswap()
    return swapped.tobytes()


@pytest.fixture(scope="module")
def correct_raw():
    """The raw image as it belongs in the file: CFA [1,2,0,1], i.e.
    G B on even rows and R G on odd rows, built one row at a time."""
    even = _pack_pair(GREEN, BLUE) * (WIDTH // 2)
    odd = _pack_pair(RED, GREEN) * (WIDTH // 2)
    return (even + odd) * (HEIGHT // 2)


@pytest.fixture(scope="module")
def received_raw(correct_raw):
    """The same image as the camera sends it, byte pairs swapped."""
    return _swap_pairs(correct_raw)


def _entry_offset(data, ifd_offset, tag):
    count = struct.unpack_from("<H", data, ifd_offset)[0]
    for i in range(count):
        at = ifd_offset + 2 + 12 * i
        if struct.unpack_from("<H", data, at)[0] == tag:
            return at
    raise KeyError(tag)


def _patch(data, ifd_offset, tag, value, typ=None):
    """Return a copy of the header with one entry's value replaced."""
    patched = bytearray(data)
    at = _entry_offset(patched, ifd_offset, tag)
    entry_type = struct.unpack_from("<H", patched, at + 2)[0]
    if typ is not None:
        struct.pack_into("<H", patched, at + 2, typ)
        entry_type = typ
    if entry_type == 3:
        struct.pack_into("<HH", patched, at + 8, value, 0)
    else:
        struct.pack_into("<I", patched, at + 8, value)
    return bytes(patched)


IFD0 = 8
SUBIFD = 422


class TestTheHeaderIsRead:
    def test_the_fixture_is_the_header_the_camera_sent(self, header):
        assert len(header) == THUMB_OFFSET
        assert header[:4] == b"II*\x00"

    def test_thumbnail_and_raw_geometry_come_from_the_header(self, header):
        info = read_dng_header(header)
        assert (info.thumb_width, info.thumb_height) == (128, 96)
        assert info.thumb_offset == THUMB_OFFSET
        assert info.thumb_bytes == THUMB_BYTES
        assert (info.width, info.height) == (WIDTH, HEIGHT)
        assert info.bits == 12
        assert info.raw_offset == RAW_OFFSET
        assert info.raw_bytes == RAW_BYTES
        assert info.cfa_pattern == (1, 2, 0, 1)
        assert info.black_level == 127
        assert info.white_level == 4095
        assert info.active_area == (26, 16, 3486, 4640)


class TestAssembly:
    def test_the_file_is_header_then_thumbnail_then_raw(
        self, header, received_raw,
    ):
        dng = assemble_dng(header, received_raw)
        assert len(dng) == RAW_OFFSET + RAW_BYTES
        assert dng[:THUMB_OFFSET] == header

    def test_the_raw_data_has_its_byte_pairs_swapped(
        self, header, received_raw, correct_raw,
    ):
        dng = assemble_dng(header, received_raw)
        assert dng[RAW_OFFSET:RAW_OFFSET + 6] == correct_raw[:6]
        assert dng[RAW_OFFSET + 1] == received_raw[0]
        assert dng[RAW_OFFSET] == received_raw[1]
        assert dng[-ROW_BYTES:] == correct_raw[-ROW_BYTES:]

    def test_the_round_trip_restores_what_was_received(
        self, header, received_raw,
    ):
        dng = assemble_dng(header, received_raw)
        assert _swap_pairs(dng[RAW_OFFSET:]) == received_raw

    def test_the_thumbnail_fills_its_strip_and_is_not_black(
        self, header, received_raw,
    ):
        dng = assemble_dng(header, received_raw)
        thumb = dng[THUMB_OFFSET:RAW_OFFSET]
        assert len(thumb) == THUMB_BYTES
        assert any(thumb)

    def test_the_thumbnail_reads_the_colours_where_the_pattern_says(
        self, header, received_raw,
    ):
        dng = assemble_dng(header, received_raw)
        thumb = dng[THUMB_OFFSET:RAW_OFFSET]
        # Red sites are bright, green middling, blue at the black level.
        for i in range(0, len(thumb), 3):
            r, g, b = thumb[i], thumb[i + 1], thumb[i + 2]
            assert r > g > b == 0

    def test_a_black_picture_gives_a_black_thumbnail(self, header):
        black = _pack_pair(127, 127) * (RAW_BYTES // 3)
        dng = assemble_dng(header, _swap_pairs(black))
        assert not any(dng[THUMB_OFFSET:RAW_OFFSET])

    def test_assembly_takes_well_under_a_second(self, header, received_raw):
        start = time.perf_counter()
        assemble_dng(header, received_raw)
        assert time.perf_counter() - start < 1.0

    def test_bytearray_and_memoryview_are_accepted(
        self, header, received_raw,
    ):
        expected = assemble_dng(header, received_raw)
        assert assemble_dng(
            bytearray(header), memoryview(received_raw),
        ) == expected


class TestRefusals:
    def test_not_a_tiff(self, received_raw):
        with pytest.raises(DngError, match="not a TIFF"):
            assemble_dng(b"\xff\xd8\xff\xe0" + bytes(1340), received_raw)

    def test_an_empty_header(self, received_raw):
        with pytest.raises(DngError, match="not a TIFF"):
            assemble_dng(b"", received_raw)

    def test_a_truncated_header(self, header, received_raw):
        with pytest.raises(DngError):
            assemble_dng(header[:300], received_raw)

    def test_a_header_without_a_raw_image(self, header, received_raw):
        # PhotometricInterpretation 2 (RGB) instead of 32803 (CFA).
        no_cfa = _patch(header, SUBIFD, 0x0106, 2)
        with pytest.raises(DngError, match="CFA"):
            assemble_dng(no_cfa, received_raw)

    def test_raw_data_shorter_than_the_header_says(
        self, header, received_raw,
    ):
        with pytest.raises(DngError, match=r"24724222.*24724224"):
            assemble_dng(header, received_raw[:-2])

    def test_raw_data_longer_than_the_header_says(
        self, header, received_raw,
    ):
        with pytest.raises(DngError, match=r"24724226.*24724224"):
            assemble_dng(header, received_raw + b"\x00\x00")

    def test_a_thumbnail_that_overlaps_the_header(
        self, header, received_raw,
    ):
        bad = _patch(header, IFD0, 0x0111, 1000)
        with pytest.raises(DngError, match="offset"):
            assemble_dng(bad, received_raw)

    def test_raw_data_that_overlaps_the_thumbnail(
        self, header, received_raw,
    ):
        bad = _patch(header, SUBIFD, 0x0111, RAW_OFFSET - 2)
        with pytest.raises(DngError, match="offset"):
            assemble_dng(bad, received_raw)

    def test_raw_data_that_leaves_a_gap_after_the_thumbnail(
        self, header, received_raw,
    ):
        bad = _patch(header, SUBIFD, 0x0111, RAW_OFFSET + 2)
        with pytest.raises(DngError, match="offset"):
            assemble_dng(bad, received_raw)

    def test_a_thumbnail_strip_of_the_wrong_size(
        self, header, received_raw,
    ):
        bad = _patch(header, IFD0, 0x0117, THUMB_BYTES - 3)
        with pytest.raises(DngError, match="thumbnail"):
            assemble_dng(bad, received_raw)

    def test_a_raw_strip_that_does_not_match_the_dimensions(
        self, header, received_raw,
    ):
        bad = _patch(header, SUBIFD, 0x0117, RAW_BYTES - 2)
        with pytest.raises(DngError, match="dimensions"):
            assemble_dng(bad, received_raw[:-2])

    def test_compressed_raw_data(self, header, received_raw):
        bad = _patch(header, SUBIFD, 0x0103, 7)
        with pytest.raises(DngError, match="compress"):
            assemble_dng(bad, received_raw)

    def test_the_refusal_is_a_value_error(self, received_raw):
        with pytest.raises(ValueError):
            assemble_dng(b"nope", received_raw)
