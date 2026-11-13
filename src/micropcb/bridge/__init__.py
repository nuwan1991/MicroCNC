"""MicroPCB Bridge: sits between the original Wegstr software and the machine.

- Bridge mode: forwards traffic to the real machine and re-assembles split
  probe replies (fixes the original software's auto-level freeze).
- Simulator mode: a simulated machine answers instead of a real one.
"""
