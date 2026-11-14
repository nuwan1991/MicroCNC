# Wegstr CNC Serial Protocol (draft v0.1)

Recovered from static analysis (JDK `javap` disassembly) of the original Wegstr
software v3.2.0 (`wegstr.jar`, main class `com.cnc.wegstr.Wegstr`), for
interoperability only. No original code is reused.

Status legend: ✅ confirmed in bytecode · ❓ needs live serial capture

---

## 1. Transport ✅

| Setting | Value |
|---|---|
| Interface | USB-serial (CH340) as a COM port |
| Baud | **9600** |
| Format | **8 data bits, 1 stop bit, no parity** (8N1) |
| Flow control | none set explicitly |
| Library used by original | JSSC (obfuscated) |

The protocol is **binary**, not G-code. The PC software does all G-code
interpretation and motion planning, and streams raw **step packets**. The
machine is effectively a "dumb" step executor.

---

## 2. PC → Machine messages

### 2.1 Motion packet (7 bytes) ✅

```
[header] [X0 lo][X0 hi] [t1 lo][t1 hi] [t2 lo][t2 hi]
          └─ tick 0 ─┘   └─ tick 1 ─┘   └─ tick 2 ─┘
```

Always **exactly 3 ticks** per packet (original throws if not 3).

**Header byte:**

| Header | Used for |
|---|---|
| `0xFF` | Normal job (project) execution |
| `0x3F` (`?`) | Handshake/probe ping, manual jog/move, auto-level probing (surface-touch sensitive) |

**Each tick = 2 bytes** encoding step counts for X, Y, Z plus spindle flag:

Inputs per tick: `x, y, z` (signed ints, |x|,|y|,|z| ≤ 15), `spindle` (bool).

```
byte0 (lo):
  bits 0-3 : |x|
  bit  4   : x < 0
  bits 5-7 : |z| & 0b111

byte1 (hi):
  bits 0-3 : |y|
  bit  4   : y < 0
  bit  5   : |z| >= 8   (z bit 3)
  bit  6   : z < 0
  bit  7   : spindle ON
```

So max **15 steps per axis per tick**. Speed is controlled by how many steps
are packed per tick (tick period is fixed by firmware ❓).

Reference encoder (pseudo-code):

```python
def encode_tick(x, y, z, spindle):
    lo = abs(x) | (0x10 if x < 0 else 0) | ((abs(z) & 7) << 5)
    hi = abs(y) | (0x10 if y < 0 else 0)
    if abs(z) >= 8: hi |= 0x20
    if z < 0:       hi |= 0x40
    if spindle:     hi |= 0x80
    return bytes([lo & 0xFF, hi & 0xFF])
```

### 2.2 Settings / keep-alive packet (2 bytes) ✅

```
[0x7F] [performance]
```

- Sent every **1000 ms** while connected and idle (no job, no move running).
- `performance` = user setting `COMPUTER_PERFORMANCE`, default **4**.
  Meaning ❓ (likely buffer depth or tick timing).

### 2.3 Handshake / port detection ✅

1. Enumerate COM ports, open each at 9600 8N1.
2. Send a `0x3F` packet with 3 zero ticks: `3F 00 00 00 00 00 00`.
3. Wait up to **500 ms** for any reply byte `1`, `2` or `3` → Wegstr found.

---

## 3. Machine → PC messages ✅

First byte is the status code:

| Byte 0 | Meaning | Original software reaction |
|---|---|---|
| `0x01` | **Ready / ACK** – packet consumed, send next | Send next queued packet; clears "released" & "overload" flags |
| `0x02` | **Spindle overload** | Stop job / move, turn spindle off, show warning |
| `0x03` | **Machine released** (motors disabled / emergency stop / lid?) ❓ exact cause | Stop job and moves |
| `0x04` `hi` `lo` | **Surface touch** (probe contact) + 16-bit value `(hi<<8)\|lo` | Stop move; in auto-level, value used as Z measurement ❓ units |

