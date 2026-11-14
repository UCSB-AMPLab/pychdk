# pychdk 0.1.4

DNG capture over USB. `shoot(stream=True, dng=True)` raised
`NotImplementedError` in 0.1.3; it now returns a complete DNG, and with
`jpeg=True` the camera's JPEG of the same exposure as well.

**This release is verified against hardware**, unlike 0.1.3. What was run,
and what was not, is under "Bench results".

## New

- **`shoot(stream=True, dng=True)`** returns the bytes of a DNG file.
- **`shoot(stream=True, dng=True, jpeg=True)`** returns a `DngCapture` with
  `.dng` and `.jpeg`, both from one exposure and one script
  (`init_usb_capture(7)`).
- **`pychdk.assemble_dng(header, raw)`** builds the file from the two parts
  CHDK sends. It reads the strip offsets and sizes from the header, swaps the
  byte pairs of the raw data, makes the thumbnail the header describes by
  sampling the raw data, and raises `DngError` for a header it cannot read or
  raw data of another length. Standard library only; about 20 ms on a desktop
  machine for a 24 MB file.
- `remote_capture_get_data` takes `accumulate=True`, so `last_capture_chunks`
  can count the chunks of every part of one capture.

## Unchanged

- `shoot(stream=True)` sends the script it sent in 0.1.3 and fetches one
  format. A test pins the script and the single fetch.
- `jpeg=True` without `stream=True` raises `ValueError`.
- Dependencies: `pyusb>=1.2.0`, `pytest>=7.0` for dev, `requires-python >=3.11`.

## Bench results

Two Canon PowerShot A2500, firmware 1.00A, CHDK 1.5.1, on 13 November 2026.

- **300 pairs with `shoot(stream=True, dng=True, jpeg=True)`**, both bodies
  held open, 600 captures. Every DNG had the expected length (24,762,432
  bytes) and every JPEG was whole. No capture failed.
- **Time per capture:** 3.4 s with a page under the cameras; 4.3 to 4.7 s on
  an empty glass, where the cameras hunt for focus.
- **The files open in macOS** and render correctly, thumbnail included.
- The camera offers the header first, then the raw data in one chunk, then
  reads no format ready for about two seconds while the script runs, then the
  JPEG.

Not run:

- `shoot(stream=True, dng=True)` without `jpeg=True` through this library.
  The header-and-raw transfer it uses (`init_usb_capture(6)`) was run once on
  one body with a separate script.
- Any camera other than the A2500, and any CHDK build other than 1.5.1,
  including the 1.6.1 build `tools/flash_chdk.py` installs.
- Any viewer other than macOS.

Known limits:

- **Defective sensor pixels show in the DNG.** The camera hides them in its
  JPEGs; the raw data carries them, and this library does not correct them or
  list them in the file.
- The byte-pair swap is applied to every camera. It is established on the
  A2500 only.

## Tests

`.venv/bin/python -m pytest tests/ -q`: 230 passed before, **266** after.
New: the capture loop against a fake camera that offers the parts in the order
and with the ready-mask behaviour seen on the A2500; assembly against a real
A2500 header; each refusal; the thumbnail.

## Releasing this

1. Merge this branch.
2. Tag `v0.1.4` on the merged tree.
3. Bump the backend's pin in all three places together: `requirements.txt`,
   `pixi.toml` and `pixi.lock`. The backend's RAW+JPEG capture for CHDK bodies
   (NEH-344) lands in the same change, since it refuses RAW+JPEG on an older
   pychdk.
