# Stage 2 — Visualization (2.1–2.3 built, 2.4 auto-level = design for approval)

## Built (2026-11-13)

| # | Feature | Where |
|---|---|---|
| 2.1 | 3D view: rotate / zoom (mouse), Top / Front / Side / Iso / Fit, **Z scale** ×1–×50, colours by depth (yellow shallow → red deep, blue = feed in air, grey = rapid, green = done) | `ui/view3d.py` |
| 2.1 | **View quality** (View menu): *Standard* (antialiased, rapids shown), *Light* (thin lines, no rapids, fewer points — built-in graphics / big files), *2D only* (no OpenGL at all) | `settings.view_quality` |
| 2.2 | G-code viewer with line numbers; click a line ↔ highlighted on the path; click the path (2D) ↔ line in the text | `ui/gcode_view.py` |
| 2.2 | **Checks** before running: rapid sideways below the surface, rapid plunge, cutting with spindle off, deeper than 2 mm, feed above machine max, outside work area. Problems are listed, marked in the G-code, and the Run dialog warns | `toolpath.check_program` |
| 2.3 | **Simulate** (playback): 1×–500×, slider, jump to selected line. Uses the real planner tick stream, so time and corners match the machine | `toolpath.Timeline` |
| 2.3 | Live view during a job: tool + done path follow the executed ticks; current line highlighted; magenta ✕ where the job paused / stopped | `ui/main_window.py` |

Surface is assumed at work Z = 0 for colours and checks.

Performance (100 k-line FlatCAM-style file, 2.4 h job): load ≈ 4.7 s,
playback ≈ 20–35 ms per frame in all three modes.

## 2.4 Auto-level (height map) — built (2026-11-13), hardware test pending

### Findings from the original software (reverse-engineered)
- Probing uses `0x3F` packets (same as jog, ramp up to 14 steps/tick).
  The machine stops on copper contact and answers `04 hi lo`.
- `f.d.a(int)` handles the reply: `diff = (first value) − value`, stored per
  X/Y point (`"x,y" → diff`); the first probed point is the reference.
  Then the Z position is set from it and it retracts (`G00 Z2.0` when ≥ 2 mm).
- **The value is the machine's own absolute Z step counter** (signed
  16-bit). Proof from capture 1: after a touch (−3670) the operator jogged
  Z− several times (machine ACKed `01` but did not move — it refuses Z−
  while touching), went up 494 steps and probed again → −3668. Our PC
  count differed by >3000 steps, the machine counter by 2.
  ⇒ height differences between points are exact, independent of the PC.
- A touch while idle answers the keep-alive with `04` (capture 1, value
  1163): useful for a wiring test, nothing moved.
- Failure message: "No touch detected or spindle was too high".

### How MicroPCB does it
- **Probe** (`controller.probe`): `3F` packets of 3 ticks at 1–6
  steps/tick (default 3 ≈ 37 mm/min), max 2 packets queued; on touch the
  queue is dropped.
  - First touch of a connection: PC Z = count − (middle of the packet in
    flight) → offset to the machine counter (≤ ±0.02 mm, cancels because
    work Z 0 is set at that touch).
  - Later touches: PC Z = machine counter + offset → exact. Checked
    against the PC count; a mismatch stops with an error (this is also
    the hardware test of the counter theory).
- **Grid**: job size (+ margin) / spacing, serpentine order. First point
  becomes work Z 0. Travel height between points = highest point so far
  + clearance. Probe travel = down to lowest point so far − 1 mm.
- **Apply**: feed moves split into ≤ 1 mm pieces, Z + bilinear height.
  Preview, checks, simulate, run all use the corrected path.
- **Save / load** map (`.hmap.json`). After a tool change: "Probe + set
  Z 0" on the first grid point, then run with the same map.
- Idle touch (keep-alive `04`) no longer marks the position as lost.
- The simulator has a virtual board 3 mm below its start position
  (tilted + warped) so the whole flow can be tried without the machine.

### Hardware test (you)
1. Auto-level tab → **Test touch**, touch the clip to the tool → "✓".
2. Tool ~2 mm above copper → **Probe here** twice (jog up 1 mm between):
   both results must agree within ±0.01 mm.
3. Small grid (e.g. 3 × 3, 10 mm) on a blank board; compare the range with
   a feeler gauge / dial if possible.
4. Air-cut with the map applied, then a real isolation cut.
