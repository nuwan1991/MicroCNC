# MicroPCB

Control software for Wegstr CNC machines, focused on PCB milling.
Status: **0.2.0** — bridge records timed serial captures + report (docs/bridge-guide.md §7); **Stage 4** — fixed bed camera: calibration, board alignment from holes / pads,
obstacle warnings, PCB / toolpath overlay on the live picture; Stage 3: PCB import (Gerber / Excellon from KiCad, EasyEDA), isolation
routing, copper clearing, drilling, cutout with tabs, tool changes, double-sided
boards with alignment pins; on top of Stage 2 (3D view, G-code viewer, checks, playback, auto-level) and
Stage 1 (protocol, serial link, simulator, GUI, G-code jobs).

Docs: [protocol](docs/protocol.md) · [Stage 1 design](docs/stage1-design.md) ·
[Stage 2](docs/stage2-design.md) · [Stage 3](docs/stage3-design.md) · [Stage 4](docs/stage4-design.md) ·
[capture guide](docs/capture-guide.md)

## Setup (once)
```
python -m venv .venv
.venv\Scripts\python -m pip install -e .[dev]     # also after updates (new: numpy, pyqtgraph, PyOpenGL)
```

## Run
GUI:
```
.venv\Scripts\python -m micropcb
```
MicroPCB CAM - Gerber / drill to G-code, no machine needed
(see [split design](docs/split-design.md)):
```
.venv\Scripts\python -m micropcb.apps.cam
```
Windows installer (MicroPCB + MicroPCB CAM, Start menu shortcuts, uninstaller) -
needs PyInstaller in the .venv and Inno Setup 6:
```
powershell -ExecutionPolicy Bypass -File packaging\build_installer.ps1
```
Output: `dist\installer\MicroPCB-Setup-<version>.exe`. Check a build without opening a
window: `MicroPCB.exe --selftest result.txt` (writes "ok …" or the error).

Bridge for the original Wegstr software (auto-level fix / simulated machine),
see [bridge guide](docs/bridge-guide.md):
```
.venv\Scripts\python -m micropcb.bridge
```
Command-line tool:
```
.venv\Scripts\python -m micropcb.cli ports          # list COM ports
.venv\Scripts\python -m micropcb.cli run --sim      # simulated machine
.venv\Scripts\python -m micropcb.cli run            # auto-detect Wegstr
.venv\Scripts\python -m micropcb.cli run --port COM5 --log
```
Close the original Wegstr software first (only one program can use the port).

## Tests
```
.venv\Scripts\python -m pytest -q
```

## Layout
| Path | |
|---|---|
| `src/micropcb/protocol.py` | packet encode/decode, reply parser |
| `src/micropcb/serial_link.py` | port detection, stop-and-wait worker, keep-alive, position counting |
| `src/micropcb/sim_machine.py` | simulated machine for offline use / tests |
| `src/micropcb/jog.py` | jog moves with accel ramp |
| `src/micropcb/cli.py` | interactive jog tool |
| `src/micropcb/gcode.py` | G-code parser (arcs split into lines) |
| `src/micropcb/planner.py` | speed planning, corners, accel ramp -> step ticks |
| `src/micropcb/job.py` | job thread: streaming, pause / resume / stop |
| `src/micropcb/controller.py` | position, zero, soft limits, jog, jobs |
| `src/micropcb/toolpath.py` | toolpath arrays, playback timeline, job checks |
| `src/micropcb/autolevel.py` | probe grid, height map, toolpath correction |
| `src/micropcb/pcb/` | Gerber + Excellon readers, board placement, CAM, tool library |
| `src/micropcb/camera/` | USB camera, bed calibration, alignment, obstacles |
| `src/micropcb/ui/` | main window, 2D/3D views, G-code viewer, settings |
| `src/micropcb/bridge/` | bridge for the original Wegstr software |
| `examples/aircut-test.nc` | air-cut test job for milestone 1.3 |