Notes:
- Reply `4` may arrive split: `[4, hi]` then `[lo]` in the next read; the
  original reassembles it. Our parser must buffer bytes, not rely on reads.
- Status `2`/`3` also act as "ready" for the next command.

### 3.1 Flow control ✅

Strict **stop-and-wait**: one packet is sent, then the PC waits for `0x01`
before sending the next. Further packets are queued on the PC side. A 100 ms
UI timer runs alongside (watchdog purpose ❓).

---

## 4. Units & scaling

| Item | Value |
|---|---|
| Distance per step | `10 / FINE_CORRECTION_<axis>` mm |
| Default `FINE_CORRECTION_X/Y/Z` | **2500** → **0.004 mm/step** (250 steps/mm) ✅ |
| Max manual coordinate | ±9999 mm (clamp in UI) |
| Display precision | 3 decimals |
| Go-to-zero safe height default | 5.0 mm |
| Z direction constant | −10 (or −1 in offline/sim mode) ❓ |

`FINE_CORRECTION` is a per-machine calibration value; expose it in settings.

---

## 5. Feature notes from the original

- Statuses: `STARTED`, `SLEPT` (paused), `RETURNING`, `GOING_TO_ZERO`,
  `AUTO_LEVEL`, `AL_RETURNING` (+ `_SLEPT` variants).
- Auto-level uses `0x3F` packets so the machine stops on probe contact and
  reports reply `4`.
- Auto-spindle setting `AUTO_SPINDLE_STATE` exists.
- Detects ports `/dev/tty*usb*` on Linux/macOS; all ports on Windows.

---

## 5a. Live capture results (2026-11-13, `Request view/1.txt`) ✅

~12 min session: 2739 writes, 2756 reads.

- **Port setup confirmed:** 9600 baud, 8N1, DTR control on, RTS control.
- **Keep-alive confirmed:** `7F 04` every ~1000 ms when idle → reply `01`
  within ~4 ms.
- **Encoding confirmed:** decoding all 2276 motion packets with §2.1 gives
  sensible moves (e.g. net Z −16887 steps ≈ −67.5 mm at 0.004 mm/step).
- **Timing (key result):** ACK delay for a full packet is **~58.8 ms**
  regardless of 9–14 steps/tick → firmware runs **~19.6 ms per tick**
  (≈ 51 ticks/s). Short packets (3 or 6 steps/tick) are ACKed in 9 / 20 ms
  — the machine appears to have a small buffer and ACKs early when it is
  empty. The original never uses more than **14** steps/tick.
  - ⇒ Max speed ≈ 14 steps × 0.004 mm / 19.6 ms ≈ **2.9 mm/s ≈ 170 mm/min**
    per axis (needs check against UI feed values).
  - Speed control = number of steps per tick. Jogs ramp up
    3 → 6 → 9 → 10 … → 14 steps/tick (acceleration).
- **Probe reply confirmed:** `04 hi lo`, arrives split over 2–3 reads.
  Seen values `F1AA, F1AC, F1AE, F1B0` (−3670…−3664) and
  `ECE8, E499, E532, E7E9` → signed 16-bit, probably **Z position in
  steps** at the moment of contact (−3668 × 0.004 ≈ −14.7 mm). Repeat
  probes on the same spot agree within ±3 steps (±0.012 mm).
- **Original-software bug (explains auto-level freeze):** its reader only
  handles the probe reply as one 3-byte read or as `[04 hi]` + `[lo]`. The
  user's replacement CH340 USB-TTL adapter delivers it as `04`,`hi`,`lo`
  (3 single-byte reads) or `04` + `hi lo`; the original then indexes past
  the array end, the serial handler dies, and auto-level hangs after the
  first touch. MicroPCB's `ReplyParser` buffers until 3 bytes → immune.
