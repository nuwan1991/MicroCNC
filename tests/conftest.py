"""Test-wide setup: never read or write the user's real MicroPCB settings, tool
library, machine state or camera data (%APPDATA%/MicroPCB). A test that closes a
window saves the settings - that once reset the user's "Max cut depth"."""
import pytest


@pytest.fixture(autouse=True)
def _private_app_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
