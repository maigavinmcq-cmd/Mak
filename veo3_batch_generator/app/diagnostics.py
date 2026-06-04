from __future__ import annotations

import json
import queue
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


class DiagnosticsLogger:
    """Small JSONL diagnostics writer for background batch execution.

    Diagnostics must never stop a batch. Network share writes can be slow or
    flaky, so every write is best-effort and guarded.
    """

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self.last_error: str | None = None
        self._queue: queue.Queue[str] | None = queue.Queue() if self.path else None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if self.path is not None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def event(self, event: str, **fields: Any) -> None:
        if self.path is None:
            return
        payload = {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "event": event,
            **fields,
        }
        try:
            text = json.dumps(payload, ensure_ascii=False, default=str)
            if self._queue is not None:
                self._queue.put(text)
        except Exception as exc:  # pragma: no cover - diagnostics are best effort
            self.last_error = str(exc)

    def close(self) -> None:
        if self.path is None:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._flush_all()

    @contextmanager
    def timed(self, event: str, **fields: Any) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            self.event(event, duration_ms=elapsed_ms, **fields)

    def _run(self) -> None:
        while not self._stop.is_set() or (self._queue is not None and not self._queue.empty()):
            batch: list[str] = []
            deadline = time.monotonic() + 0.5
            while len(batch) < 200:
                timeout = max(0.0, deadline - time.monotonic())
                try:
                    assert self._queue is not None
                    item = self._queue.get(timeout=timeout)
                except queue.Empty:
                    break
                batch.append(item)
                if time.monotonic() >= deadline:
                    break
            if batch:
                self._write_batch(batch)

    def _flush_all(self) -> None:
        if self._queue is None:
            return
        batch: list[str] = []
        while True:
            try:
                batch.append(self._queue.get_nowait())
                if len(batch) >= 200:
                    self._write_batch(batch)
                    batch = []
            except queue.Empty:
                break
        if batch:
            self._write_batch(batch)

    def _write_batch(self, batch: list[str]) -> None:
        if self.path is None or not batch:
            return
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    for line in batch:
                        handle.write(line + "\n")
        except Exception as exc:  # pragma: no cover - diagnostics are best effort
            self.last_error = str(exc)
