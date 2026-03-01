"""Manual integration test -- two-camera coordinated capture.

Usage: python examples/test_two_cameras.py

Requires two Canon cameras with CHDK firmware connected via USB hub.
"""
import sys
import pychdk


def main():
    print("Searching for cameras...")
    devices = pychdk.list_devices()
    if len(devices) < 2:
        print(f"Found {len(devices)} camera(s), need 2. "
              "Connect both cameras and try again.")
        sys.exit(1)

    print(f"Found {len(devices)} cameras.")
    mc = pychdk.MultiCam()

    # Identify cameras
    for i, cam in enumerate(mc.cameras):
        major, minor = cam._chdk.get_version()
        result = cam.lua_execute("return 2 + 2")
        print(f"Camera {i}: serial={cam.info.serial_num}, "
              f"CHDK {major}.{minor}, 2+2={result}")

    # Switch to record mode
    print("Switching all cameras to record mode...")
    mc.prepare_all("record")

    # Coordinated capture
    print("Capturing from both cameras...")
    try:
        results = mc.shoot(stream=True)
        for i, data in enumerate(results):
            if data:
                fname = f"capture_cam{i}.jpg"
                with open(fname, "wb") as f:
                    f.write(data)
                print(f"  Camera {i}: saved {fname} ({len(data)} bytes)")
            else:
                print(f"  Camera {i}: no data")
    except Exception as e:
        print(f"Streaming capture failed: {e}")
        print("Trying non-streaming capture...")
        mc.execute_all("shoot()", do_return=False)
        print("Shots taken (saved to SD cards)")

    mc.close()
    print("Done.")


if __name__ == "__main__":
    main()
