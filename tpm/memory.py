"""RAM budget helpers. The target machine is a 16 GB laptop that often has < 3 GB free."""
from __future__ import annotations

import gc
import os
from typing import Iterator, Optional

import psutil


def free_bytes() -> int:
    return int(psutil.virtual_memory().available)


def total_bytes() -> int:
    return int(psutil.virtual_memory().total)


def budget_bytes(fraction: float = 0.35, floor_mb: int = 256, cap_mb: Optional[int] = None) -> int:
    """Bytes a single in-memory block may use right now."""
    b = int(free_bytes() * fraction)
    b = max(b, floor_mb * 1024 * 1024)
    if cap_mb:
        b = min(b, cap_mb * 1024 * 1024)
    return b


def chunk_rows(n_cols: int, bytes_per_value: int = 8, fraction: float = 0.35, min_rows: int = 20_000, max_rows: int = 2_000_000) -> int:
    """How many rows of n_cols fit in the current budget."""
    per_row = max(1, n_cols) * bytes_per_value * 2  # x2 for pandas overhead/copies
    n = budget_bytes(fraction) // per_row
    return int(min(max(n, min_rows), max_rows))


def duckdb_memory_limit() -> str:
    """DuckDB memory limit string, e.g. '1536MB'. Leaves headroom for Python + Ollama."""
    mb = max(512, int(free_bytes() * 0.4 / (1024 * 1024)))
    return f"{mb}MB"


def duckdb_threads() -> int:
    return max(1, min(os.cpu_count() or 2, 6))


def release() -> None:
    gc.collect()


def iter_ranges(n: int, step: int) -> Iterator[tuple[int, int]]:
    start = 0
    while start < n:
        end = min(n, start + step)
        yield start, end
        start = end


def memory_snapshot() -> dict:
    vm = psutil.virtual_memory()
    return {"total_gb": round(vm.total / 1e9, 2), "available_gb": round(vm.available / 1e9, 2), "percent_used": vm.percent}
