# MicroPCB

Control software for Wegstr CNC machines, focused on PCB milling.
Status: **milestone 1.1** — protocol, serial link, simulator, command-line jog tool.

Docs: [protocol](docs/protocol.md) · [Stage 1 design](docs/stage1-design.md) ·
[capture guide](docs/capture-guide.md)

## Setup (once)
```
python -m venv .venv
.venv\Scripts\python -m pip install -e .[dev]
```

## Run
GUI (milestone 1.2):
```
.venv\Scripts\python -m micropcb
```
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
