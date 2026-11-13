"""Assemble a DNG file from the parts CHDK's remote capture sends.

With init_usb_capture asking for the DNG header (0x04) and the raw data
(0x02), CHDK sends two separate transfers and no thumbnail. The header
is a complete TIFF directory structure whose strip offsets point past
its own end: IFD0 describes an RGB thumbnail stored straight after the
header, and a SubIFD describes the CFA raw image stored after the
thumbnail. A file is therefore header + thumbnail + raw data, and the
thumbnail has to be made on this side.

On a Canon PowerShot A2500 running CHDK 1.5.1 the raw data arrives with
the two bytes of every 16-bit pair swapped; once swapped back, the
12-bit samples are packed most significant bit first, two pixels in
three bytes, which is what the header's uncompressed CFA strip
describes. assemble_dng swaps every pair unconditionally. That has been
measured on the A2500 only.

Everything here is pure: bytes in, bytes out, standard library only.
"""
import struct
from array import array
from collections import namedtuple


class DngError(ValueError):
    """The header and raw data cannot be assembled into a DNG."""


DngHeader = namedtuple("DngHeader", [
    "thumb_width", "thumb_height", "thumb_offset", "thumb_bytes",
    "width", "height", "bits", "raw_offset", "raw_bytes",
    "cfa_pattern", "black_level", "white_level", "active_area",
    "as_shot_neutral",
])
DngHeader.__doc__ = """What assemble_dng reads from a CHDK DNG header.

active_area is (top, left, bottom, right), as the DNG ActiveArea tag
orders it. cfa_pattern is the 2x2 CFAPattern, row by row, with 0 for
red, 1 for green and 2 for blue. as_shot_neutral is None when the
header does not carry the tag.
"""

# TIFF field types: byte size and struct code of one value. Rationals
# are read as two integers and divided.
_TYPES = {
    1: (1, "B"), 2: (1, "B"), 3: (2, "H"), 4: (4, "I"),
    5: (8, "II"), 6: (1, "b"), 7: (1, "B"), 8: (2, "h"),
    9: (4, "i"), 10: (8, "ii"), 11: (4, "f"), 12: (8, "d"),
    13: (4, "I"),
}

_NEW_SUBFILE_TYPE = 0x00FE
_IMAGE_WIDTH = 0x0100
_IMAGE_LENGTH = 0x0101
_BITS_PER_SAMPLE = 0x0102
_COMPRESSION = 0x0103
_PHOTOMETRIC = 0x0106
_STRIP_OFFSETS = 0x0111
_SAMPLES_PER_PIXEL = 0x0115
_STRIP_BYTE_COUNTS = 0x0117
_SUB_IFDS = 0x014A
_CFA_REPEAT_PATTERN_DIM = 0x828D
_CFA_PATTERN = 0x828E
_BLACK_LEVEL = 0xC61A
_WHITE_LEVEL = 0xC61D
_AS_SHOT_NEUTRAL = 0xC628
_ACTIVE_AREA = 0xC68D

_PHOTOMETRIC_RGB = 2
_PHOTOMETRIC_CFA = 32803

# The thumbnail is for previews, so a display gamma is enough.
_THUMB_GAMMA = 1 / 2.2


