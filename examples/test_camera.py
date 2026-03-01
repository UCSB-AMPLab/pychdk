"""Manual integration test -- connect to a CHDK camera and exercise the API.

Usage: python examples/test_camera.py

Requires a Canon camera with CHDK firmware connected via USB.
"""
import sys
import pychdk


def main():
    print("Searching for CHDK cameras...")
    devices = pychdk.list_devices()
    if not devices:
        print("No cameras found. Is the camera connected and powered on?")
        sys.exit(1)

    print(f"Found {len(devices)} camera(s):")
    for i, info in enumerate(devices):
        print(f"  [{i}] vendor=0x{info.vendor_id:04x} "
              f"product=0x{info.product_id:04x} "
              f"bus={info.bus_num} addr={info.device_num} "
              f"serial={info.serial_num}")

    # Connect to first camera
    info = devices[0]
    print(f"\nConnecting to camera {info.serial_num}...")
    cam = pychdk.ChdkDevice(info)
    print("Connected.")

    # Check CHDK version
    major, minor = cam._chdk.get_version()
    print(f"CHDK PTP version: {major}.{minor}")

    # Run some Lua
    result = cam.lua_execute("return 2 + 2")
    print(f"2 + 2 = {result}")

    result = cam.lua_execute('return get_buildinfo()')
    print(f"Build info: {result}")

    # Switch to record mode
    print("Switching to record mode...")
    cam.switch_mode("record")
    print("In record mode.")

    # Capture
    print("Capturing image (streaming)...")
    try:
        data = cam.shoot(stream=True)
        if data:
            with open("test_capture.jpg", "wb") as f:
                f.write(data)
            print(f"Saved test_capture.jpg ({len(data)} bytes)")
        else:
            print("No image data received")
    except Exception as e:
        print(f"Streaming capture failed: {e}")
        print("Trying non-streaming capture...")
        cam.shoot(download_after=False)
        print("Shot taken (saved to SD card)")

    cam.close()
    print("Done.")


if __name__ == "__main__":
    main()
