"""Pure Python CHDK PTP camera control."""
import importlib

__version__ = "0.1.1"

__all__ = [
    "ChdkDevice", "list_devices", "DeviceInfo", "install_signal_handlers",
    "PTPError", "ChdkPTP", "MultiCam", "util",
    "parse_own_txt", "format_own_txt",
]

# Submodules reachable as attributes of the package.
_SUBMODULES = ("util",)

# Every other export, mapped to the submodule that owns it.
_EXPORTS = {
    "ChdkDevice": "pychdk.device",
    "list_devices": "pychdk.device",
    "DeviceInfo": "pychdk.device",
    "install_signal_handlers": "pychdk.device",
    "PTPError": "pychdk.ptp",
    "ChdkPTP": "pychdk.chdk",
    "MultiCam": "pychdk.multicam",
    "parse_own_txt": "pychdk.util",
    "format_own_txt": "pychdk.util",
}


def __getattr__(name):
    """Resolve an export on first access (PEP 562).

    Importing the submodules eagerly meant that reaching pychdk.util —
    which needs nothing but math — imported pychdk.device and so pyusb.
    Each name now costs only the submodule that owns it, and is cached
    in the module globals afterwards.

    Args:
        name: Attribute being looked up on the package.

    Returns:
        The submodule or the exported object.

    Raises:
        AttributeError: If the package exports no such name.
    """
    if name in _SUBMODULES:
        value = importlib.import_module(f"pychdk.{name}")
    elif name in _EXPORTS:
        value = getattr(importlib.import_module(_EXPORTS[name]), name)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value


def __dir__():
    """List the exports alongside whatever has already been resolved."""
    return sorted(set(globals()) | set(__all__))
