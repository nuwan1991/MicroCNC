# Split into MicroPCB CAM + MicroPCB Control (design draft)

Status: **design only – nothing built yet.** Written 2026-11-15 for MicroPCB 0.2.2.

## 1. Goal

Today one window does everything: Gerber import, toolpaths, checks, machine
control, auto-level, camera. Split it into two programs that share one code
base and one installer:

| | **MicroPCB CAM** | **MicroPCB Control** |
|---|---|---|
| Input | Gerber (RS-274X) + Excellon drill, or a ZIP | G-code (`.nc`), from MicroPCB CAM or any other CAM |
| Does | Board placement, isolation / clearing / drilling / hole milling / cutout / pins, tool library, CAM checks, 2D/3D preview | Connect, jog, zero, probe, auto-level, camera alignment + obstacles, run / pause / stop, recording |
| Output | `.nc` per operation + `board.nc.json` side file | Engraved board |
| Needs the machine | No – any PC | Yes |

Reasons (agreed in chat 2026-11-15):
- Each window gets simpler; Control is too crowded now.
- CAM work on any PC; only the `.nc` (+ side file) goes to the machine PC.
- Control runs G-code from FlatCAM, KiCad plugins, or non-PCB jobs.
- A Gerber change cannot break machine control and the other way round.

Non-goals: no new CAM features, no change to the machine protocol, no change to
how the machine moves. MicroPCB Bridge stays as it is.

## 2. One package, three programs

Same repository, same `micropcb` package, same `pyproject.toml`:

```
micropcb-cam      -> micropcb.apps.cam:main       (new)
micropcb-control  -> micropcb.apps.control:main   (new)
micropcb-bridge   -> micropcb.bridge.app:main     (exists)
micropcb          -> all-in-one window, kept until phase 4 (section 8)
```

One installer puts all three in the Start menu. They share `Settings`
(Documents\MicroPCB), so the tool library and mm-per-step stay in one place.

### Who owns which module

| Module | CAM | Control | Notes |
|---|---|---|---|
| `pcb/` (gerber, excellon, board, cam, tools) | ✔ | – | Control must not import it |
| `gcode.py`, `planner.py`, `toolpath.py` | ✔ preview / checks | ✔ run / checks | shared core |
| `jobfile.py` (**new**: side file read/write) | writes | reads | section 4 |
| `controller.py`, `serial_link.py`, `protocol.py`, `job.py`, `jog.py`, `sim_machine.py` | – | ✔ | |
| `autolevel.py` | – | ✔ | |
| `camera/` | – | ✔ | `align.board_features` gets its input from the side file instead of a `PlacedBoard` |
| `bridge/` (recorder, analyze) | – | ✔ | |
| `ui/pcb_panel.py`, `ui/pcb_view.py`, `ui/cam_panel.py` | ✔ | – | |
| `ui/autolevel_panel.py`, `ui/camera_*.py`, jog / console parts of `main_window.py` | – | ✔ | |
| `ui/preview.py`, `ui/view3d.py`, `ui/gcode_view.py` | ✔ | ✔ | shared widgets |
| `settings.py`, `ui/settings_dialog.py` | ✔ tool / CAM pages | ✔ machine pages | one file, two dialogs |

A test (`test_imports.py`) checks that nothing under the Control program imports
`micropcb.pcb` and that CAM does not import `serial_link` / `controller`.

## 3. Changes that are not just moving code

1. **Camera alignment needs pads and holes.** Today `board_features()` reads
   them from the loaded Gerber. Plain G-code has none. → side file (section 4).
   Without a side file Control falls back to **drill points found in the
   G-code** (plunges deeper than 0.5 mm, grouped within 0.05 mm).
2. **Board preview in Control** (copper drawn under the toolpath) comes from the
   side file outline + pad polygons. Without it: toolpath only, as for any G-code.
3. **Checks are split:**
   - CAM: gaps / traces narrower than the tool, not-isolated nets, tool width vs.
     depth, outline, drill sizes.
   - Control: rapid plunge into material, depth against the **applied height
     map**, outside soft limits, outside the height map, outside the camera's
     obstacle-free area. These need the machine or the map.
4. **Work zero.** The `.nc` is in board coordinates with work zero at the board's
   front-left corner (what the CAM placement already does). The side file states
   it explicitly so Control can show "Zero = front-left corner of the board".

## 4. Side file `board.nc.json`

Written by CAM next to every `.nc` (same name + `.json`). Optional for Control. Example (comments are explanations only; the real file is plain JSON):
Units mm, work coordinates (same as the G-code). JSON, small (tens of kB).

