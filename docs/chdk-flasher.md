# CHDK SD Card Flasher

A single-file Python CLI tool that downloads, formats, and flashes CHDK firmware onto SD cards for Canon Powershot A2500 cameras.

## Scope

- **Camera:** Canon Powershot A2500, firmware 1.00A only
- **CHDK build:** 1.6.1-6315 complete, from mighty-hoernsche.de
- **Platform:** macOS only (uses `diskutil`, `dd`)
- **Location:** `tools/flash_chdk.py`
- **Dependencies:** Python 3 stdlib only (`urllib.request`, `zipfile`, `subprocess`, `json`, `tempfile`, `plistlib`)

## What it does

1. Downloads the CHDK zip (cached at `~/.cache/pychdk/`)
2. Detects removable/external SD cards via `diskutil list -plist`
3. Confirms which disk to use with the user
4. Formats the card as FAT32 with volume label `CHDK_A2500`
5. Extracts CHDK files (`CHDK/` folder + `DISKBOOT.BIN`) to card root
6. Patches FAT32 boot sector with `BOOTDISK` at offset 0x1E0 (requires `sudo`)
7. Writes `OWN.TXT` with camera side (`ODD\n` or `EVEN\n`) if specified
8. Ejects the card

## Safety

- Only lists removable, external disks — internal drives are filtered out via `diskutil info -plist` checking for `RemovableMedia` and `Internal` keys
- Requires explicit `y` confirmation before formatting
- If multiple removable disks are found, asks user to pick
- If no removable disks found, tells user to insert a card and exits

## Boot sector patching

After formatting and copying files:

1. Unmount the partition (`diskutil unmount /dev/diskNs1`)
2. Read first 512 bytes via `sudo dd if=/dev/diskNs1 bs=512 count=1`
3. Write `BOOTDISK` (8 ASCII bytes) at offset 0x1E0
4. Write patched sector back via `sudo dd of=/dev/diskNs1 bs=512 count=1`
5. Re-mount, verify, eject

## Camera side assignment

After flashing, the script prompts:

```
Which side is this camera? [o]dd / [e]ven / [s]kip:
```

Writes `ODD\n` or `EVEN\n` to `OWN.TXT` on the card root. This is how Captua identifies left vs right cameras — it reads `OWN.TXT` via `download_file('OWN.TXT')`.

## Example session

```
$ python3 tools/flash_chdk.py

Downloading CHDK 1.6.1-6315 for A2500... (cached at ~/.cache/pychdk/a2500-100a-1.6.1-6315-full.zip)
Found removable disk: /dev/disk4 (SDCARD, 16GB, FAT32)
This will ERASE /dev/disk4. Continue? [y/N] y
Formatting /dev/disk4 as FAT32...
Extracting CHDK files to /Volumes/CHDK_A2500...
Patching boot sector...
Password: ********
Which side is this camera? [o]dd / [e]ven / [s]kip: o
Writing OWN.TXT (ODD)...
Ejecting...
Done! Lock the SD card and insert into camera.
```
