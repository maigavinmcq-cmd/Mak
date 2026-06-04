from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


def _process_memory_mb() -> float | None:
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
        if not ok:
            return None
        return round(counters.WorkingSetSize / 1024 / 1024, 2)
    except Exception:
        return None


class UIDiagnostics:
    """Low-overhead UI lag diagnostics."""

    def __init__(
        self,
        project_root: str | Path,
        output_root: str | Path | None = None,
        heartbeat_interval_ms: int = 500,
        lag_threshold_ms: int = 1500,
        slow_operation_ms: int = 700,
    ) -> None:
        self.project_root = Path(project_root)
        self.heartbeat_interval_ms = int(heartbeat_interval_ms)
        self.lag_threshold_ms = int(lag_threshold_ms)
        self.slow_operation_ms = int(slow_operation_ms)
        root = Path(output_root) if output_root else self.project_root / "outputs"
        self.log_dir = root / "ui_lag_diagnostics"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.ui_diagnostics_path = self.log_dir / f"ui_lag_diagnostics_{stamp}.jsonl"
        self.last_heartbeat = time.monotonic()
        self.last_action = ""
        self._last_lag_status_time = 0.0

    def mark_action(self, action: str) -> None:
        self.last_action = str(action or "")

    def snapshot(self, **extra: Any) -> dict[str, Any]:
        thread_names = [thread.name for thread in threading.enumerate()[:20]]
        payload: dict[str, Any] = {
            "active_threads": threading.active_count(),
            "thread_names": thread_names,
            "process_memory_mb": _process_memory_mb(),
            "last_action": self.last_action,
        }
        payload.update(extra)
        return payload

    def write_event(self, event_type: str, **payload: Any) -> None:
        data = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "event": event_type,
            **payload,
        }
        try:
            with self.ui_diagnostics_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError:
            pass

    def record_heartbeat(self, **extra: Any) -> dict[str, Any] | None:
        now = time.monotonic()
        expected = max(1, self.heartbeat_interval_ms) / 1000
        elapsed = now - self.last_heartbeat
        self.last_heartbeat = now
        delay_ms = round(max(0.0, elapsed - expected) * 1000, 2)
        if delay_ms < self.lag_threshold_ms:
            return None
        payload = self.snapshot(delay_ms=delay_ms, expected_interval_ms=self.heartbeat_interval_ms, **extra)
        self.write_event("UI_LAG", **payload)
        return payload

    @contextmanager
    def measure(self, operation: str, **context: Any) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
            if elapsed_ms >= self.slow_operation_ms:
                self.write_event(
                    "SLOW_UI_OPERATION",
                    operation=operation,
                    elapsed_ms=elapsed_ms,
                    **self.snapshot(**context),
                )