```jsonc
{
  "format": "micropcb-job",
  "version": 1,
  "created": "2026-11-15 10:00:00",
  "app": "MicroPCB CAM 0.3.0",
  "source": ["board-F_Cu.gbr", "board.drl"],
  "operation": "isolation",            // isolation | clearing | drilling | holes | cutout | pins
  "side": "top",                       // top | bottom (bottom = mirrored)
  "mirror_x_mm": null,                 // mirror axis used for the bottom side
  "zero": "board front-left",
  "board": {"size": [42.0, 25.0], "outline": [[0, 0], [42, 0], [42, 25], [0, 25]]},
  "tool": {"name": "V-bit 20°", "kind": "vbit", "tip_mm": 0.1, "angle_deg": 20,
            "depth_mm": 0.04, "cut_width_mm": 0.114},
  "features": [                        // for camera alignment
    {"kind": "hole", "x": 3.0, "y": 4.0, "d": 0.8},
    {"kind": "pad",  "x": 10.2, "y": 5.1, "size": 1.5}
  ],
  "copper": [[[x, y], ...], ...],      // pad / track polygons for the preview, may be simplified
  "expected": {"time_s": 655, "cut_area": [0.0, 0.0, 37.0, 36.5]}
}
```

Rules:
- Control ignores unknown keys; `version` changes only for breaking changes.
- If the `.nc` is newer than the side file, or its size / hash differs from
  `"nc_sha1"` (added by CAM), Control says "side file is out of date" and uses
  the G-code only.
- `copper` is limited to ~20 000 points (simplified to 0.01 mm); features are
  always complete.

## 5. MicroPCB CAM window

- Left: files and layers (today's PCB panel); placement (rotate, mirror, offset).
- Middle: board view / 2D toolpath / 3D preview.
- Right: operations (today's CAM panel) and tool library.
- Bottom: Checks.
- **Export…** writes one `.nc` + side file per operation, or all into one folder.
- **Send to Control**: saves, then opens the file in a running Control window
  (local socket), or starts Control with the file.
- No serial port, no machine settings.

## 6. MicroPCB Control window

- Top: connection, machine position, E-stop / release status.
- Left: jog, zero, probe / auto-level, camera.
- Middle: 2D / 3D / Camera views of the job (with copper under it when the side
  file exists).
- Right: job (open, run, pause, stop, timer), Checks (machine part).
- Bottom: Console with Record / Analyze / Folder.
- **Open job…** takes any `.nc`; shows "with board data" or "G-code only".
- Recent jobs list; reopens the height map / alignment that belonged to the job
  only after asking.

## 7. Double-sided boards (works across the two programs)

1. CAM exports `top-isolation.nc`, `bottom-isolation.nc`, `drill.nc` (+ side files
   with `side` and `mirror_x_mm`).
2. Control: top side, drill.
3. Flip the board. Control loads the bottom file, the camera finds the drilled
   holes from the side file features and computes the alignment (Stage 4.3).

## 8. Migration – phases

| Phase | Work | Result |
|---|---|---|
| 1 | `jobfile.py` (side file read / write + tests); CAM panel "Export" also writes it; all-in-one window reads it | format proven, nothing removed |
| 2 | `apps/cam.py`: new window from `pcb_panel`, `pcb_view`, `cam_panel`, preview; import test | CAM app runs on its own |
| 3 | `apps/control.py`: new window from `main_window` without PCB tabs; camera alignment from side file / G-code drill points | Control app runs on its own |
| 4 | Real-machine test of CAM → Control (single and double side, auto-level, camera, recording). Then the all-in-one `micropcb` command starts Control and offers CAM | old window retired |
| 5 | Installer with three shortcuts (milestone 1.4) | release |

Each phase is its own commit series, all tests passing, the all-in-one window
working until phase 4.

## 9. Tests

- Side file: round trip, unknown keys ignored, out-of-date detection, size limit.
- CAM export → Control open gives identical moves to today's single window
  (same board, same tools: compare planned ticks).
- Alignment from side file features equals alignment from the Gerber today
  (simulated camera, `SimScene`).
- Drill-point fallback finds the holes of `drill.nc` from the samples.
- Import boundaries (section 2).
- Smoke tests: both windows open offscreen.

## 10. Open questions (for the user)

1. Names: "MicroPCB CAM" and "MicroPCB Control" – OK?
2. Should Control still be able to open Gerber files directly (calls CAM in the
   background), or G-code only?
3. One `.nc` per operation (current behaviour) or one combined file with tool
   changes as the default export?
4. Keep the all-in-one window permanently as a third option, or retire it in
   phase 4?
