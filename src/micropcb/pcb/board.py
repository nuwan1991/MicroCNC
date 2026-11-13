"""Board assembly: load a KiCad / EasyEDA export, sort the layers, place the
board on the machine (origin, rotation, mirror for the bottom side).

Roles: "top", "bottom", "outline", "drill", or "other" (mask, silk, paste,
drill maps … shown greyed, not used for machining).
"""
from __future__ import annotations

import io
import os
import re
import zipfile
from dataclasses import dataclass, field

from shapely import affinity
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.ops import polygonize, unary_union

from .excellon import DrillFile, Hole, Slot, is_excellon, parse_excellon
from .gerber import GerberLayer, is_gerber, parse_gerber

ROLES = ("top", "bottom", "outline", "drill", "other")
ROLE_NAMES = {"top": "Top copper", "bottom": "Bottom copper", "outline": "Board outline",
              "drill": "Drill", "other": "Other (not used)"}

# file-name rules: (regex on lower-case name, role); first match wins
_NAME_RULES = [
    (r"drl_map|drill_map|map\.gbr", "other"),
    (r"(f[_.]cu|\.gtl$|toplayer|top_layer|copper_top|\.cmp$)", "top"),
    (r"(b[_.]cu|\.gbl$|bottomlayer|bottom_layer|copper_bottom|\.sol$)", "bottom"),
    (r"(edge[_.]cuts|\.gm1$|\.gko$|outline|boardoutline|profile|\.gml$)", "outline"),
    (r"(\.drl$|\.xln$|\.drd$|drill|\.exc$|\.txt$)", "drill"),
    (r"(mask|silk|paste|\.gt[osp]$|\.gb[osp]$|fab|courtyard|\.g\d+$|adhes)", "other"),
]


def role_from_function(ff: str) -> str | None:
    f = ff.lower()
    if f.startswith("copper"):
        if "top" in f:
            return "top"
        if "bot" in f:
            return "bottom"
        return "other"   # inner layers
    if f.startswith("profile"):
        return "outline"
    if f.startswith(("plated", "nonplated")):
        return "drill"
    if f:
        return "other"
    return None


def role_from_name(name: str) -> str:
    n = os.path.basename(name).lower()
    for rx, role in _NAME_RULES:
        if re.search(rx, n):
            return role
    return "other"


@dataclass
class LayerFile:
    name: str
    role: str
    gerber: GerberLayer | None = None
    drill: DrillFile | None = None
    auto_role: str = ""
    text: str = ""            # kept for layers parsed on demand

    def ensure_parsed(self) -> None:
        """Parse a Gerber that was skipped at load (mask / silk / paste)."""
        if self.gerber is None and self.text and self.drill is None:
            self.gerber = parse_gerber(self.text, self.name)
            self.text = ""


def _read_inputs(paths: list[str]) -> list[tuple[str, str]]:
    """(name, text) for every file in the given files / folders / zips."""
    out: list[tuple[str, str]] = []

    def add_zip(data: bytes, label: str):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for info in z.infolist():
                if info.is_dir() or info.file_size > 50_000_000:
                    continue
                if info.filename.lower().endswith(".zip"):
                    add_zip(z.read(info), info.filename)
                    continue
                out.append((info.filename, z.read(info).decode("utf-8", "replace")))

    for p in paths:
        if os.path.isdir(p):
            for fn in sorted(os.listdir(p)):
                fp = os.path.join(p, fn)
                if os.path.isfile(fp):
                    out.extend(_read_inputs([fp]))
        elif p.lower().endswith(".zip"):
            with open(p, "rb") as f:
                add_zip(f.read(), p)
        else:
            if os.path.getsize(p) > 50_000_000:
                continue
            with open(p, encoding="utf-8", errors="replace") as f:
                out.append((p, f.read()))
    return out


def load_files(paths: list[str]) -> tuple[list[LayerFile], list[str]]:
    """Parse all Gerber / Excellon files. Returns (layers, problems)."""
    layers: list[LayerFile] = []
    problems: list[str] = []
    for name, text in _read_inputs(paths):
        base = os.path.basename(name)
        try:
            if is_excellon(text):
                d = parse_excellon(text, base)
                layers.append(LayerFile(base, "drill", drill=d, auto_role="drill"))
            elif is_gerber(text):
                m = re.search(r"TF\.FileFunction,([^*%\n]*)", text[:5000])
                role = (role_from_function(m.group(1)) if m else None) or role_from_name(base)
                if role == "drill":
                    role = "other"
                lf = LayerFile(base, role, auto_role=role, text=text)
                if role != "other":       # mask / silk / paste: parse only when shown
                    lf.ensure_parsed()
                layers.append(lf)
        except Exception as e:  # one bad file must not stop the import
            problems.append(f"{base}: {e}")
    order = {r: k for k, r in enumerate(ROLES)}
    layers.sort(key=lambda l: (order[l.role], l.name))
    return layers, problems


@dataclass
class Placement:
    rotation: int = 0          # degrees CCW, multiple of 90
    mirror: bool = False       # view from the bottom (board flipped)
    flip_axis: str = "x"       # mirror left-right ("x") or top-bottom ("y")
    margin: float = 0.0        # extra offset from work 0,0


