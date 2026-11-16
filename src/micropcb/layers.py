"""Job layers and project files (MicroCNC 0.3).

A project holds several G-code files as layers - top isolation, drill, alignment
holes, bottom isolation, board cutout ... - each with its own job options, plus
one height map per board side. Start runs the selected layer.

Project file (*.mcnc.json):
    {"app": "MicroCNC", "format": 1, "selected": 0,
     "layers": [{"path": "top.nc", "name": "...", "kind": "top", "side": "top",
                 "origin": "file", "max_depth": 0.08, "visible": true}, ...],
     "maps": {"top": <height map JSON or null>, "bottom": ...}}
Paths are stored relative to the project file when they are below its folder.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

KINDS = {   # kind: (label, colour, default side, max cut depth used by default)
    "top": ("Top isolation", "#d4a64a", "top", True),
    "bottom": ("Bottom isolation", "#56b6c2", "bottom", True),
    "drill": ("Drill", "#e5534b", "top", False),
    "align": ("Alignment holes", "#c678dd", "top", False),
    "cutout": ("Board cutout", "#8b97a5", "top", False),
    "other": ("G-code", "#9cdcfe", "top", True),
}
SIDES = ("top", "bottom")


def guess_kind(filename: str, meta=None) -> str:
    """Layer kind from the CAM side file (operation / side) or the file name."""
    if meta is not None:
        op = str(getattr(meta, "operation", "") or "").lower()
        side = str(getattr(meta, "side", "") or "").lower()
        if "align" in op or "pin" in op:
            return "align"
        if "drill" in op:
            return "drill"
        if "cut" in op or "outline" in op:
            return "cutout"
        if "iso" in op or "clear" in op or "engrav" in op:
            return "bottom" if side.startswith("b") else "top"
    n = os.path.basename(filename).lower()
    if any(k in n for k in ("align", "pin", "fiducial")):
        return "align"
    if any(k in n for k in ("drill", "drl", "hole")):
        return "drill"
    if any(k in n for k in ("cutout", "outline", "edge", "profile", "contour", "cut")):
        return "cutout"
    if any(k in n for k in ("bottom", "bot", "b_cu", "b.cu", "back", "solder")):
        return "bottom"
    if any(k in n for k in ("top", "f_cu", "f.cu", "front", "iso", "pcb", "copper")):
        return "top"
    return "other"


@dataclass
class Layer:
    path: str
    text: str = ""
    kind: str = "other"
    side: str = "top"
    name: str = ""
    origin: str = "file"          # "file" | "corner"
    max_depth: float = 0.0        # mm, 0 = off
    visible: bool = True
    meta: object = None           # jobfile.JobInfo, not saved in the project
    missing: bool = False         # file not found when the project was opened

    @property
    def label(self) -> str:
        return self.name or KINDS.get(self.kind, KINDS["other"])[0]

    @property
    def color(self) -> str:
        return KINDS.get(self.kind, KINDS["other"])[1]

    @classmethod
    def new(cls, path: str, text: str, meta=None, default_depth: float = 0.0,
            origin: str = "file") -> "Layer":
        kind = guess_kind(path, meta)
        label, _, side, use_depth = KINDS[kind]
        side_meta = str(getattr(meta, "side", "") or "").lower()
        if side_meta.startswith("b"):
            side = "bottom"
        name = label if kind != "other" else os.path.splitext(os.path.basename(path))[0]
        return cls(path=path, text=text, kind=kind, side=side, name=name, origin=origin,
                   max_depth=default_depth if use_depth else 0.0, meta=meta)

    def to_dict(self, base: str | None = None) -> dict:
        p = self.path
        if base and p:
            try:
                rel = os.path.relpath(p, base)
                if not rel.startswith(".."):
                    p = rel
            except ValueError:      # other drive
                pass
        return {"path": p, "name": self.name, "kind": self.kind, "side": self.side,
                "origin": self.origin, "max_depth": self.max_depth, "visible": self.visible}


@dataclass
class Project:
    layers: list[Layer] = field(default_factory=list)
    selected: int = -1
    maps: dict = field(default_factory=dict)     # side -> height map JSON text (or None)
    path: str = ""

    def to_json(self, path: str) -> str:
        base = os.path.dirname(os.path.abspath(path))
        maps = {s: (json.loads(t) if t else None) for s, t in self.maps.items()}
        return json.dumps({"app": "MicroCNC", "format": 1, "selected": self.selected,
                           "layers": [ly.to_dict(base) for ly in self.layers],
                           "maps": maps}, indent=1)

    @classmethod
    def load(cls, path: str) -> "Project":
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict) or "layers" not in d:
            raise ValueError("not a MicroCNC project file")
        base = os.path.dirname(os.path.abspath(path))
        layers = []
        for e in d["layers"]:
            p = str(e.get("path", ""))
            if p and not os.path.isabs(p):
                p = os.path.normpath(os.path.join(base, p))
            ly = Layer(path=p, kind=str(e.get("kind", "other")),
                       side=str(e.get("side", "top")), name=str(e.get("name", "")),
                       origin=str(e.get("origin", "file")),
                       max_depth=float(e.get("max_depth", 0.0) or 0.0),
                       visible=bool(e.get("visible", True)))
            if ly.kind not in KINDS:
                ly.kind = "other"
            if ly.side not in SIDES:
                ly.side = "top"
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    ly.text = f.read()
            except OSError:
                ly.missing = True
            layers.append(ly)
        maps = {s: (json.dumps(m) if m else None) for s, m in (d.get("maps") or {}).items()
                if s in SIDES}
        sel = int(d.get("selected", 0) if layers else -1)
        return cls(layers, max(-1, min(sel, len(layers) - 1)), maps, path)
