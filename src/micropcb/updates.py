"""Check for updates: read a small JSON file from the MicroCNC web site.

latest.json (uploaded with every release next to the installer):
    {"version": "0.3.1", "date": "2026-12-02",
     "notes": "What's new ...", "url": "https://www.microchip.lk/MicroCNC"}

Only "version" is required. Nothing is downloaded or installed automatically.
"""
from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass

from . import DOWNLOAD_URL, UPDATE_URL, __version__


@dataclass
class UpdateInfo:
    version: str
    date: str = ""
    notes: str = ""
    url: str = DOWNLOAD_URL

    @property
    def newer(self) -> bool:
        return parse_version(self.version) > parse_version(__version__)


def parse_version(v: str) -> tuple[int, ...]:
    """"0.3.10" -> (0, 3, 10); text after the numbers is ignored ("0.3.1-beta")."""
    m = re.match(r"\s*v?(\d+(?:\.\d+)*)", str(v))
    if not m:
        raise ValueError(f"not a version: {v!r}")
    nums = [int(x) for x in m.group(1).split(".")]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def parse_info(text: str) -> UpdateInfo:
    d = json.loads(text)
    if not isinstance(d, dict) or "version" not in d:
        raise ValueError("no version in the update file")
    parse_version(d["version"])
    url = str(d.get("url") or DOWNLOAD_URL)
    if not url.lower().startswith(("https://", "http://")):
        url = DOWNLOAD_URL
    return UpdateInfo(str(d["version"]), str(d.get("date", "")), str(d.get("notes", "")), url)


def fetch(url: str = UPDATE_URL, timeout: float = 8.0) -> UpdateInfo:
    """Download and parse latest.json. Raises OSError / ValueError on failure."""
    req = urllib.request.Request(url, headers={"User-Agent": f"MicroCNC/{__version__}",
                                               "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 (fixed https URL)
        return parse_info(r.read(65536).decode("utf-8", errors="replace"))
