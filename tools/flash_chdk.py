#!/usr/bin/env python3
"""Flash CHDK firmware onto SD cards for Canon A2500 cameras.

Downloads CHDK 1.6.1-6315, formats the SD card as FAT32, extracts
CHDK files, patches the boot sector to make it bootable, and optionally
writes OWN.TXT for camera side assignment (ODD/EVEN).
"""

import plistlib
import secrets
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pychdk.util import format_own_txt, parse_own_txt

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


def _mounted_path(disk: str) -> str | None:
    """Return where the card is mounted, or None if it will not mount.

    get_mount_point exits the process when diskutil fails, which is
    why SystemExit is caught here.
    """
    try:
        return get_mount_point(disk)
    except (SystemExit, ValueError):
        return None


CARD_BLANK = "blank"
CARD_HAS_FILESYSTEM = "filesystem"
CARD_UNKNOWN = "unknown"


# The keys every whole-disk entry of `diskutil list -plist` carries,
# captured from real output. An entry without them is not a shape we
# recognize, and an unrecognized shape is never called blank.
_WHOLE_DISK_KEYS = frozenset({"Content", "DeviceIdentifier", "Size"})


def _disk_identifier(disk: str) -> str:
    """Reduce /dev/diskN to the diskN that diskutil reports."""
    return disk.rsplit("/", 1)[-1]


def _entry_holds_a_filesystem(entry: dict) -> bool:
    """Whether one diskutil layout entry describes anything mountable."""
    if entry.get("Content"):
        return True
    return bool(entry.get("Partitions") or entry.get("APFSVolumes"))


def _entry_is_recognizably_blank(entry: dict) -> bool:
    """Whether an entry positively shows a device with nothing on it.

    Being unable to find a filesystem is not the same as finding none:
    an empty entry, or one made of keys we do not know, says nothing
    about the card and must not be read as saying it is empty.
    """
    if not _WHOLE_DISK_KEYS.issubset(entry):
        return False
    return not _entry_holds_a_filesystem(entry)


def _classify_card(disk: str) -> str:
    """Ask diskutil what the whole card holds.

    Inspecting one partition cannot answer this: an existing but
    unformatted diskNs1 reports fine and then will not mount, and a
    diskNs1 that is missing does not prove the card is empty, since the
    filesystem may be on the whole device or on another partition. So
    read the layout of the whole device and judge from every entry in
    it. `diskutil list -plist` returns AllDisksAndPartitions, whose
    entries carry a Content naming the partition scheme or filesystem
    and, when there is one, a Partitions or APFSVolumes list.

    Blank has to be recognized, not inferred from failing to recognize
    anything else: the payload must describe the device we asked about,
    in the shape we captured, and show it empty. Anything we cannot
    read, cannot understand, or that turns out to be about some other
    disk is CARD_UNKNOWN and stops the run. Only a layout that
    positively shows an empty card is safe to erase without looking.

    Args:
        disk: /dev/diskN path.

    Returns:
        CARD_BLANK, CARD_HAS_FILESYSTEM or CARD_UNKNOWN.
    """
    result = _run(["diskutil", "list", "-plist", disk], check=False)
    if result.returncode != 0:
        return CARD_UNKNOWN
    try:
        layout = plistlib.loads(result.stdout.encode())
    except Exception:
        return CARD_UNKNOWN
    if not isinstance(layout, dict):
        return CARD_UNKNOWN
    entries = layout.get("AllDisksAndPartitions")
    if not isinstance(entries, list) or not entries:
        # diskutil succeeded but told us nothing about this disk.
        return CARD_UNKNOWN
    if not all(isinstance(entry, dict) for entry in entries):
        return CARD_UNKNOWN

    # Any filesystem anywhere in the payload is enough to stop.
    for entry in entries:
        parts = entry.get("Partitions") or []
        volumes = entry.get("APFSVolumes") or []
        if not isinstance(parts, list) or not isinstance(volumes, list):
            return CARD_UNKNOWN
        if _entry_holds_a_filesystem(entry):
            return CARD_HAS_FILESYSTEM
        for part in list(parts) + list(volumes):
            if isinstance(part, dict) and _entry_holds_a_filesystem(part):
                return CARD_HAS_FILESYSTEM

    # Nothing found — but only the entry for the device we asked about
    # can say the card is empty, and only in a shape we recognize.
    wanted = _disk_identifier(disk)
    described = [
        entry for entry in entries
        if entry.get("DeviceIdentifier") == wanted
    ]
    if len(described) != 1:
        return CARD_UNKNOWN
    if not _entry_is_recognizably_blank(described[0]):
        return CARD_UNKNOWN
    return CARD_BLANK


