from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


_APPEND_EVENT_LOCK = threading.Lock()


def append_event(path: str | Path, event_type: str, payload: dict[str, Any] | None = None) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    event = dict(payload or {})
    event["type"] = event_type
    event.setdefault("created_at", time.time())
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    with _APPEND_EVENT_LOCK:
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def append_events(path: str | Path, events: list[dict[str, Any]]) -> None:
    if not events:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    now = time.time()
    for payload in events:
        event = dict(payload or {})
        event.setdefault("created_at", now)
        lines.append(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
    with _APPEND_EVENT_LOCK:
        with target.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


def read_events(
    path: str | Path,
    offset: int = 0,
    *,
    max_events: int | None = None,
    max_bytes: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    source = Path(path)
    if not source.exists():
        return [], offset
    try:
        size = source.stat().st_size
    except OSError:
        size = 0
    if int(offset or 0) > size:
        offset = 0
    events: list[dict[str, Any]] = []
    with source.open("r", encoding="utf-8") as handle:
        handle.seek(max(0, int(offset or 0)))
        start_offset = handle.tell()
        while True:
            if max_events is not None and len(events) >= max_events:
                break
            before = handle.tell()
            line = handle.readline()
            if not line:
                break
            after = handle.tell()
            if max_bytes is not None and events and after - start_offset > max_bytes:
                handle.seek(before)
                break
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                events.append(data)
        return events, handle.tell()
