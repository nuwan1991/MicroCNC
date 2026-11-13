"""MicroPCB command-line tool (milestone 1.1) - connect and jog.

Usage:
    python -m micropcb.cli ports
    python -m micropcb.cli run [--port COM5 | --sim] [--log]
"""
from __future__ import annotations

import argparse
import sys

from .jog import jog_ticks, mm_to_steps, pack
from .serial_link import (EV_LOST, EV_OVERLOAD, EV_PROBE, EV_RELEASED, EV_TRAFFIC,
                          SerialLink, find_machine, list_ports)

HELP = """Commands:
  x 10 | y -5 | z 1.5     jog axis by mm (relative)
  f 8                     max steps/tick for jog (1-14), now {f}
  s on | s off            spindle
  pos                     show position
  zero                    set current position as 0,0,0
  stop                    drop queued motion
  log                     toggle raw traffic log
  help | quit
"""

WORK_AREA_MM = {"x": 140.0, "y": 200.0, "z": 40.0}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="micropcb")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ports", help="list serial ports")
    r = sub.add_parser("run", help="connect and jog interactively")
    r.add_argument("--port")
    r.add_argument("--sim", action="store_true", help="use simulated machine")
    r.add_argument("--log", action="store_true", help="print raw traffic")
    r.add_argument("--mm-per-step", type=float, default=0.004)
    args = ap.parse_args(argv)

    if args.cmd == "ports":
        for dev, desc in list_ports():
            print(f"{dev:8} {desc}")
        return 0

    state = {"log": args.log}

    def listener(ev: str, payload: object) -> None:
        if ev == EV_TRAFFIC:
            if state["log"]:
                d, b = payload  # type: ignore[misc]
                print(f"  {d} {b.hex(' ')}")
        elif ev == EV_RELEASED:
            print("\n!! MOTORS RELEASED - position lost" if payload
                  else "\n.. motors engaged again - run 'zero' after re-aligning")
        elif ev == EV_OVERLOAD:
            print("\n!! SPINDLE OVERLOAD - motion stopped")
        elif ev == EV_PROBE:
            print(f"\n.. probe touch, machine value {payload}")
        elif ev == EV_LOST:
            print(f"\n!! CONNECTION LOST: {payload}")

    link = SerialLink(listener)
    if args.sim:
        from .sim_machine import SimMachine
        link.open(SimMachine())
        print("Connected to SIMULATED machine")
    else:
        port = args.port or find_machine()
        if not port:
            print("Wegstr not found. Use 'ports' and --port COMx.")
            return 1
        link.open_port(port)
        print(f"Connected on {port}")

    mmps = args.mm_per_step
    max_f = 14
    print(HELP.format(f=max_f))
    try:
        while link.connected:
            try:
                line = input("micropcb> ").strip().lower()
            except EOFError:
                break
            if not line:
                continue
            parts = line.split()
            cmd = parts[0]
            try:
                if cmd in ("quit", "exit", "q"):
                    break
                if cmd == "help":
                    print(HELP.format(f=max_f))
                elif cmd in ("x", "y", "z") and len(parts) == 2:
                    mm = float(parts[1])
                    if abs(mm) > WORK_AREA_MM[cmd]:
                        print(f"refused: more than {WORK_AREA_MM[cmd]} mm")
                        continue
                    steps = mm_to_steps(mm, mmps)
                    link.send(pack(jog_ticks(cmd, steps, max_f, link.spindle),
                                   spindle=link.spindle))
                    link.wait_idle(120)
                    show_pos(link, mmps)
                elif cmd == "f" and len(parts) == 2:
                    max_f = max(1, min(14, int(parts[1])))
                    print(f"max steps/tick = {max_f} "
                          f"(~{max_f * mmps / 0.0196 * 60:.0f} mm/min)")
                elif cmd == "s" and len(parts) == 2:
                    link.set_spindle(parts[1] == "on")
                    link.wait_idle(5)
                elif cmd == "pos":
                    show_pos(link, mmps)
                elif cmd == "zero":
                    link.set_position(0, 0, 0)
                    show_pos(link, mmps)
                elif cmd == "stop":
                    link.stop()
                elif cmd == "log":
                    state["log"] = not state["log"]
                else:
                    print("unknown command, type 'help'")
            except (ValueError, RuntimeError) as e:
                print(f"error: {e}")
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        link.close()
    return 0


def show_pos(link: SerialLink, mmps: float) -> None:
    x, y, z = link.position
    flag = "" if link.position_valid else "   (POSITION NOT VALID - re-zero)"
    print(f"X {x * mmps:9.3f}  Y {y * mmps:9.3f}  Z {z * mmps:9.3f} mm{flag}")


if __name__ == "__main__":
    sys.exit(main())
