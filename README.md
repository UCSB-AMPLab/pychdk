# pychdk

Pure Python CHDK PTP camera control. Communicates directly with Canon cameras running [CHDK](https://chdk.fandom.com/wiki/CHDK) firmware over USB using [pyusb](https://pyusb.github.io/pyusb/) — no Lua embedding or C compilation required on the host.

Inspired by [chdkptp.py](https://github.com/jbaiter/chdkptp.py) by Johannes Baiter, built as part of his work on [spreads](https://github.com/DIYBookScanner/spreads), but written from scratch to eliminate the Lua VM and C dependencies. It is designed for use with [Captua](https://github.com/UCSB-AMPLab/digitization-toolkit-software) but is usable as a standalone library for any CHDK camera automation.

## Features

- USB device discovery and PTP session management
- CHDK PTP extension protocol (opcode `0x9999`) — script execution, message passing, file transfer, live view, remote capture
- High-level `ChdkDevice` API for single-camera control
- `MultiCam` for coordinated multi-camera capture (concurrent shooting, mode switching)
- APEX96 exposure conversion utilities (shutter speed, ISO, aperture)
- macOS support — automatically handles `ptpcamerad` daemon that claims PTP devices

## Requirements

- Python 3.11+
- [libusb](https://libusb.info/) (install via `brew install libusb` on macOS)
- Canon camera with CHDK firmware connected via USB

## Installation

```bash
pip install -e ".[dev]"
```

## Quick start

```python
import pychdk

# Find connected cameras
devices = pychdk.list_devices()

# Connect to a camera
cam = pychdk.ChdkDevice(devices[0])

# Check which version of the CHDK PTP protocol the camera speaks.
# This is the protocol version only — not the camera's firmware, and not
# the CHDK build on the card. There is no dedicated method for those;
# for the build, run CHDK's own Lua:
#   cam.lua_execute("return get_buildinfo()")
major, minor = cam._chdk.get_version()
print(f"CHDK PTP protocol {major}.{minor}")

# Execute Lua on the camera
result = cam.lua_execute("return 2 + 2")
print(result)  # 4

# Switch to record mode and capture
cam.switch_mode("record")
data = cam.shoot(stream=True)  # JPEG bytes via remote capture
cam.close()
```

### Multi-camera capture

```python
import pychdk

mc = pychdk.MultiCam()  # discovers all connected cameras
mc.prepare_all("record")
results = mc.shoot(stream=True)  # concurrent capture
mc.close()
```

## Architecture

The library is layered bottom-up:

```
MultiCam          — coordinated multi-camera operations
ChdkDevice        — high-level single-camera API
ChdkPTP           — CHDK extension protocol (scripts, files, capture)
PTPSession        — PTP session and transaction management
PTPDevice         — USB transport (bulk read/write, endpoint discovery)
pyusb / libusb    — raw USB access
```

### Modules

| Module | Description |
|--------|-------------|
| `usb_transport.py` | USB device discovery, interface claiming, bulk I/O |
| `ptp.py` | PTP container packing/unpacking, session lifecycle, transactions |
| `chdk.py` | CHDK commands — script execution, message queue, file transfer, remote capture |
| `device.py` | `ChdkDevice` class and `list_devices()` discovery |
| `multicam.py` | `MultiCam` class for concurrent operations across cameras |
| `util.py` | APEX96 conversions (`shutter_to_tv96`, `iso_to_sv96`, `aperture_to_av96`) and camera path helpers |

## Camera side assignment

For book scanning with two cameras, each body is assigned a page parity — which pages it shoots — stored in a file called `OWN.TXT` on the camera's SD card. A parity line reads `ODD` or `EVEN`; which of the two sits on the left is the operator's reading direction, and the file says nothing about it. An id line, `id=3f9a1c2b7d4e`, gives the body a stable identity, because pyusb cannot always read a serial number from a Canon compact. The Captua workflow reads the file via `download_file('A/OWN.TXT')` to determine page sequencing and EXIF orientation, and to tell one body from the other across replugs. Note the `A/` prefix: `download_file` and `upload_file` both take a card path.

Either line can stand alone, so a reader must handle four shapes: both lines, a parity alone, an identity alone, and no file at all. `parse_own_txt` and `format_own_txt` in `util.py` read and write all four; treat a missing parity as unassigned rather than as an error.

Skipping the parity prompt while flashing means "leave the assignment alone", so a card that said `EVEN` still says `EVEN` afterwards. An identity-only file is what skipping produces on a card that had no parity to preserve — the body keeps its identity even though nobody has said yet which pages it shoots — and a card with neither a parity nor an identity gets no file at all. In practice `tools/flash_chdk.py` never writes a parity on its own, because it mints an identity whenever it writes one; a parity-only file comes from an older card or a hand-written one, and is read normally.

## Tools

### `tools/flash_chdk.py`

Prepares SD cards for Canon A2500 cameras with CHDK firmware. Downloads the CHDK build, formats the card as FAT32, extracts files, patches the boot sector to make it bootable, and optionally writes `OWN.TXT` for camera side assignment.

```bash
python3 tools/flash_chdk.py
```

The script:
1. Downloads and caches CHDK 1.6.1-6315 for A2500 (firmware 1.00A)
2. Detects physical removable disks (SD cards)
3. Formats the card as FAT32 with volume label `CHDK_A2500`
4. Extracts `DISKBOOT.BIN` and `CHDK/` to the card
5. Patches the FAT32 boot sector with `BOOTDISK` at offset `0x1E0` (requires sudo)
6. Asks which side (odd/even) and writes `OWN.TXT`
7. Ejects the card — lock it and insert into camera

macOS only. Requires Python 3 stdlib only (no extra dependencies).

## Testing

Unit tests use mocks and don't require camera hardware:

```bash
pytest tests/ -v
```

Integration test scripts in `examples/` require real cameras:

```bash
python3 examples/test_camera.py        # single camera
python3 examples/test_two_cameras.py   # two cameras via USB hub
```

## Project structure

```
src/pychdk/
  __init__.py          # public API exports
  usb_transport.py     # USB device discovery and bulk I/O
  ptp.py               # PTP protocol containers and sessions
  chdk.py              # CHDK extension protocol
  device.py            # high-level ChdkDevice API
  multicam.py          # multi-camera coordination
  util.py              # exposure conversions, path helpers
tests/
  test_usb_transport.py
  test_ptp.py
  test_chdk.py
  test_device.py
  test_multicam.py
  test_util.py
examples/
  test_camera.py       # single-camera integration test
  test_two_cameras.py  # two-camera integration test
tools/
  flash_chdk.py        # SD card preparation tool
```

## Known limitations

- **Remote capture on A2500**: The A2500 CHDK port is alpha-level, and streaming remote capture (`shoot(stream=True)`) can fail on it with PTP error `0x2002`. The library does not fall back — the exception reaches the caller and nothing else is attempted. Nor does the exception tell you what state the camera is in: the failure can come from the script submission, before `init_usb_capture` has run at all, or from a capture that initialized and was then cancelled. So whether USB remote capture is still enabled afterwards, and whether an ordinary `shoot()` would recover the picture, are both unestablished. Recovery is an open question for the bench, not a documented workaround.
- **macOS only** for `tools/flash_chdk.py`. The library itself works on macOS and Linux.
- **Canon cameras only** — CHDK is Canon-specific. Device discovery defaults to Canon's USB vendor ID (`0x04A9`).

## License

MIT. See [LICENSE](LICENSE).
