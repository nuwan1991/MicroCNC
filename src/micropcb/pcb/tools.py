"""Tool library (Stage 3): V-bits, end mills, drills with feeds.

Saved as %APPDATA%\\MicroPCB\\tools.json. Feeds are in mm/min; the Wegstr
moves at most about 170 mm/min, faster values simply run at the maximum.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

VBIT, ENDMILL, DRILL = "vbit", "endmill", "drill"


@dataclass
class Tool:
    name: str
    kind: str                 # vbit | endmill | drill
    diameter: float           # mm (shank side for V-bits: not used for width)
    angle: float = 0.0        # V-bit included angle, degrees
    tip: float = 0.0          # V-bit tip width, mm
    feed: float = 120.0       # mm/min
    plunge: float = 40.0      # mm/min
    stepdown: float = 0.5     # mm per pass (end mills / pecking)

    def cut_width(self, depth: float) -> float:
        """Width of the groove at this depth."""
        if self.kind == VBIT:
            return self.tip + 2 * abs(depth) * math.tan(math.radians(self.angle / 2))
        return self.diameter

    @property
    def label(self) -> str:
        if self.kind == VBIT:
            return f"{self.name} (V {self.angle:g}° tip {self.tip:g})"
        return f"{self.name} (Ø{self.diameter:g})"


def default_tools() -> list[Tool]:
    t = [
        Tool("V-bit 30° 0.1", VBIT, 3.175, 30, 0.1, 100, 30, 0.1),
        Tool("V-bit 20° 0.1", VBIT, 3.175, 20, 0.1, 100, 30, 0.1),
        Tool("V-bit 60° 0.2", VBIT, 3.175, 60, 0.2, 100, 30, 0.1),
        Tool("End mill 0.8", ENDMILL, 0.8, feed=100, plunge=30, stepdown=0.4),
        Tool("End mill 1.0", ENDMILL, 1.0, feed=120, plunge=30, stepdown=0.5),
        Tool("End mill 2.0", ENDMILL, 2.0, feed=150, plunge=40, stepdown=0.6),
    ]
    for d in (0.6, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.5, 2.0, 3.0, 3.2):
        t.append(Tool(f"Drill {d:g}", DRILL, d, feed=60, plunge=40, stepdown=0.8))
    return t


@dataclass
class ToolLibrary:
    tools: list[Tool] = field(default_factory=default_tools)

    def of_kind(self, *kinds: str) -> list[Tool]:
        return [t for t in self.tools if t.kind in kinds]

    def get(self, name: str) -> Tool | None:
        return next((t for t in self.tools if t.name == name), None)

    def drill_sizes(self) -> list[float]:
        return sorted(t.diameter for t in self.of_kind(DRILL))

    @classmethod
    def load(cls, path: Path | None = None) -> "ToolLibrary":
        from ..settings import app_dir
        path = path or app_dir() / "tools.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls([Tool(**t) for t in data["tools"]])
        except (OSError, ValueError, KeyError, TypeError):
            return cls()

    def save(self, path: Path | None = None) -> None:
        from ..settings import app_dir
        path = path or app_dir() / "tools.json"
        path.write_text(json.dumps({"tools": [asdict(t) for t in self.tools]}, indent=1),
                        encoding="utf-8")