def read_existing_own_txt(disk: str) -> tuple[str | None, str | None]:
    """Read a card's parity and camera id before the card is erased.

    main() formats the card long before OWN.TXT could be read off it,
    so the id has to be salvaged first or it is gone. That makes the
    difference between "this card has no id" and "this card's id could
    not be read" worth keeping: the first is an ordinary blank card,
    the second means erasing would destroy an identity we cannot see.
    A card that reports no filesystem at all is genuinely blank, and
    formatting one is what this tool is for. A card that reports a
    volume we then could not mount or read is not blank — it is
    unexamined, and erasing it would be a guess.

    Args:
        disk: /dev/diskN path.

    Both fields are salvaged, not just the id: an operator who skips
    the parity prompt means "leave the assignment alone", which is only
    possible if we still know what the assignment was.

    Returns:
        Tuple of (side, camera_id), each None if the card genuinely
        carries none.

    Raises:
        SystemExit: If the card holds a filesystem that could not be
            inspected, so that no caller can go on to format it.
    """
    mount_point = _mounted_path(disk)
    if mount_point is None:
        if _classify_card(disk) == CARD_BLANK:
            print("Card holds no filesystem; treating it as blank.")
            return None, None
        print(
            "The card holds a filesystem that could not be inspected, "
            "or its layout could not be read at all. Stopping before it "
            "is erased: it may carry a camera id, and formatting would "
            "lose that body's identity. Check the card and the reader, "
            "then run this again."
        )
        sys.exit(1)

    own_txt = Path(mount_point) / "OWN.TXT"
    try:
        data = own_txt.read_bytes()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        print(f"Could not read {own_txt}: {exc}")
        print(
            "Stopping before the card is erased. This card may carry a "
            "camera id, and formatting it would lose that body's "
            "identity. Repair the card, or delete OWN.TXT deliberately, "
            "then run this again."
        )
        sys.exit(1)
    return parse_own_txt(data)


def write_camera_side(mount_point: str, existing_side: str | None = None,
                      existing_id: str | None = None):
    """Ask which pages this camera shoots and write OWN.TXT.

    The file carries the page parity and a stable id for the body. An
    id already on the card is kept, so re-flashing a card does not
    change which camera the toolkit thinks it is — including when the
    operator declines to set a parity, since the card has already been
    erased by then and skipping would otherwise throw the rescued id
    away. A card with neither parity nor id has nothing worth writing.

    Args:
        mount_point: Where the card is mounted.
        existing_side: Parity read off the card before it was erased.
            Skipping the prompt keeps it, so a card that said EVEN
            still says EVEN.
        existing_id: Id read off the card before it was erased, if any.
            Both fall back to reading OWN.TXT here, for a card that was
            not formatted in this run.
    """
    choice = input("Which pages does this camera shoot? [o]dd / [e]ven / [s]kip: ").strip().lower()
    if choice in ("o", "odd"):
        side = "ODD"
    elif choice in ("e", "even"):
        side = "EVEN"
    elif choice in ("s", "skip", ""):
        print("Skipping camera side assignment.")
        side = existing_side
    else:
        print(f"Unknown choice '{choice}', skipping.")
        side = existing_side

    own_txt = Path(mount_point) / "OWN.TXT"
    camera_id = existing_id
    if side is None or camera_id is None:
        try:
            on_card_side, on_card_id = parse_own_txt(own_txt.read_bytes())
        except OSError:
            on_card_side, on_card_id = None, None
        side = side or on_card_side
        camera_id = camera_id or on_card_id
    if camera_id:
        origin = "kept the id already on the card"
    elif side:
        camera_id = secrets.token_hex(6)
        origin = "minted a new id"
    else:
        # No parity to record and no identity to preserve.
        return

    own_txt.write_text(format_own_txt(side, camera_id))
    print(f"Wrote OWN.TXT ({side or 'no parity'}, id={camera_id}) — {origin}")


def eject_card(disk: str):
    """Eject the disk."""
    print("Ejecting...")
    _run(["diskutil", "eject", disk])


def main():
    zip_path = download_chdk()
    disks = find_removable_disks()
    disk = pick_disk(disks)
    # Salvage the body's identity before eraseDisk takes it away.
    existing_side, existing_id = read_existing_own_txt(disk)
    mount_point = format_card(disk)
    extract_chdk(zip_path, mount_point)
    patch_boot_sector(disk)

    mount_point = get_mount_point(disk)
    write_camera_side(mount_point, existing_side=existing_side,
                      existing_id=existing_id)
    eject_card(disk)
    print("Done! Lock the SD card and insert into camera.")


if __name__ == "__main__":
    main()
