# MicroCNC user guide

MicroCNC controls Wegstr CNC machines for PCB milling: jogging, work zero,
auto-level (height map), and running G-code jobs. Safety features are listed in
**Help ▸ Safety features**.

## The window

| Area | What it holds |
|---|---|
| Top row | Program name and version, machine status, jog speed, project, soft limits, recording |
| Menu row | File · View · Settings · Help, and **E-STOP** (also the **Esc** key) |
| Left column | Port + Connect, Position, Jog, Spindle, Job (summary + Start / Pause / Stop), Layers |
| View row | 2D · 3D · PCB · Camera, and view tools (Fit, Top / Iso in 3D, Map overlay, ▾ Panel, ⛶) |
| Views | The job, with the simulation bar under it |
| Bottom panel | Console, G-code, Checks, Auto-level, PCB, CAM (F6 shows / hides it) |

## Quick start

1. **Connect.** Close the original Wegstr software, pick the port, press **Connect**.
2. **Machine home (once).** Move X and Y to their minimum and Z to the top, then
   **Settings ▸ Set machine home here…**. This switches the soft limits on.
3. **Work zero.** Jog to the board's front-left corner and press **0** next to X
   and Y. For Z, use **Probe + set Z 0** in the Auto-level tab (probe clip on the
   copper), or jog down and press the Z **0**. The **Zero all** button zeroes all three.
4. **Add the job.** **File ▸ Add G-code layer(s)…** (Ctrl+L). Add every file of the
   board: top isolation, drill, alignment holes, bottom isolation, cutout. Each
   becomes a layer; the type and side are guessed from the file name and can be
   changed under the list.
5. **Check.** Look at the Checks tab and the job summary. Use **▶ Simulate** to
   play the job in the views (the machine does not move).
6. **Auto-level** (recommended for isolation). See below.
7. **Run.** Select the layer and press **▶ Start**. Remove the probe clip first.

## Jog

- **Step (mm):** 0.004 (one motor step) · 0.01 · 0.1 · 1 · 5 · 10.
- **Speed:** 0–100 % of the machine's maximum; the label shows mm/min.
- **⌨ Keys:** hold the arrow keys (X/Y) and PgUp / PgDn (Z); release to stop.
- **⌂ Go to work zero:** Z up to safe height, X/Y to 0, then Z down to 0.
- **⤒ Z safe height:** lifts Z to the safe height from Settings.
- **Go to:** type work X, Y and / or Z (an empty box = that axis stays) and press
  **Go to** or Enter. Z first goes up to the safe height, X/Y move, Z goes to its
  value last. A Z below 0 asks first.
- With the tool low, an X/Y jog asks first (switch in Settings).

## Spindle

The lamp and the word (**Running** / **Stopped**) show the state. The switch is the
action: slide right to start, left to stop.

## Layers and projects

- Tick a layer to show it (other layers are drawn faint in the 2D view); click it to
  select it. **Start runs the selected layer.**
- Each layer has its own **Type**, **Side**, **Origin** and **Max cut depth**. Max
  cut depth is on by default for isolation layers and off for drill, alignment and
  cutout layers.
- The map column shows the height map of the layer's side: ✓ applied, ✗ none,
  "off" measured but not applied.
- **File ▸ Save project** (Ctrl+S) stores the layers, their options and both height
  maps in one `.mcnc.json` file. **File ▸ Open project** (Ctrl+O) brings them back.
  After opening, set Z 0 again before applying a stored height map.

## Auto-level

1. **Probe test:** clip on the copper, spindle off. **Test touch…** checks the
   wiring; **Probe + set Z 0** makes the copper work Z 0.
2. **Grid:** choose **Map for side** (top / bottom), press **Use job size**, set the
   **Point spacing** and **Edge margin**. **Z lift between points** is how far the
   tool rises before moving to the next point; **First probe max depth** is how far
   the first probe may go down. The points to be probed show in the 2D view **while
   you type** (○ grid points, ● extra points near small gaps, small grey dots only if
   the board bends there).
3. **Start auto-level.** When it finishes, the map is applied and saved
   automatically (Settings). The layers of that side use it.

## Run part of a job

Move the simulation bar to where the job should start and press **⟦ Start here**;
move it to the end and press **End here ⟧**. **✕ Whole job** runs everything again.
The part starts and ends at the safe height.

## Pause, resume and stop

**⏸ Pause** stops at the next move; **▶ Resume** continues (via safe height when
paused in the air, restarting the spindle first if it was switched off).
**■ Stop** ends the job. **E-STOP** / **Esc** stops all motion and the spindle.

## Updates

**Help ▸ Check for updates…** compares this version with the newest one on
www.microchip.lk/MicroCNC and opens the download page. Nothing is installed
automatically. "Check at startup" can be switched off in the update window.

## Support

Microchip Solutions (Pvt) Ltd — www.microchip.lk/microcnc.
**Help ▸ Copy system info** puts the version details on the clipboard for a report.
