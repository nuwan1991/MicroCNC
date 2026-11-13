# Serial Capture Guide

Goal: answer the open questions in [protocol.md](protocol.md) §6.

## Tool
**HHD Free Serial Port Monitor** (free edition), from hhdsoftware.com.
It sniffs a COM port while another program uses it, records timestamps,
and exports to text/CSV. Any similar sniffer works if it records **raw hex
with millisecond timestamps** for both directions.

## Setup
1. Plug in the machine, open Device Manager → Ports → note the CH340 COM
   number (e.g. COM5).
2. Start the sniffer **first** (HHD Free Serial Port Monitor), with Wegstr
   software closed:
   1. Run it **as Administrator** (it installs a capture driver; first run
      may ask to reboot).
   2. Menu **File → New Session** (or the *New Session* button). The
      *New Monitoring Session* wizard opens.
   3. **Select device:** tick the CH340 port (e.g. *USB-SERIAL CH340 (COM5)*).
      Next.
   4. **Select visualizers / processing:** tick **Request View** (or
      *Data View*) and **Console View** if offered. Request View shows each
      read/write with direction, timestamp and hex — this is the main one.
      Next.
   5. **Data destination:** tick **Log to file**, choose
      `...\New Software\captures\01-connect-idle.dmslog` (native format) —
      keeps everything even if the view is cleared. Next/Start.
   6. In Request View, right-click the column header and make sure
      **Time** (absolute, ms precision), **Direction**, **Data** (hex) are
      visible. If there's an option *Show IRP_MJ_DEVICE_CONTROL*, keep it on
      — it shows the baud-rate setup.
3. Then start the Wegstr software and connect. You should see rows appear
   in Request View immediately (writes `3F 00 00 ...`).
4. After each session: in the sniffer **Stop** the session, then
   **File → Export** (or right-click Request View → *Export / Save as*)
   to **text or CSV** with the same file name (`01-connect-idle.txt`).
   Send me both `.txt` and `.dmslog`. Start a **new session** for the next
   test (step 2.2) so files stay separate.

> Menu names vary slightly by version; if something isn't where described,
> send me a screenshot and I'll point to it.

5. Free edition limits: if it refuses to log/export or has a time cap,
   alternative is **"Serial Port Monitor" by Electronic Team** (14-day
   trial) – same procedure: *Session → New session → select COM port →
   Request view → Start monitoring*.
6. **Safety:** remove the tool or raise Z well above the bed; keep a hand
   near the power switch.

## Sessions (save each as a separate file)
Name files `NN-name.txt` and put them in `captures/`.

| # | File | What to do | Answers |
|---|---|---|---|
| 01 | `01-connect-idle.txt` | Connect, wait 15 s idle, disconnect | handshake, keep-alive |
| 02 | `02-perf-N.txt` | In settings change *Computer performance* to 1, then 10; idle 5 s each | performance byte |
| 03 | `03-jog-x.txt` | Jog X+ exactly 10 mm, then X− 10 mm (note the speed set in UI) | tick period, step scale |
| 04 | `04-jog-yz.txt` | Jog Y+ 10 mm, Z+ 5 mm, Z− 5 mm | axis bits, Z sign |
| 05 | `05-jog-speeds.txt` | Jog X+ 20 mm at slowest, then at fastest speed | feed-rate encoding |
| 06 | `06-spindle.txt` | Spindle on 5 s, off | spindle bit |
| 07 | `07-job.txt` | Run a tiny G-code (below), with a stopwatch | job stream, timing |
| 08 | `08-probe.txt` | Attach probe clip, run auto-level on a small area or touch-off | reply 04 value |
| 09 | `09-release.txt` | During a slow jog, press E-stop / whatever "releases" the machine | reply 03 |
| 10 | `10-overload.txt` | Only if safely possible: block spindle briefly | reply 02 (optional) |
| 11 | `11-unplug-keepalive.txt` | While connected & idle, close the Wegstr software abruptly (Task Manager); observe if motors release | keep-alive need |

Test G-code for session 07 (`test.nc`, air cut, Z well above material):
```
G21 G90
G0 Z5
G1 X10 F300
G1 Y10
G1 X0
G1 Y0
G0 Z10
M30
```
Write down: feed shown in UI, total run time measured.

## Also send
- Screenshot of the settings dialog (all values).
- Machine model and the motors' physical steps/mm if known.
- Notes on anything odd (beeps, lamp changes) with time.
