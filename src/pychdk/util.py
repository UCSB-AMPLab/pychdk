"""Exposure conversion utilities for CHDK cameras.

CHDK uses APEX96 values (APEX * 96) for exposure parameters.
These functions convert between human-readable values and APEX96.
"""
import math
import re


def shutter_to_tv96(shutter_speed):
    """Convert shutter speed in seconds to TV96 (APEX96 time value).

    Formula: TV96 = -96 * log2(shutter_speed)

    Args:
        shutter_speed: Shutter speed in seconds (e.g., 0.01 for 1/100s).

    Returns:
        Integer TV96 value.
    """
    return round(-96 * math.log2(shutter_speed))


def iso_to_sv96(real_iso):
    """Convert REAL ISO sensitivity to SV96 (APEX96 sensitivity value).

    Formula: SV96 = 96 * log2(real_iso / 3.125)

    This is the APEX96 conversion for real sensitivity, and it is the
    same arithmetic CHDK does: shooting_get_sv96_from_iso computes
    log2(iso * 32 / 100) * 96 (core/shooting.c), which is the identical
    quantity written with 32/100 instead of 1/3.125, and that function
    is what CHDK's own Lua iso_to_sv96 calls (modules/luascript.c). The
    result is what set_sv96 wants: Lua set_sv96 goes to
    shooting_set_sv96, and its counterpart get_sv96 reads
    shooting_get_sv96_real (core/shooting.c) — both in real units.

    This is arithmetic, not a market-to-real conversion. Feed it a
    market ISO — the number in the camera's own ISO menu — and you get
    the sv96 for that number; the mistake is then treating that result
    as a real sv96, which is what set_sv96 takes.

    CHDK keeps the two quantities apart deliberately: it stores them in
    separate properties (PROPCASE_SV for real, PROPCASE_SV_MARKET for
    market) and exposes iso_market_to_real, iso_real_to_market,
    sv96_market_to_real and sv96_real_to_market to move between them.
    The offset is per-camera and is not one number: core/shooting.c
    defaults SV96_MARKET_OFFSET to 69 sv96 units under
    `#if !defined(SV96_MARKET_OFFSET)`, with the comment "Can be
    overriden in platform_camera.h (see IXUS700 for example)" — and the
    IXUS700 platform does override it, to 20.

    So this library does not convert between them. Before handing this
    result to set_sv96, a market number needs the camera's own
    market-to-real step. ChdkDevice.shoot does that on the camera and
    therefore does not call this function at all; see its docstring.

    Read from the CHDK sources named above. Not measured on a camera.

    Args:
        real_iso: Real ISO sensitivity (e.g., 100, 200, 400).

    Returns:
        Integer SV96 value.
    """
    return round(96 * math.log2(real_iso / 3.125))


def aperture_to_av96(aperture):
    """Convert f-number to AV96 (APEX96 aperture value).

    Formula: AV96 = 192 * log2(aperture)

    Args:
        aperture: f-number (e.g., 2.8, 5.6, 8.0).

    Returns:
        Integer AV96 value.
    """
    return round(192 * math.log2(aperture))


def to_camerapath(path):
    """Ensure path has the camera filesystem prefix 'A/'.

    CHDK cameras use 'A/' as the root of the SD card filesystem.

    Args:
        path: File path, with or without 'A/' prefix.

    Returns:
        Path with 'A/' prefix.
    """
    if path.startswith("A/") or path.startswith("A\\"):
        return path
    return "A/" + path


# A camera id is hex, long enough not to collide and short enough to
# read aloud over a workbench. The flasher mints twelve characters.
_CAMERA_ID_RE = re.compile(r"^[0-9a-f]{12,32}$")


def parse_own_txt(data):
    """Read a camera's page parity and id from OWN.TXT.

    OWN.TXT records which pages a body shoots — ODD or EVEN — not which
    side of the table it stands on; which parity appears on the left is
    the operator's reading direction, and the file says nothing about
    it. A second line, 'id=<hex>', carries a stable identity for the
    body, because pyusb cannot always read a serial number from a Canon
    compact.

    Tolerates CRLF, a byte order mark, surrounding whitespace, blank
    lines, lowercase keywords, a missing id line and unknown lines. It
    never raises: rubbish simply parses as nothing.

    An id counts only when it is twelve to thirty-two hexadecimal
    characters. Every 'id=' line is examined and the first valid one
    wins, so a corrupt line cannot shield a good one below it; any
    further valid id is ignored rather than merged, because a file with
    two identities has no way to say which body it belongs to. The id
    comes back lowercased, so callers can compare it directly.

    Args:
        data: File contents as bytes or str.

    Returns:
        Tuple of (side, camera_id), where side is 'ODD', 'EVEN' or None
        and camera_id is the first well-formed id, lowercased, or None.
    """
    if isinstance(data, (bytes, bytearray)):
        text = bytes(data).decode("utf-8-sig", errors="replace")
    elif isinstance(data, str):
        text = data
    else:
        return None, None

    side = None
    camera_id = None
    for line in text.splitlines():
        line = line.replace("\ufeff", "").strip()
        if not line:
            continue
        if side is None and line.upper() in ("ODD", "EVEN"):
            side = line.upper()
        elif camera_id is None and line.upper().startswith("ID="):
            value = line.split("=", 1)[1].strip().lower()
            if _CAMERA_ID_RE.match(value):
                camera_id = value
    return side, camera_id


def format_own_txt(side, camera_id=None):
    """Render the canonical OWN.TXT for a page parity and camera id.

    The parity is which pages the body shoots, ODD or EVEN, not a side
    of the table. Each line is written only when there is something to
    put on it: an id with no parity is a valid file, since a body keeps
    its identity whether or not an operator has decided which pages it
    shoots, and parse_own_txt reads it back. With neither, there is
    nothing to record and the result is empty.

    Args:
        side: 'ODD' or 'EVEN', in any case, or None for no parity.
        camera_id: Stable hex identity for the body, or None.

    Returns:
        File contents as a str with a single trailing newline, or an
        empty string when there is nothing to write.
    """
    lines = []
    if side:
        lines.append(str(side).strip().upper())
    if camera_id:
        lines.append("id=" + str(camera_id).strip())
    if not lines:
        return ""
    return "\n".join(lines) + "\n"
