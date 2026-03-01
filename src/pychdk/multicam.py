"""Multi-camera coordination for book scanning.

Discovers all connected CHDK cameras and provides methods for
coordinated capture across multiple devices.
"""
import concurrent.futures

from pychdk.device import ChdkDevice, list_devices


class MultiCam:
    """Manages multiple CHDK cameras for coordinated capture."""

    def __init__(self):
        devices = list_devices()
        if not devices:
            raise RuntimeError("No CHDK cameras found")
        self.cameras = []
        for info in devices:
            cam = ChdkDevice(info)
            self.cameras.append(cam)

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
        """Switch all cameras to the specified mode concurrently."""
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=len(self.cameras)
        ) as pool:
            futures = [
                pool.submit(cam.switch_mode, mode)
                for cam in self.cameras
            ]
            for f in futures:
                f.result()

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