- A `04 04 8B` was received right after a `7F 04` keep-alive (probe
  touching while idle).
- Not observed: replies `02` (overload), `03` (released); spindle bit set
  in only 3 ticks.

## 5b. Capture 2 (`Request view/2.txt`) — motor release button ✅

- **Handshake confirmed:** `3F 00 00 00 00 00 00` → `01` (9 ms), then
  `7F 04` keep-alive starts.
- **Reply `03` = motors released** by the machine's *release* button
  (motors unpowered, axes turn by hand). It is a **state**, not an event:
  while released the machine answers **every** packet (keep-alive or
  motion) with `03` instead of `01`. Pressing the button again → replies
  return to `01`.
  - Seen: 16:09:29–16:09:49, 16:09:57–16:10:00, 16:14:10–16:14:11.
  - ⇒ After a release, PC step count is no longer valid → position must be
    marked **lost**; user must re-zero.
- **Hardware E-stop** cuts total power → appears to the PC as no reply /
  USB device lost (no protocol message).
- More probe readings: `D492`, `D461`, `C64E`.

## 6. Open questions → live capture

Answer these with a serial sniffer while using the original software:

1. Tick period (how long the machine takes per tick → max feed rate).
2. Meaning of `performance` byte in `7F xx` (try values 1..10).
3. Exact trigger for reply `03` (E-stop button? motor disable? lid?).
4. ~~Units/sign of the 16-bit value in reply `04`.~~ Answered: machine's absolute Z step counter (signed 16-bit); the original uses differences to the first probe. See stage2-design.md §2.4.
5. Does the machine need keep-alive `7F` to stay enabled? What happens if
   it stops?
6. Any per-axis direction inversion or homing/limit switches? (none found
   in code yet → likely no hardware homing).

Suggested capture sessions: connect → idle 10 s → jog X+ 1 mm → jog Z− →
spindle on/off → small G-code job → probe touch → press E-stop.

## Observations from bridge capture 2026-11-14 (original software, real machine, PCB_test.txt + 55-point auto-level)
- **Machine pace:** `01` replies arrive every 59.2 ms while streaming = **19.74 ms per tick**, constant;
  the reply comes ~40 ms after a packet, i.e. the machine holds ~1 packet in a buffer and
  answers when a slot is free. The original software answers in 0.5–30 ms (median 16 ms) and
  never starved the machine.
- **Speed:** jog, rapid (G0) and feed all run at max 14 steps/tick (≈170 mm/min); the original
  software does not make G0 faster.
- **Ramp:** every G-code segment starts at 10 steps/tick (main axis) and adds 1 per tick up to 14;
  there is **no deceleration** — segments end abruptly (last tick = remainder) and the next starts
  at 10 again, also at corners and on each short arc piece.
- **Spindle:** the job starts moving in the same packet that sets the spindle bit — no spin-up wait.
  G0 X Y Z is executed as one 3-axis line.
- **Probing:** Z− at 14 steps/tick until reply `04 hi lo`; ~80 steps (2 packets) that were already
  sent after the touch are refused by the machine, so the sent-step count drifts by ≈0.32 mm per
  probe while the counter stays exact (repeat touches −2495, −2495, −2495). The lift after a touch
  starts with ramp 10,11,12… Use the counter, never the sent steps (MicroPCB does).
- MicroPCB's planner estimate for PCB_test.txt (5109 ticks = 100.8 s) matches the original run
  (99.8 s) within 1 %.
- **Touch is not reported at slow Z (capture 2026-11-14 17:01, MicroPCB):** probing with
  `3F 60 40 60 40 60 40` (Z −3 per tick) the machine answered `01` to every packet while the
  user touched the clip to the tool repeatedly — no `04`. The original probes at Z −14 per tick
  and every touch is reported. MicroPCB now probes at 14 steps/tick, one moving tick per packet.
  Lowest speed that still reports a touch: not yet known.
