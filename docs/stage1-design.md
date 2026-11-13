# Stage 1 Design — Basic Control (draft for approval)

Based on [protocol.md](protocol.md). No code until this is approved.

## 1. Goal
A reliable Windows app that can connect to the Wegstr, jog, set zero, switch
the spindle, and run a G-code file safely — replacing daily use of the
original software for simple jobs.

**Out of scope for Stage 1:** Gerber/Excellon, auto-level/height map,
3D preview (a simple 2D preview is in), camera, tool library.

## 2. Key fact that shapes the design
The machine only executes **raw step ticks** (≤ 14 steps/axis per 19.6 ms
tick). It has no G-code, no position memory, no homing. So **our app is the
motion controller**:
- interprets G-code,
- plans speed/acceleration,
- converts motion into step ticks,
- tracks machine position (open-loop, steps counted on the PC).

## 3. Technology
| Item | Choice |
|---|---|
| Language | Python 3.12 |
| UI | PySide6 (Qt 6) |
| Serial | pyserial |
| 2D preview | Qt QGraphicsView (pyqtgraph later for 3D) |
| Packaging | PyInstaller → single Windows .exe folder |
| Tests | pytest |

Cross-platform later with no rewrite.

## 4. Architecture

```
┌──────────── UI (Qt main thread) ────────────┐
│ MainWindow: DRO · Jog panel · Job panel ·   │
│ Preview · Console · Status bar · E-STOP     │
└───────────────▲───────────────┬─────────────┘
         signals│               │commands
┌───────────────┴───────────────▼─────────────┐
│ MachineController (state machine)           │
│  IDLE · JOGGING · RUNNING · PAUSED ·        │
│  STOPPING · ALARM · DISCONNECTED            │
└──────┬─────────────────▲────────────────────┘
       │ tick stream     │ events
┌──────▼──────┐   ┌──────┴──────────┐
│ MotionPlanner│  │ SerialLink      │ (own thread)
│ + StepGen    │─▶│ stop-and-wait,  │◀──▶ COM (CH340)
└──────▲──────┘   │ keep-alive,     │
       │          │ reply parser    │
┌──────┴──────┐   └─────────────────┘
│ GCodeParser │        ▲ swap for
└─────────────┘   SimulatedMachine (offline mode)
```