@dataclass
class Board:
    layers: list[LayerFile] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, paths: list[str]) -> "Board":
        layers, problems = load_files(paths)
        return cls(layers, problems)

    # ------------------------------------------------------------ layers
    def by_role(self, role: str) -> list[LayerFile]:
        out = [l for l in self.layers if l.role == role]
        if role in ("top", "bottom", "outline"):
            for l in out:      # role may have been changed by the user
                l.ensure_parsed()
        return out

    def copper(self, side: str):
        g = [l.gerber.geometry for l in self.by_role(side) if l.gerber]
        return unary_union(g) if g else Polygon()

    def drills(self) -> tuple[list[Hole], list[Slot]]:
        holes, slots = [], []
        for l in self.by_role("drill"):
            if l.drill:
                holes += l.drill.holes
                slots += l.drill.slots
        return holes, slots

    def outline_polygon(self):
        """Board shape from the outline layer (largest closed area, holes =
        inner cut-outs). Falls back to the copper bounding box."""
        lines = []
        for l in self.by_role("outline"):
            if l.gerber:
                lines += [LineString(p) for p in l.gerber.paths if len(p) > 1]
        if lines:
            polys = list(polygonize(unary_union(lines)))
            if polys:
                polys.sort(key=lambda p: -p.area)
                outer = polys[0]
                inner = [p for p in polys[1:] if outer.contains(p.representative_point())]
                shape = Polygon(outer.exterior, [p.exterior for p in inner])
                return shape
            # outline not closed: use its extent
            u = unary_union(lines)
            return u.envelope if not u.is_empty else Polygon()
        g = unary_union([self.copper("top"), self.copper("bottom")])
        return g.envelope if not g.is_empty else Polygon()

    def native_bounds(self):
        shp = self.outline_polygon()
        if shp.is_empty:
            return None
        return shp.bounds

    # ------------------------------------------------------------ placement
    def transform(self, pl: Placement):
        """Function geometry -> geometry placing the board at work 0,0
        (lower-left), rotated and optionally mirrored."""
        b = self.native_bounds()
        if b is None:
            return lambda g: g, (0.0, 0.0)
        x0, y0, x1, y1 = b
        w, h = x1 - x0, y1 - y0
        rw, rh = (h, w) if pl.rotation % 180 else (w, h)

        def tf(g):
            g = affinity.translate(g, -x0 - w / 2, -y0 - h / 2)   # centre at 0,0
            if pl.rotation % 360:
                g = affinity.rotate(g, pl.rotation, origin=(0, 0))
            if pl.mirror:
                g = affinity.scale(g, -1 if pl.flip_axis == "x" else 1,
                                   -1 if pl.flip_axis == "y" else 1, origin=(0, 0))
            return affinity.translate(g, rw / 2 + pl.margin, rh / 2 + pl.margin)

        return tf, (rw, rh)

    def size(self, rotation: int = 0) -> tuple[float, float]:
        b = self.native_bounds()
        if b is None:
            return 0.0, 0.0
        w, h = b[2] - b[0], b[3] - b[1]
        return (h, w) if rotation % 180 else (w, h)

    def suggest_rotation(self, area_xy: tuple[float, float]) -> int:
        """0 if the board fits as is, 90 if it only fits rotated."""
        w, h = self.size(0)
        if w <= area_xy[0] and h <= area_xy[1]:
            return 0
        if h <= area_xy[0] and w <= area_xy[1]:
            return 90
        return 0

    def placed(self, pl: Placement) -> "PlacedBoard":
        tf, size = self.transform(pl)
        holes, slots = self.drills()

        def pt(x, y):
            p = tf(LineString([(x, y), (x, y + 1e-9)]))
            return p.coords[0]

        out = PlacedBoard(size=size, placement=pl)
        out.outline = tf(self.outline_polygon())
        for side in ("top", "bottom"):
            out.copper[side] = tf(self.copper(side))
        for l in self.by_role("other"):
            if l.gerber is not None and l.gerber.geometry is not None:
                out.other.append((l.name, tf(l.gerber.geometry)))
        for hh in holes:
            x, y = pt(hh.x, hh.y)
            out.holes.append(Hole(x, y, hh.d, hh.tool))
        for s in slots:
            x1, y1 = pt(s.x1, s.y1)
            x2, y2 = pt(s.x2, s.y2)
            out.slots.append(Slot(x1, y1, x2, y2, s.d, s.tool))
        return out


@dataclass
class PlacedBoard:
    """Everything in work coordinates (mm), ready for viewing / CAM."""
    size: tuple[float, float] = (0.0, 0.0)
    placement: Placement = field(default_factory=Placement)
    outline: object = None
    copper: dict = field(default_factory=dict)
    other: list = field(default_factory=list)
    holes: list = field(default_factory=list)
    slots: list = field(default_factory=list)

    def fits(self, area_xy: tuple[float, float]) -> bool:
        return self.size[0] <= area_xy[0] + 1e-6 and self.size[1] <= area_xy[1] + 1e-6

    def hole_sizes(self) -> dict[float, int]:
        out: dict[float, int] = {}
        for h in self.holes:
            out[round(h.d, 3)] = out.get(round(h.d, 3), 0) + 1
        for s in self.slots:
            out[round(s.d, 3)] = out.get(round(s.d, 3), 0) + 1
        return dict(sorted(out.items()))


def as_polygons(g) -> list[Polygon]:
    if g is None or g.is_empty:
        return []
    if isinstance(g, Polygon):
        return [g]
    if isinstance(g, MultiPolygon):
        return list(g.geoms)
    return [p for p in getattr(g, "geoms", []) if isinstance(p, Polygon)]
