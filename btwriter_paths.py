"""Shared paths for source runs and packaged Windows builds."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def app_base_dir() -> Path:
    override = os.environ.get("BTWRITER_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent
