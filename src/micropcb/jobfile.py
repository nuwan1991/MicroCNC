"""Job side file `<name>.nc.json` and job list `<board>-jobs.json` (split design, phase 1).

Written by the CAM part next to every exported G-code file, read by the machine
control part. Plain JSON, mm, work coordinates (the same as the G-code).
See docs/split-design.md section 4.

This module must not import micropcb.pcb (the Control program will not have it).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field

FORMAT = "micropcb-job"
LIST_FORMAT = "micropcb-joblist"
VERSION = 1
MAX_COPPER_POINTS = 20000

OPERATIONS = ("isolation", "clearing", "drilling", "holes", "cutout", "pins", "combined")


@dataclass
class JobFeature:
    kind: str            # "hole" | "pad"
    x: float
    y: float
    size: float          # hole diameter / pad size, mm


@dataclass
class JobInfo:
    operation: str = "combined"
    side: str = "top"                     # top | bottom
    mirror_x_mm: float | None = None      # mirror axis for the bottom side
    zero: str = "board front-left"
    board_size: tuple[float, float] = (0.0, 0.0)
    outline: list[list[tuple[float, float]]] = field(default_factory=list)
    tools: list[dict] = field(default_factory=list)   # in order of use
    features: list[JobFeature] = field(default_factory=list)
    copper: list[list[tuple[float, float]]] = field(default_factory=list)
    time_s: float | None = None
    cut_area: tuple[float, float, float, float] | None = None
    source: list[str] = field(default_factory=list)
    app: str = ""
    created: str = ""
    nc_sha1: str = ""

    # ------------------------------------------------------------------ json
    def to_dict(self) -> dict:
        d = {"format": FORMAT, "version": VERSION,
             "created": self.created or time.strftime("%Y-%m-%d %H:%M:%S"),
             "app": self.app, "source": list(self.source),
             "operation": self.operation, "side": self.side,
             "mirror_x_mm": self.mirror_x_mm, "zero": self.zero,
             "board": {"size": list(self.board_size),
                       "outline": [[list(p) for p in ring] for ring in self.outline]},
             "tools": self.tools,
             "features": [asdict(f) for f in self.features],
             "copper": [[[round(x, 4), round(y, 4)] for x, y in ring]
                        for ring in _limit(self.copper)],
             "expected": {"time_s": self.time_s,
                          "cut_area": list(self.cut_area) if self.cut_area else None},
             "nc_sha1": self.nc_sha1}
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "JobInfo":
        if d.get("format") != FORMAT:
            raise ValueError("not a MicroPCB job side file")
        if int(d.get("version", 0)) > VERSION:
            raise ValueError(f"side file version {d.get('version')} is newer than this "
                             "program - update MicroPCB")
        b = d.get("board") or {}
        exp = d.get("expected") or {}
        return cls(
            operation=d.get("operation", "combined"), side=d.get("side", "top"),
            mirror_x_mm=d.get("mirror_x_mm"), zero=d.get("zero", "board front-left"),
            board_size=tuple(b.get("size") or (0.0, 0.0)),
            outline=[[tuple(p) for p in ring] for ring in b.get("outline") or []],
            tools=list(d.get("tools") or []),
            features=[JobFeature(f["kind"], float(f["x"]), float(f["y"]), float(f["size"]))
                      for f in d.get("features") or []],
            copper=[[tuple(p) for p in ring] for ring in d.get("copper") or []],
            time_s=exp.get("time_s"),
            cut_area=tuple(exp["cut_area"]) if exp.get("cut_area") else None,
            source=list(d.get("source") or []), app=d.get("app", ""),
            created=d.get("created", ""), nc_sha1=d.get("nc_sha1", ""))

    @property
    def holes(self) -> list[JobFeature]:
        return [f for f in self.features if f.kind == "hole"]


def _limit(rings, max_points: int = MAX_COPPER_POINTS):
    """Keep whole rings until the point budget is used (largest rings first stay)."""
    out, n = [], 0
    for ring in rings:
        if n + len(ring) > max_points:
            break
        out.append(ring)
        n += len(ring)
    return out


def sha1(text: str) -> str:
    return hashlib.sha1(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def side_path(nc_path: str) -> str:
    return nc_path + ".json"


def write(nc_path: str, nc_text: str, info: JobInfo) -> str:
    """Write the side file for nc_path (its text = nc_text). Returns the path."""
    info.nc_sha1 = sha1(nc_text)
    p = side_path(nc_path)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(info.to_dict(), f, indent=1)
    return p


def read(nc_path: str, nc_text: str | None = None) -> tuple[JobInfo | None, str]:
    """Side file for a G-code file: (info, message).

    info is None when there is none or it does not belong to this G-code
    (edited / regenerated since) - the message says why."""
    p = side_path(nc_path)
    if not os.path.exists(p):
        return None, ""
    try:
        with open(p, encoding="utf-8") as f:
            info = JobInfo.from_dict(json.load(f))
    except (OSError, ValueError, KeyError, TypeError) as e:
        return None, f"side file not usable: {e}"
    if nc_text is not None and info.nc_sha1 and info.nc_sha1 != sha1(nc_text):
        return None, "side file is out of date (the G-code changed) - using the G-code only"
    return info, "with board data"


# ---------------------------------------------------------------------- job list
def write_list(path: str, board: str, files: list[dict]) -> str:
    """files: [{"file": "top-isolation.nc", "operation": ..., "tool": label}, ...] in order."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"format": LIST_FORMAT, "version": VERSION, "board": board,
                   "created": time.strftime("%Y-%m-%d %H:%M:%S"), "files": files}, f, indent=1)
    return path


def read_list(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if d.get("format") != LIST_FORMAT:
        raise ValueError("not a MicroPCB job list")
    return d
