# MicroPCB Bridge — setup and use

The bridge sits between the **original Wegstr software** and the machine:

```
Wegstr software ⇄ COM20 ═(com0com virtual cable)═ COM21 ⇄ MicroPCB Bridge ⇄ ┬ COM5 real machine (HW-597B)
                                                                         └ simulated machine
```

- **Bridge mode:** passes everything through and joins the 3-byte probe
  reply into one piece, which fixes the auto-level freeze with the HW-597B
  adapter.
- **Simulator mode:** no machine needed. The Wegstr software thinks a real
  machine is connected.

## 1. Install com0com (once)

com0com is a free driver that creates pairs of linked virtual COM ports.

1. Download the **signed** version 3.0.0.0 from the com0com project on
   SourceForge: file `com0com-3.0.0.0-i386-and-x64-signed.zip`.
   Unsigned versions will not load on Windows 10/11.
2. Unzip it and run `setup_com0com_W7_x64_signed.exe` **as Administrator**.
   - In the component list, untick *"CNCA0 <-> CNCB0"*. We will create our
     own pair with proper COM names.
3. Open **Start → com0com → Setup Command Prompt** (as Administrator) and
   type:
   ```
   install PortName=COM20,EmuBR=yes PortName=COM21,EmuBR=yes
   ```
   Pick two numbers that are not in use (check Device Manager → Ports).
   `EmuBR=yes` makes the virtual cable behave like a real 9600-baud line.
4. If Windows asks to install driver software, allow it.
5. Device Manager → *Ports (COM & LPT)* (or *com0com - serial port
   emulators*) should now show **COM20** and **COM21**.

If the driver is refused (Secure Boot / driver signature error), tell me
the exact message and we will use another virtual-port driver.

## 2. Start the bridge

```
.venv\Scripts\python -m micropcb.bridge
```

1. **Mode:** *Bridge to real machine* or *Simulated machine*.
2. **Bridge side virtual port:** **COM21**.
3. **Machine port:** the HW-597B (CH340) port, e.g. COM5. This is only
   needed in bridge mode.
4. Click **Start**. Status turns green: *Running*.

## 3. Start the Wegstr software

- Start it as usual. When it searches for the machine it should find it
  on **COM20**.
- If it does not, select COM20 manually in its connection settings.
- Leave the bridge window open the whole time.

## 4. What you see in the bridge window

| Item | Meaning |
|---|---|
| Packets / Replies | Traffic counters |
| Probe touches | Number of `04` probe replies |
| **Re-assembled** | Probe replies that arrived split and were joined. Each one is a freeze avoided. |
| Show packets | Live hex log (tick *incl. keep-alive* to also see `7F 04`) |

## 5. Simulator controls

| Control | Effect |
|---|---|
| Position X/Y/Z | Where the simulated machine is (0.004 mm/step) |
| Probe surface enabled | Probe touches happen when Z goes down to the surface |
| Surface Z at start position | Surface height relative to the position where the simulation started |
| Tilt X / Tilt Y | Board slope in mm per 100 mm, to test auto-level correction |
| Centre bulge | Board warp: centre higher (+) or lower (−) |
| Release button | Machine answers `03` (motors released) |
| Spindle overload | Machine answers `02` |
| Power cut | Machine stops answering (like the hardware E-stop) |
| Reset position | Sets the simulated position back to 0,0,0 |

Changes to the surface settings take effect immediately, even while running.

## Notes

- Only one program can open a COM port. Close the bridge before using
  MicroPCB directly on the real machine.
- MicroPCB itself does **not** need the bridge.
- The bridge adds about 1 ms per reply. The original software waits for
  each reply anyway, so you will not notice it.

## 6. Testing MicroPCB auto-level (2.4) through the bridge

MicroPCB can also connect through the bridge. The bridge window then shows
every packet and probe reply, which is handy while testing auto-level.

```
MicroPCB ⇄ COM20 ═(com0com)═ COM21 ⇄ MicroPCB Bridge ⇄ ┬ simulated machine (step A)
                                                     └ real machine (step B)
```

### A. Simulated machine (no hardware)
1. Start the bridge: `.venv\Scripts\python -m micropcb.bridge`
   - Mode **Simulated machine**, virtual port **COM21**.
   - Surface Z at start: **−3.000**; try Tilt X **0.5**, Centre bulge **0.1**.
   - Tick **Show packets** to watch the traffic. Click **Start**.
2. Start MicroPCB: `.venv\Scripts\python -m micropcb`
   - Port **COM20** → **Connect** → **Zero all**.
3. Open `examples\aircut-test.nc`, go to the **Auto-level** tab:
   - **Probe here** → touch at about −3.000 mm. Jog Z up 1 mm, **Probe here**
     again → same value. In the bridge: *Probe touches* +1 each time.
   - **Use job size** → **Start auto-level**. Points appear in 2D/3D;
     the range should match the tilt / bulge you set.
   - **Apply height map to the job** → **Run** (bridge simulator executes it).
4. Change the tilt/bulge in the bridge and measure again: the map follows.

Measured end-to-end on 2026-11-13 (real com0com ports, real-time timing):
heights matched the simulated board exactly; about 8 s per point.

### B. Real machine
Same as A, but the bridge in **Bridge to real machine** mode with the
machine port (HW-597B). Clip on the copper, spindle OFF, tool 1–3 mm above
the board. Then the hardware test in `docs/stage2-design.md` §2.4.
The bridge log + MicroPCB console (Show raw traffic) record everything —
send them if a probe check fails.
(MicroPCB does not need the bridge on the real machine; it is only for
watching the traffic.)

## 7. Recording serial data (version 0.2)
Box **Record serial data (timed capture)** in the bridge window:
- **● Record** starts a capture, **■ Stop recording** ends it. **Record on Start** records
  automatically from bridge Start to Stop.
- Files go to `Documents\MicroPCB captures` (change with **Folder…**):
  - `capture-YYYYMMDD-HHMMSS.csv` – one row per packet / reply, time in seconds (µs
    resolution, taken in the bridge threads), direction, bytes, decoded ticks, spindle bits,
    step counter X/Y/Z, gap to the previous row, reply time after the last packet, notes
    (probe counter value, split replies, stops). Header lines `#` say mode, ports, version.
  - `…-report.txt` – summary: packet / reply counts, reply time statistics per packet kind,
    real time per tick while streaming, max steps per tick, travel and range, spindle
    on/off times, keep-alive interval, probe touches, long pauses, slow replies,
    overload / release events.
  - `…-motion.nc` – the motion the Wegstr software actually sent, rebuilt as G-code
    (straight segments, F from the real timing, jog / job sections, M3/M5).
- **Analyze capture…** makes the report and .nc again from any capture CSV; the same from
  the command line:
  `.venv\Scripts\python -m micropcb.bridge.analyze "path\capture-….csv"`
- **Save log…** saves the traffic window text.

For analysis by Claude: run a G-code job in the Wegstr software with recording on, then
send the `.csv` (and the original `.nc` job) — the timing shows how the original software
streams, ramps and waits, and the motion file can be compared with the job.