def _read_ifd(data, offset, endian):
    """Read one image file directory into {tag: tuple of values}."""
    if offset < 8 or offset + 2 > len(data):
        raise DngError(
            f"The header points to a directory at {offset}, outside its "
            f"{len(data)} bytes"
        )
    (count,) = struct.unpack_from(endian + "H", data, offset)
    end = offset + 2 + 12 * count
    if end + 4 > len(data):
        raise DngError(
            f"The directory at {offset} has {count} entries and runs past "
            f"the end of the {len(data)}-byte header"
        )
    entries = {}
    for i in range(count):
        at = offset + 2 + 12 * i
        tag, typ, n = struct.unpack_from(endian + "HHI", data, at)
        if typ not in _TYPES:
            continue
        size, code = _TYPES[typ]
        total = size * n
        if total <= 4:
            where = at + 8
        else:
            (where,) = struct.unpack_from(endian + "I", data, at + 8)
            if where + total > len(data):
                raise DngError(
                    f"Tag 0x{tag:04x} points to {total} bytes at {where}, "
                    f"outside the {len(data)}-byte header"
                )
        if typ == 2:
            entries[tag] = (bytes(data[where:where + total]),)
            continue
        values = struct.unpack_from(endian + code * n, data, where)
        if typ in (5, 10):
            pairs = []
            for num, den in zip(values[::2], values[1::2]):
                if den == 0:
                    raise DngError(f"Tag 0x{tag:04x} has a zero denominator")
                pairs.append(num / den)
            values = tuple(pairs)
        entries[tag] = values
    return entries


def _one(ifd, tag, name, default=None):
    values = ifd.get(tag)
    if not values:
        if default is not None:
            return default
        raise DngError(f"The {name} has no tag 0x{tag:04x}")
    return values[0]


def _single_strip(ifd, name):
    offsets = ifd.get(_STRIP_OFFSETS, ())
    counts = ifd.get(_STRIP_BYTE_COUNTS, ())
    if len(offsets) != 1 or len(counts) != 1:
        raise DngError(
            f"The {name} is not stored as a single strip "
            f"({len(offsets)} offsets, {len(counts)} byte counts)"
        )
    return int(offsets[0]), int(counts[0])


