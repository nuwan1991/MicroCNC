"""Every module must at least compile/import (catches broken UI files the
other tests never load)."""
import compileall
from pathlib import Path


def test_all_sources_compile():
    src = Path(__file__).resolve().parents[1] / "src" / "micropcb"
    assert compileall.compile_dir(str(src), quiet=1, force=True)
