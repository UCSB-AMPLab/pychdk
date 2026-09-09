"""Exposure conversion utilities for CHDK cameras.

CHDK uses APEX96 values (APEX * 96) for exposure parameters.
These functions convert between human-readable values and APEX96.
"""
import math


def shutter_to_tv96(shutter_speed):
    """Convert shutter speed in seconds to TV96 (APEX96 time value).

    Formula: TV96 = -96 * log2(shutter_speed)

    Args:
        shutter_speed: Shutter speed in seconds (e.g., 0.01 for 1/100s).

    Returns:
        Integer TV96 value.
    """
    return round(-96 * math.log2(shutter_speed))


def iso_to_sv96(iso):
    """Convert ISO value to SV96 (APEX96 sensitivity value).

    Formula: SV96 = 96 * log2(ISO / 3.125)

    Args:
        iso: ISO sensitivity (e.g., 100, 200, 400).

    Returns:
        Integer SV96 value.
    """
    return round(96 * math.log2(iso / 3.125))


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

    Args:
        data: File contents as bytes or str.

    Returns:
        Tuple of (side, camera_id), where side is 'ODD', 'EVEN' or None
        and camera_id is the id line's value or None.
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
            value = line.split("=", 1)[1].strip()
            if value:
                camera_id = value
    return side, camera_id


def format_own_txt(side, camera_id=None):
    """Render the canonical OWN.TXT for a page parity and camera id.

    The parity is which pages the body shoots, ODD or EVEN, not a side
    of the table. The id line is written only when an id is given.

    Args:
        side: 'ODD' or 'EVEN', in any case.
        camera_id: Stable hex identity for the body, or None.

    Returns:
        File contents as a str, with a single trailing newline.
    """
    lines = [str(side).strip().upper()]
    if camera_id:
        lines.append("id=" + str(camera_id).strip())
    return "\n".join(lines) + "\n"