def read_dng_header(header):
    """Read the thumbnail and raw image descriptions from a DNG header.

    Args:
        header: The bytes CHDK sends for the DNG header (format 0x04).

    Returns:
        A DngHeader.

    Raises:
        DngError: If the header is not a TIFF, or does not describe an
            uncompressed 8-bit RGB thumbnail in IFD0 and an
            uncompressed single-strip CFA image with a 2x2 pattern in a
            SubIFD.
    """
    header = bytes(header)
    if header[:4] == b"II*\x00":
        endian = "<"
    elif header[:4] == b"MM\x00*":
        endian = ">"
    else:
        raise DngError(
            "The DNG header is not a TIFF: it does not begin with a TIFF "
            f"byte-order mark (first bytes {header[:4]!r})"
        )
    (ifd0_offset,) = struct.unpack_from(endian + "I", header, 4)
    ifd0 = _read_ifd(header, ifd0_offset, endian)

    raw_ifd = None
    for sub_offset in ifd0.get(_SUB_IFDS, ()):
        sub = _read_ifd(header, sub_offset, endian)
        if sub.get(_PHOTOMETRIC, (None,))[0] == _PHOTOMETRIC_CFA:
            raw_ifd = sub
            break
    if raw_ifd is None:
        raise DngError(
            "The DNG header describes no CFA raw image: no SubIFD has "
            "PhotometricInterpretation 32803"
        )

    # The thumbnail: IFD0, uncompressed 8-bit RGB.
    thumb_width = int(_one(ifd0, _IMAGE_WIDTH, "thumbnail"))
    thumb_height = int(_one(ifd0, _IMAGE_LENGTH, "thumbnail"))
    if (_one(ifd0, _PHOTOMETRIC, "thumbnail") != _PHOTOMETRIC_RGB
            or _one(ifd0, _SAMPLES_PER_PIXEL, "thumbnail", 1) != 3
            or tuple(ifd0.get(_BITS_PER_SAMPLE, ())) != (8, 8, 8)
            or _one(ifd0, _COMPRESSION, "thumbnail", 1) != 1):
        raise DngError(
            "The DNG header's IFD0 is not an uncompressed 8-bit RGB "
            "thumbnail"
        )
    thumb_offset, thumb_bytes = _single_strip(ifd0, "thumbnail")
    if thumb_bytes != thumb_width * thumb_height * 3:
        raise DngError(
            f"The thumbnail strip is {thumb_bytes} bytes; a "
            f"{thumb_width}x{thumb_height} RGB thumbnail needs "
            f"{thumb_width * thumb_height * 3}"
        )

    # The raw image.
    width = int(_one(raw_ifd, _IMAGE_WIDTH, "raw image"))
    height = int(_one(raw_ifd, _IMAGE_LENGTH, "raw image"))
    bits = int(_one(raw_ifd, _BITS_PER_SAMPLE, "raw image"))
    if _one(raw_ifd, _COMPRESSION, "raw image", 1) != 1:
        raise DngError("The raw image is compressed; only uncompressed is read")
    if not 8 <= bits <= 16:
        raise DngError(f"The raw image has {bits} bits per sample")
    if _one(raw_ifd, _SAMPLES_PER_PIXEL, "raw image", 1) != 1:
        raise DngError("The raw image has more than one sample per pixel")
    raw_offset, raw_bytes = _single_strip(raw_ifd, "raw image")
    row_bytes = (width * bits + 7) // 8
    if raw_bytes != row_bytes * height:
        raise DngError(
            f"The raw strip is {raw_bytes} bytes, which does not match the "
            f"dimensions: {width}x{height} at {bits} bits is "
            f"{row_bytes * height}"
        )
    if raw_bytes % 2:
        raise DngError(
            f"The raw strip is {raw_bytes} bytes, an odd number, so its "
            "byte pairs cannot be swapped"
        )
    if tuple(raw_ifd.get(_CFA_REPEAT_PATTERN_DIM, ())) != (2, 2):
        raise DngError("The raw image does not have a 2x2 CFA pattern")
    cfa = tuple(int(v) for v in raw_ifd.get(_CFA_PATTERN, ()))
    if len(cfa) != 4 or set(cfa) != {0, 1, 2}:
        raise DngError(f"The raw image's CFA pattern {cfa} is not RGB 2x2")

    black_values = raw_ifd.get(_BLACK_LEVEL, (0,))
    black = sum(black_values) / len(black_values)
    white = float(_one(raw_ifd, _WHITE_LEVEL, "raw image", (1 << bits) - 1))
    if not 0 <= black < white:
        raise DngError(
            f"The raw image's black level {black} is not below its white "
            f"level {white}"
        )
    area = tuple(int(v) for v in raw_ifd.get(
        _ACTIVE_AREA, (0, 0, height, width)))
    top, left, bottom, right = area if len(area) == 4 else (None,) * 4
    if (top is None or not 0 <= top < bottom <= height
            or not 0 <= left < right <= width
            or bottom - top < 2 or right - left < 2):
        raise DngError(
            f"The raw image's active area {area} does not fit "
            f"{width}x{height}"
        )

    neutral = ifd0.get(_AS_SHOT_NEUTRAL)
    if neutral is not None and (len(neutral) != 3 or min(neutral) <= 0):
        neutral = None

    return DngHeader(
        thumb_width=thumb_width, thumb_height=thumb_height,
        thumb_offset=thumb_offset, thumb_bytes=thumb_bytes,
        width=width, height=height, bits=bits,
        raw_offset=raw_offset, raw_bytes=raw_bytes,
        cfa_pattern=cfa, black_level=black, white_level=white,
        active_area=area, as_shot_neutral=neutral,
    )


