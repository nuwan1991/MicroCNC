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
