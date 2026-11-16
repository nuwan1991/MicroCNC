"""Persistent settings and machine state (JSON in %APPDATA%\\MicroPCB)."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


def heightmap_dir(s: "Settings") -> Path:
    """Folder for automatically saved height maps."""
    d = Path(s.heightmap_dir) if s.heightmap_dir else \
        Path.home() / "Documents" / "MicroPCB height maps"
    d.mkdir(parents=True, exist_ok=True)
    return d


def app_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / ".config")
    d = Path(base) / "MicroPCB"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class Settings:
    mm_per_step: list[float] = field(default_factory=lambda: [0.004, 0.004, 0.004])
    work_area_mm: list[float] = field(default_factory=lambda: [140.0, 200.0, 40.0])
    max_steps_per_tick: int = 14
    job_max_depth: float = 0.0       # mm, 0 = off ("Max cut depth" of the job)
    job_origin: str = "file"         # "file" | "corner" (front-left of the job = work 0)
    jog_steps_per_tick: int = 14
    performance: int = 4
    safe_height_mm: float = 5.0
    soft_limits: bool = True
    auto_reconnect: bool = True
    last_port: str = ""
    last_dir: str = ""
    view_quality: str = "full"      # full | light | 2d
    check_max_depth_mm: float = 2.0
    # safety switches (Settings dialog; all on by default)
    resume_via_safe_height: bool = True   # paused above copper: resume at safe Z to next cut
    resume_restart_spindle: bool = True   # spindle off during pause: restart before going down
    jog_low_z_warning: bool = True        # ask before X/Y jog with the tool low
    jog_low_z_mm: float = 0.5             # "low" = work Z below this
    autosave_heightmaps: bool = True      # save every finished auto-level run
    heightmap_dir: str = ""               # "" = Documents/MicroPCB height maps

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or app_dir() / "settings.json"
        s = cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            names = {f.name for f in fields(cls)}
            for k, v in data.items():
                if k in names:
                    setattr(s, k, v)
        except (OSError, ValueError):
            pass
        return s

    def save(self, path: Path | None = None) -> None:
        path = path or app_dir() / "settings.json"
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


@dataclass
class MachineState:
    """Position bookkeeping that must survive restarts (no homing switches)."""

    machine_pos: list[int] = field(default_factory=lambda: [0, 0, 0])  # steps
    work_offset: list[int] = field(default_factory=lambda: [0, 0, 0])  # steps
    home_set: bool = False   # machine home defined -> soft limits active
    clean_exit: bool = True  # False while running; True after normal close

    @classmethod
    def load(cls, path: Path | None = None) -> "MachineState":
        path = path or app_dir() / "state.json"
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            return cls(**{k: d[k] for k in ("machine_pos", "work_offset",
                                            "home_set", "clean_exit") if k in d})
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self, path: Path | None = None) -> None:
        path = path or app_dir() / "state.json"
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
