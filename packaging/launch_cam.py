"""MicroPCB-CAM.exe - Gerber / drill to G-code (PyInstaller entry point)."""
import sys

from micropcb.apps.cam import main

if __name__ == "__main__":
    sys.exit(main())
