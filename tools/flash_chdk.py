#!/usr/bin/env python3
"""Flash CHDK firmware onto SD cards for Canon A2500 cameras.

Downloads CHDK 1.6.1-6315, formats the SD card as FAT32, extracts
CHDK files, patches the boot sector to make it bootable, and optionally
writes OWN.TXT for camera side assignment (ODD/EVEN).
"""

import plistlib
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

CHDK_URL = "https://www.mighty-hoernsche.de/bins/a2500-100a-1.6.1-6315-full.zip"
CHDK_FILENAME = "a2500-100a-1.6.1-6315-full.zip"
CACHE_DIR = Path.home() / ".cache" / "pychdk"
VOLUME_LABEL = "CHDK_A2500"


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command, exit on failure unless check=False."""
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.returncode != 0 and kwargs.get("check") is not False:
        print(f"Command failed: {' '.join(cmd)}")
        if result.stderr:
            print(result.stderr.strip())
        sys.exit(1)
    return result


def download_chdk() -> Path:
    """Download CHDK zip, using cached copy if available."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = CACHE_DIR / CHDK_FILENAME
    if cached.exists():
        print(f"Using cached {cached}")
        return cached
    print("Downloading CHDK 1.6.1-6315 for A2500...")
    urllib.request.urlretrieve(CHDK_URL, cached)
    print(f"Saved to {cached}")
    return cached


def find_removable_disks() -> list[dict]:
    """Find physical removable disks (SD cards) using diskutil.

    Returns list of dicts with keys: disk, name, size_gb.
    Filters to physical, removable media — excludes disk images
    and synthesized volumes. Works with both external USB readers
    and built-in SD card slots.
    """
    result = _run(["diskutil", "list", "-plist"])
    plist = plistlib.loads(result.stdout.encode())

    disks = []
    for disk_id in plist.get("WholeDisks", []):
        info_result = _run(["diskutil", "info", "-plist", disk_id])
        info = plistlib.loads(info_result.stdout.encode())

        if not info.get("RemovableMedia", False):
            continue
        if info.get("VirtualOrPhysical") != "Physical":
            continue

        size_bytes = info.get("TotalSize", 0)
        size_gb = size_bytes / (1024**3)
        name = info.get("MediaName", "Unknown")
        disks.append({
            "disk": f"/dev/{disk_id}",
            "name": name,
            "size_gb": round(size_gb, 1),
        })
    return disks


def pick_disk(disks: list[dict]) -> str:
    """Let user pick a disk. Returns /dev/diskN path."""
    if not disks:
        print("No removable disks found. Insert an SD card and try again.")
        sys.exit(1)

    if len(disks) == 1:
        d = disks[0]
        print(f"Found removable disk: {d['disk']} ({d['name']}, {d['size_gb']}GB)")
    else:
        print("Found multiple removable disks:")
        for i, d in enumerate(disks):
            print(f"  [{i}] {d['disk']} ({d['name']}, {d['size_gb']}GB)")
        choice = input("Which disk? ").strip()
        try:
            return disks[int(choice)]["disk"]
        except (ValueError, IndexError):
            print("Invalid choice.")
            sys.exit(1)

    confirm = input("This will ERASE the disk. Continue? [y/N] ").strip().lower()
    if confirm != "y":
        print("Aborted.")
        sys.exit(0)
    return disks[0]["disk"]


def format_card(disk: str) -> str:
    """Format disk as FAT32. Returns mount point."""
    print(f"Formatting {disk} as FAT32...")
    _run([
        "diskutil", "eraseDisk",
        "FAT32", VOLUME_LABEL,
        "MBRFormat", disk,
    ])
    partition = disk + "s1"
    info_result = _run(["diskutil", "info", "-plist", partition])
    info = plistlib.loads(info_result.stdout.encode())
    mount_point = info.get("MountPoint", f"/Volumes/{VOLUME_LABEL}")
    print(f"Formatted. Mounted at {mount_point}")
    return mount_point


def extract_chdk(zip_path: Path, mount_point: str):
    """Extract CHDK files to the SD card."""
    print(f"Extracting CHDK files to {mount_point}...")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(mount_point)

    diskboot = Path(mount_point) / "DISKBOOT.BIN"
    chdk_dir = Path(mount_point) / "CHDK"
    if not diskboot.exists():
        print("ERROR: DISKBOOT.BIN not found after extraction.")
        sys.exit(1)
    if not chdk_dir.is_dir():
        print("ERROR: CHDK/ directory not found after extraction.")
        sys.exit(1)
    print("Extracted: DISKBOOT.BIN + CHDK/")


def patch_boot_sector(disk: str):
    """Write BOOTDISK signature to FAT32 boot sector at offset 0x1E0.

    Requires sudo for raw disk access.
    """
    partition = disk + "s1"

    print("Unmounting partition for boot sector patching...")
    _run(["diskutil", "unmount", partition])

    print("Patching boot sector (requires sudo)...")
    result = subprocess.run(
        ["sudo", "dd", f"if={partition}", "bs=512", "count=1"],
        capture_output=True,
    )
    if result.returncode != 0:
        print(f"Failed to read boot sector: {result.stderr.decode()}")
        sys.exit(1)

    sector = bytearray(result.stdout)
    if len(sector) != 512:
        print(f"ERROR: Boot sector is {len(sector)} bytes, expected 512.")
        sys.exit(1)

    offset = 0x1E0
    sector[offset : offset + 8] = b"BOOTDISK"

    result = subprocess.run(
        ["sudo", "dd", f"of={partition}", "bs=512", "count=1"],
        input=bytes(sector),
        capture_output=True,
    )
    if result.returncode != 0:
        print(f"Failed to write boot sector: {result.stderr.decode()}")
        sys.exit(1)

    print("Boot sector patched.")


def get_mount_point(disk: str) -> str:
    """Mount partition and return mount point."""
    partition = disk + "s1"
    _run(["diskutil", "mount", partition])
    info_result = _run(["diskutil", "info", "-plist", partition])
    info = plistlib.loads(info_result.stdout.encode())
    return info.get("MountPoint", f"/Volumes/{VOLUME_LABEL}")


def write_camera_side(mount_point: str):
    """Ask user for camera side and write OWN.TXT."""
    choice = input("Which side is this camera? [o]dd / [e]ven / [s]kip: ").strip().lower()
    if choice in ("o", "odd"):
        side = "ODD"
    elif choice in ("e", "even"):
        side = "EVEN"
    elif choice in ("s", "skip", ""):
        print("Skipping camera side assignment.")
        return
    else:
        print(f"Unknown choice '{choice}', skipping.")
        return

    own_txt = Path(mount_point) / "OWN.TXT"
    own_txt.write_text(side + "\n")
    print(f"Wrote OWN.TXT ({side})")


def eject_card(disk: str):
    """Eject the disk."""
    print("Ejecting...")
    _run(["diskutil", "eject", disk])


def main():
    zip_path = download_chdk()
    disks = find_removable_disks()
    disk = pick_disk(disks)
    mount_point = format_card(disk)
    extract_chdk(zip_path, mount_point)
    patch_boot_sector(disk)

    mount_point = get_mount_point(disk)
    write_camera_side(mount_point)
    eject_card(disk)
    print("Done! Lock the SD card and insert into camera.")


if __name__ == "__main__":
    main()
