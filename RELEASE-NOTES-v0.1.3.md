# pychdk 0.1.3

A release about sentences. Most of what changed here is a claim the library
made that was broader than what the code did: docstrings, error messages and
README lines that told a reader something we had not established. Three
findings needed real code changes; the rest needed the truth written down.

**None of this is verified against hardware.** Every correction below is
reasoned from reading CHDK's own sources — `modules/luascript.c`,
`lib/ubasic/ubasic.c`, `core/live_view.{c,h}`, `core/shooting.c`,
`core/remotecap.c`, `core/ptp.h` — and from reading this library. That is
exactly how the original errors got in. A green test suite here means "the
code now agrees with what we believe CHDK does", not "we watched a camera do
it". Everything below is a bench item.

## Breaking changes

Pre-1.0, with Captua as the only consumer. There is no compatibility shim and
no deprecation period, and the version stays `0.1.3` rather than pretending
otherwise.

- **`ChdkDevice.shoot(market_iso=...)` is now `shoot(real_iso=...)`.** The
  value was never treated as market ISO. `iso_to_sv96` computes
  `96 * log2(ISO / 3.125)`, the APEX96 conversion for *real* sensitivity, and
  `set_sv96` wants real sv96 as well. Market ISO — the number in the camera's
  own ISO menu — is a different quantity, and the conversion between them is
  per-camera, so this library does not do it. The parameter now says which one
  it takes. **Callers must check whether the number they were passing was a
  market number**; renaming the keyword makes the call fail loudly instead of
  silently mis-exposing. Captua's `backend/capture/backends/chdk_backend.py`
  passes `market_iso=` and will need updating with the pointer bump.
- **`shoot(download_after=...)` and `shoot(remove_after=...)` are gone.**
  Neither was implemented: `download_after` listed `A/DCIM`, discarded the
  listing and returned `None`; `remove_after` did nothing at all. They were
  removed rather than implemented, so the documentation is true now. Whether
  the card path ever needs building is a bench decision.
- **`ChdkDevice.switch_mode` now raises.** A switch that is never confirmed
  raises `RuntimeError` naming the mode asked for and what `get_mode()` last
  reported, instead of returning exactly as a success did. A poll that comes
  back with *no* answer — `execute_lua_wait` returns `None` when a script ends
  with no RET message — is now retried rather than read as a falsy
  `is_record`. The first version of this fix used a plain `bool()`, and since
  `bool(None)` is `False`, a camera that said nothing at all would have been
  confirmed as being in play mode. Caught in the review of this release, not
  on a camera.
- **`util.iso_to_sv96`'s parameter is renamed** `iso` to `real_iso`. Only
  matters to a caller passing it by keyword.

## Corrected behaviour

- **`switch_mode` confirmed the opposite of what it set.** The polling code
  carried the comment "get_mode() returns 0 (falsy) for record, nonzero for
  play" and inverted the answer accordingly. That describes **uBASIC**'s
  `get_mode` (`lib/ubasic/ubasic.c`: 0 record, 1 play, 2 video record). The
  call is **Lua**, and CHDK's Lua `get_mode()` returns three values —
  `is_record, is_video, mode` — where `is_record` is `!mode_play` and so is
  *true* in record mode (`luaCB_get_mode`, `modules/luascript.c`). Since
  `execute_lua_wait` returns only the first RET value, the library was reading
  `is_record` and negating it. The inversion survived three releases with no
  visible symptom precisely because the loop fell through and returned on
  failure; it now raises, and the comment names the language and all three
  return values.
- **`get_frames` requested no pixel data.** It called `get_display_data()`
  with the default `flags=0`. CHDK's `live_view_get_data` adds each data block
  only if the matching `LV_TFR_*` bit was requested (`core/live_view.c`), so a
  flagless request returns the header and framebuffer descriptions and nothing
  else — the generator could yield frames with no image in them.
  `get_frames` now defaults to `LV_TFR_VIEWPORT` and takes `flags` as an
  argument. The `LV_TFR_*` constants from `core/live_view.h` are now named in
  `pychdk.chdk`. Captua was unaffected because it calls
  `get_display_data(LV_TFR_VIEWPORT)` directly rather than using this
  generator; that was luck.

## Corrected claims

- **`close()` claimed nothing in the library shares a device.** It does.
  `_cleanup_all` closes every open device from the main thread, and it runs
  from the SIGINT/SIGTERM handler, which can close a device a `MultiCam`
  worker is inside `shoot()` on. The `atexit` hook is *not* the same hazard:
  `concurrent.futures` registers its pool shutdown through
  `threading._register_atexit`, which joins the worker threads during
  `threading._shutdown`, and that runs before `atexit` handlers — so a worker
  has finished before `_cleanup_all` runs at exit. A daemon thread belonging
  to the host is not joined that way, but that is the host's thread. The
  docstring now draws that line.

  **No lock was added and the teardown path is unchanged**: a plain lock is
  hazardous here, because `close()` runs from a signal handler and at
  interpreter shutdown, where a lock held by a thread being torn down turns a
  clean exit into a hang. The design question is open.