def _thumbnail(info, raw):
    """Make the RGB thumbnail by sampling the swapped raw data.

    One 2x2 CFA block per thumbnail pixel, taken from the middle of the
    stretch of the active area that pixel covers: red and blue from
    their own sites, green as the mean of the block's two greens. Each
    channel is scaled from the black level to the white level, given
    the as-shot white balance when the header carries one, and
    gamma-encoded. Only width x height x 4 samples are read, so the
    cost does not depend on the size of the raw image.

    The CFA pattern is taken as anchored at row 0, column 0 of the raw
    image, and the blocks start on even rows and columns. Where the
    active area begins on an even row and column — 26 and 16 on the
    A2500 — this is the same as anchoring it at the active area.
    """
    bits = info.bits
    mask = (1 << bits) - 1
    row_bits = ((info.width * bits + 7) // 8) * 8
    top, left, bottom, right = info.active_area
    top += top & 1
    left += left & 1
    block_rows = (bottom - top) // 2
    block_cols = (right - left) // 2
    if block_rows < 1 or block_cols < 1:
        raise DngError(f"The active area {info.active_area} has no 2x2 block")

    if info.as_shot_neutral:
        green = info.as_shot_neutral[1]
        gains = [green / n for n in info.as_shot_neutral]
    else:
        gains = [1.0, 1.0, 1.0]
    black = info.black_level
    span = info.white_level - black
    luts = []
    for gain in gains:
        lut = bytearray(mask + 1)
        for level in range(mask + 1):
            v = (level - black) / span * gain
            if v <= 0:
                continue
            lut[level] = 255 if v >= 1 else int(255 * v ** _THUMB_GAMMA + 0.5)
        luts.append(lut)

    def sample(row, col):
        bit = row * row_bits + col * bits
        byte = bit >> 3
        shift = bit & 7
        n = (shift + bits + 7) >> 3
        value = int.from_bytes(raw[byte:byte + n], "big")
        return (value >> (n * 8 - shift - bits)) & mask

    cfa = info.cfa_pattern
    tw, th = info.thumb_width, info.thumb_height
    cols = [left + ((2 * x + 1) * block_cols // (2 * tw)) * 2 for x in range(tw)]
    out = bytearray(tw * th * 3)
    i = 0
    for y in range(th):
        row = top + ((2 * y + 1) * block_rows // (2 * th)) * 2
        for col in cols:
            sums = [0, 0, 0]
            counts = [0, 0, 0]
            for k, (dy, dx) in enumerate(((0, 0), (0, 1), (1, 0), (1, 1))):
                colour = cfa[k]
                sums[colour] += sample(row + dy, col + dx)
                counts[colour] += 1
            out[i] = luts[0][sums[0] // counts[0]]
            out[i + 1] = luts[1][sums[1] // counts[1]]
            out[i + 2] = luts[2][sums[2] // counts[2]]
            i += 3
    return bytes(out)


def assemble_dng(header, raw):
    """Build a DNG file from a CHDK DNG header and its raw data.

    The file is the header, then a thumbnail made from the raw data,
    then the raw data with every pair of bytes swapped. Offsets and
    sizes are read from the header, and the header has to place the
    thumbnail straight after itself and the raw data straight after
    the thumbnail, which is the layout CHDK's header describes.

    Args:
        header: DNG header bytes (remote capture format 0x04).
        raw: Raw data bytes as received (format 0x02).

    Returns:
        The DNG as bytes.

    Raises:
        DngError: A ValueError. Raised when the header is not a TIFF
            this can read, when the raw data's length differs from the
            header's byte count, or when the header's offsets do not
            lay out header + thumbnail + raw data end to end.
    """
    header = bytes(header)
    info = read_dng_header(header)
    if len(raw) != info.raw_bytes:
        raise DngError(
            f"The raw data is {len(raw)} bytes; the header declares "
            f"{info.raw_bytes}"
        )
    if info.thumb_offset != len(header):
        raise DngError(
            f"The thumbnail offset is {info.thumb_offset}; it has to be "
            f"{len(header)}, the end of the header"
        )
    if info.raw_offset != info.thumb_offset + info.thumb_bytes:
        raise DngError(
            f"The raw data offset is {info.raw_offset}; it has to be "
            f"{info.thumb_offset + info.thumb_bytes}, the end of the "
            "thumbnail"
        )

    swapped = array("H")
    swapped.frombytes(raw)
    swapped.byteswap()
    view = memoryview(swapped).cast("B")
    thumb = _thumbnail(info, view)
    return b"".join((header, thumb, view))
