"""Unified-package API entry; keep the backend-layout source authoritative."""
from pathlib import Path
import runpy

_backend = runpy.run_path(str(Path(__file__).resolve().parents[1] / "backend" / "dashboard" / "plugin_api.py"))
router = _backend["router"]