- **`drain_messages` said "drain all pending messages".** It reads at most
  fifty and reports nothing about what remains. Documented as what it is: a
  bounded attempt, up to fifty, with no indication whether the queue was
  emptied.
- **`REMOTE_CAP_NOTSET` was described as "an initialization that never
  happened".** CHDK cancels a remote capture on its own download timeout and
  reports it identically — "following a timeout, RemoteCaptureIsReady and
  RemoteCaptureGetData will behave as if remote capture were not initialized"
  (`set_remotecap_timeout`, `modules/luascript.c`; the mechanism is
  `remotecap_reset` in `core/remotecap.c`, also reached on a transfer error).
  The docstring and the matching `RuntimeError` in `device.py` now say what
  the status establishes: remote capture is not initialised *now*, for at
  least two reasons it cannot distinguish.
- **`MultiCam.shoot`'s docstring promised "List of image data bytes (one per
  camera)".** With the card path that was a list of `None`s. It now states
  what each path returns. The first rewrite then overshot in the other
  direction, saying the library "has no path that fetches a card image":
  `download_file` does exactly that, by path. What is missing is discovering
  the path the shot just taken was written to, and that is what the docstrings
  now say.
- **README, A2500 remote capture — rewritten twice.** The old sentence said
  the library "falls back to SD card capture (`shoot()`) which triggers the
  shutter but stores images on the card". It does not fall back at all. The
  replacement written for this release then claimed that `init_usb_capture`
  had already run by the time the failure surfaced, so remote capture was
  still enabled on the camera — which is itself more than the exception
  establishes: the failure can come from the script submission, before
  initialization ran at all, or from a capture that initialized and was then
  cancelled on CHDK's own timeout. The README now says only what is
  supported: there is no automatic fallback, and the exception establishes
  neither what state the camera is in nor whether an ordinary `shoot()` would
  recover the picture.
- **README, "Check CHDK version".** `get_version()` returns the version of the
  CHDK **PTP protocol** the camera speaks (`PTP_CHDK_VERSION_MAJOR/MINOR`,
  `core/ptp.h`), not the camera's firmware or the CHDK build on the card.
  There is no dedicated method for those, but the build is reachable through
  CHDK's own Lua — `lua_execute("return get_buildinfo()")`, which
  `examples/test_camera.py` already calls. Relabelled, and the README now
  points at `get_buildinfo()` instead of saying no such call exists.
- **`examples/test_two_cameras.py`** printed "Shots taken (saved to SD cards)"
  immediately after submitting a script with `do_return=False`, which it never
  waits on. It now prints what is actually known at that point.
  `examples/test_camera.py` got the same treatment and dropped its
  `download_after=` argument.

## Data-safety fix

- **`tools/flash_chdk.py` could erase a disk the operator never confirmed.**
  With one removable disk, `pick_disk` printed it and asked "This will ERASE
  the disk. Continue? [y/N]". With two or more it printed the list, took an
  index, and returned the chosen disk *before reaching the confirmation* — so
  the multi-disk path, the one where picking wrong is most likely, was the one
  path with no confirmation at all. Selection and confirmation are now
  separate steps and the confirmation is asked on every path. The prompt names
  the specific disk — device node, media name, size — rather than "the disk",
  because `find_removable_disks` accepts any physical removable medium, which
  on a Mac includes an external USB drive that is not an SD card. A negative
  index is now rejected rather than counted from the end of the list.

  This is the only finding in this release that can destroy someone's data.

## Tests

`.venv/bin/python -m pytest tests/ -q` — 205 passed before, 225 after. New
coverage: the `switch_mode` polarity (written from CHDK's sources so that it
fails under either inversion, not from what the implementation expects), the
raise on an unconfirmed switch, the live-view transfer flags, the removed
`shoot` keywords, and six cases around the flasher's confirmation prompt.

## A note on how this release was reviewed

Six further corrections in the lists above were found by adversarial review
*after* the first pass was written and its tests were green — five of them
sentences that still claimed more than the code or the cited source
established, and one a real fault (`bool(None)`) introduced by the fix for
the inverted polarity. That is the same failure mode this release exists to
correct, reproduced inside the correction. The tests were green throughout.
Prose has no tests; the only check on it is someone reading it against the
source, twice.
