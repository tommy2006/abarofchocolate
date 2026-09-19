"""Live sensor monitor: a page of its own next to the run pipeline. See ``monitor.LiveMonitor``."""
from __future__ import annotations

from .monitor import LiveMonitor
from .router import create_router

__all__ = ["LiveMonitor", "create_router"]
