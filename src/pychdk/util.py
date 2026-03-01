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
