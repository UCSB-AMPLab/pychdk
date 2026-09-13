"""Multi-camera coordination for book scanning.

Discovers all connected CHDK cameras and provides methods for
coordinated capture across multiple devices.
"""
import concurrent.futures

from pychdk.device import ChdkDevice, list_devices


class MultiCam:
    """Manages multiple CHDK cameras for coordinated capture."""

    def __init__(self):
        """Open every camera found, or leave none of them open.

        A camera that fails to open partway down the list leaves the
        ones before it open and claimed, and the half-built MultiCam is
        discarded, so nothing is left holding them: not the caller, who
        never got an object, and not the cleanup registry, which tracks
        devices weakly. They stay claimed until the process ends.

        PTPDevice.open's guarantee does not reach this, because these
        cameras opened successfully. They are orphans rather than
        partial opens, so the rollback has to be here.
        """
        devices = list_devices()
        if not devices:
            raise RuntimeError("No CHDK cameras found")
        self.cameras = []
        try:
            for info in devices:
                self.cameras.append(ChdkDevice(info))
        except BaseException:
            self.close()
            raise

    def shoot(self, **kwargs):
        """Capture from all cameras concurrently.

        Args:
            **kwargs: Passed to each ChdkDevice.shoot().

        Returns:
            List of image data bytes (one per camera), in the
            same order as self.cameras.
        """
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(self.cameras)
        ) as pool:
            futures = [
                pool.submit(cam.shoot, **kwargs)
                for cam in self.cameras
            ]
            return [f.result() for f in futures]

    def prepare_all(self, mode="record"):
        """Switch all cameras to the specified mode sequentially.

        Sequential switching avoids USB contention that can cause
        cameras to miss mode-switch commands on a shared bus.
        """
        for cam in self.cameras:
            cam.switch_mode(mode)

    def execute_all(self, lua_code, **kwargs):
        """Execute Lua code on all cameras concurrently.

        Returns:
            List of results (one per camera).
        """
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(self.cameras)
        ) as pool:
            futures = [
                pool.submit(cam.lua_execute, lua_code, **kwargs)
                for cam in self.cameras
            ]
            return [f.result() for f in futures]

    def close(self):
        """Close all camera connections."""
        for cam in self.cameras:
            try:
                cam.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
