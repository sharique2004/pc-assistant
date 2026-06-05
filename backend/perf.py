"""Lightweight, opt-in latency instrumentation for Bibi.

Turn it on with the env var ``BIBI_PERF=1``. When off, every helper here is a
near-zero-overhead no-op, so it is safe to leave the calls in production code.

It logs per-stage elapsed milliseconds and an optional resource probe
(RSS memory / thread count / child-process count) so you can see *where* time
goes and whether a long session is leaking (memory/threads climbing over time).

Usage:
    from perf import timer, probe
    with timer("vision"):
        ...                      # logs:  [perf] vision   1234.5 ms
    probe("after-cmd")           # logs:  [perf] probe after-cmd rss=210MB threads=14 children=0
"""
from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager

logger = logging.getLogger("bibi.perf")

# Enabled when BIBI_PERF is set to anything truthy (1/true/yes/on).
ENABLED = os.getenv("BIBI_PERF", "").strip().lower() in ("1", "true", "yes", "on")

try:  # psutil is optional — RSS/children just go unreported without it.
    import psutil  # type: ignore

    _PROC = psutil.Process()
except Exception:  # pragma: no cover
    _PROC = None


@contextmanager
def timer(stage: str):
    """Context manager that logs how long the wrapped block took (ms)."""
    if not ENABLED:
        yield
        return
    t0 = time.perf_counter()
    try:
        yield
    finally:
        dt = (time.perf_counter() - t0) * 1000.0
        logger.info("[perf] %-18s %9.1f ms", stage, dt)


def mark(stage: str, since: float) -> float:
    """Log elapsed ms since a prior ``time.perf_counter()`` stamp; return a new
    stamp so you can chain stage boundaries:

        t = time.perf_counter()
        ...; t = mark("transcribe", t)
        ...; t = mark("plan", t)
    """
    if ENABLED:
        dt = (time.perf_counter() - since) * 1000.0
        logger.info("[perf] %-18s %9.1f ms", stage, dt)
    return time.perf_counter()


def probe(label: str = "") -> None:
    """Log a resource snapshot — useful before/after a long session to catch
    'gets slower the longer it runs' leaks (rss/threads/children climbing)."""
    if not ENABLED:
        return
    threads = threading.active_count()
    rss_mb = -1
    children = -1
    if _PROC is not None:
        try:
            rss_mb = int(_PROC.memory_info().rss / (1024 * 1024))
            children = len(_PROC.children(recursive=True))
        except Exception:
            pass
    logger.info(
        "[perf] probe %-12s rss=%sMB threads=%s children=%s",
        label or "-", rss_mb, threads, children,
    )
