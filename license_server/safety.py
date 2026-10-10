"""Shared safety checks of the seller tools: the private key must live OUTSIDE the project and be owner-only."""

from __future__ import annotations

import os
import stat
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def inside_project(path: Path) -> bool:
    try:
        path.resolve().relative_to(PROJECT_ROOT)
        return True
    except ValueError:
        return False


def check_private_key_file(path: Path) -> str | None:
    """Return a problem description, or None if the key file location and permissions are acceptable."""
    if inside_project(path):
        return "the private key file is inside the project folder; keep it outside (it must never reach Git, Docker or a customer ZIP)"
    if not path.is_file():
        return "the private key file does not exist"
    if os.name == "posix":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            return f"the private key file is readable by other users (mode {oct(mode)}); run: chmod 600 <file>"
    return None