### Modules
| Module | Responsibility |
|---|---|
| `protocol.py` | Pure functions: encode tick/packet, encode `7F` settings, parse replies (byte-stream buffered). 100 % unit-tested against the capture. |
| `serial_link.py` | Port scan + handshake, open 9600 8N1, worker thread, send one packet → wait `01` (timeout 500 ms), keep-alive `7F perf` every 1 s when idle, emit events (ack, probe, overload, released, lost). |
| `sim_machine.py` | Fake machine with same interface (ACK after 19.6 ms/tick). Lets us develop/test without hardware. |
| `gcode.py` | Parse G-code → list of moves. |
| `planner.py` | Line/arc segments → velocity profile (trapezoid accel) → step ticks (Bresenham per axis, ≤ max steps/tick). |
| `controller.py` | State machine, position tracking, job streaming, pause/stop, safety rules. |
| `settings.py` | JSON settings in `%APPDATA%\WegstrPCB\`. |
| `ui/` | Windows and widgets. |

## 5. G-code support (Stage 1)
| Code | Support |
|---|---|
| G0, G1 | ✅ rapid = max speed, feed = F |
| G2, G3 | ✅ (I/J and R), split into short lines (chord error 0.005 mm) |
| G4 | ✅ dwell (send empty ticks) |
| G17 | ✅ (XY plane only) |
| G20/G21 | ✅ inch / mm |
| G90/G91 | ✅ absolute / relative |
| G92 | ✅ set work offset |
| M3/M5 | ✅ spindle on/off (bit in every tick) |
| M0/M1 | ✅ pause, wait for user |
| M2/M30 | ✅ end |
| S, T, M6 | S ignored (no speed control); M6 = pause with "change tool" prompt |
| others | listed as warnings on load, job can still run |

Pre-run check: show file bounds, estimated time, warn if outside machine
area or above max feed.

## 6. Motion planning
- **Units:** `mm_per_step = 10 / FINE_CORRECTION` per axis (default 0.004),
  configurable per axis + per-axis direction invert.
- **Speed:** steps/tick = feed (mm/s) × 0.0196 s / mm_per_step, capped at
  `max_steps_per_tick` (default 14, configurable).
  → max ≈ 170 mm/min at defaults (to be confirmed with ruler test).
- **Acceleration:** trapezoid ramp, default like original jog (3 → 14
  steps/tick over ~6 packets), configurable.
- **Junctions:** Stage 1 simple: decelerate to a safe corner speed between
  segments with sharp angles; straight continuations keep speed.
- **Fractional steps** carried between ticks so no position drift.
- Packets always 3 ticks; pad with zero ticks at the end.

## 7. Position & coordinates
- PC counts every step actually ACKed → **machine position** (MPos).
- **Work zero** per axis → work position (WPos) shown in DRO.
- No homing switches → on startup position = last saved position (saved on
  every stop/exit) with a "Position may be wrong — set zero" warning if the
  app crashed.
- Probe reply `04` value: logged and shown in Stage 1; used in Stage 3.

## 8. User interface (Stage 1)

```
┌───────────────────────────────────────────────────────────────┐
│ [COM5 ▼][Connect]  ● Connected · Idle          [  E-STOP  ]  │
├──────────────────────┬────────────────────────────────────────┤
│  DRO   Work   Machine│                                        │
│  X   12.345  52.345  │                                        │
│  Y    0.000  40.000  │           2D toolpath preview          │
│  Z   -0.100  -3.100  │      (job path, tool position,          │
│  [Zero X][Y][Z][All] │       work area, bounds)               │
│  [Go to zero]        │                                        │
├──────────────────────┤                                        │
│  JOG  step:0.01 0.1 1 10 │                                    │
│        [Y+]     [Z+]  │                                       │
│   [X-] [  ] [X+]      │                                       │
│        [Y-]     [Z-]  │                                       │
│  speed ▬▬▬▬●▬ 100 mm/min │                                    │
│  Spindle [ON/OFF]     ├────────────────────────────────────────┤
├──────────────────────┤ Job: board.nc  1243/5120 lines  24%    │
│  Console (toggle raw)│ [Open][Run][Pause][Stop]  ETA 12:40    │
└──────────────────────┴────────────────────────────────────────┘
```

- **Keyboard jog:** arrows = X/Y, PgUp/PgDn = Z; continuous while key held,
  step mode by click. Keyboard jog disabled while a job runs.
- **E-STOP:** big button + `Esc` key, always active.
- Languages: English first; text kept in a translation file for later.

## 9. Safety rules
1. **Stop = stop sending.** The machine has a ~1 packet buffer, so it
   halts within ≈ 60 ms. Spindle-off packet sent immediately after.
2. **Pause** = decelerate (ramp down), then stop sending; Resume ramps up.
3. Reply `02` overload or `03` released → immediate stop, spindle off,
   ALARM state, user must acknowledge.
   - `03` (release button) is a held state: show **"Motors released"**
     banner while it lasts, block jog/run, mark position **lost**
     (DRO greyed) until the user re-zeros.
4. No ACK within 500 ms or USB port vanishes (hardware E-stop cuts power)
   → "Connection lost / E-stop", ALARM, job stopped, position lost,
   auto-reconnect when the port reappears.
5. Soft limits from configured work area (default 0…? mm, to set per
   model); jog and job refuse to go outside.
6. Job start requires: connected, idle, zero set, file checked.
7. Spindle bit is part of every tick → spindle cannot be "left on" by a
   lost command; on stop we send zero ticks with spindle off.

## 10. Testing plan
- Unit tests: `protocol` encode/decode against every packet in the
  capture `1.txt`; planner (step totals equal target distance exactly).
- Simulator tests: run full jobs in offline mode.
- Hardware tests (you): ruler test 10 mm, jog each axis, spindle, E-stop,
  air-cut test G-code, compare time with original.

## 11. Milestones
| # | Deliverable | You test |
|---|---|---|
| 1.1 | `protocol` + `serial_link` + console app: connect, keep-alive, jog by command line | connect, jog |
| 1.2 | GUI: connect, DRO, jog, zero, spindle, E-stop | hand controls |
| 1.3 | G-code parser + planner + run/pause/stop + 2D preview | air-cut job |
| 1.4 | Settings dialog, persistence, installer build | daily use |

## 12. Decisions (FINAL — 2026-11-13)
| # | Decision |
|---|---|
| 1 | App name: **MicroPCB** (working name) |
| 2 | Work area **X 140 × Y 200 × Z 40 mm**; step 0.004 mm; repeatability 0.02 mm (soft limits use these) |
| 3 | Python 3.12 + PySide6 approved |
| 4 | mm/step adjustable per axis in Settings (default 0.004) |
| 5 | After motor release / E-stop mid-job: **restart only** in Stage 1 |

Settings dir: `%APPDATA%\MicroPCB\`.

**Hardware test of 1.1 (2026-11-13):** connect, jog, spindle, release all OK.
`y 10` measured 10 mm → 0.004 mm/step confirmed. Axis directions (operator
in front of machine; the **bed** moves in X/Y, spindle moves in Z):
- X+ : bed moves right → left (tool moves right relative to the work)
- Y+ : bed moves toward the operator (tool moves to the back)
- Z+ : spindle up, Z− : down

⇒ Standard CNC convention for tool-relative motion; no inversion needed.
GUI jog arrows are labelled by **tool** direction (→ = X+, ↑ = Y+), with
a tooltip explaining the bed moves the opposite way.

### Original questions (answered)
1. App name (working name: **WegstrPCB**)?
2. Machine model / work area (X×Y×Z mm) for soft limits?
3. OK with Python + PySide6?
4. Ruler test result for 0.004 mm/step (jog 10 mm in original, measure).
5. ~~E-stop / lid switch?~~ Answered: E-stop = total power cut; release
   button = reply `03` (handled in §9).
6. Can a job be **resumed** after a release (user re-aligns by hand), or
   always restart? (Proposal: Stage 1 = restart only.)
