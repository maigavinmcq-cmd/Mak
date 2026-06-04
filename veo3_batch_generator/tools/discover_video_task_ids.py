from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


TASK_ID_PATTERN = re.compile(r"\btask_[A-Za-z0-9_-]+\b")
VIDEO_HINTS = (
    "video submitted task_id",
    "video task id",
    "视频任务已提交",
    "resubmit success",
    "MANUAL_POLL",
    "video completed",
    "视频已下载",
)


def _iter_jsonl(path: Path):
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line_no, line in enumerate(fh, 1):
                try:
                    yield line_no, json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return None


def runtime_event_roots(project_root: Path) -> list[Path]:
    roots = [
        Path(tempfile.gettempdir()) / "veo3_batch_generator" / "runtime_events",
        project_root / "outputs" / "runtime_events",
    ]
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return unique


def _add_record(records: dict[str, dict], task_id: str, record: dict) -> None:
    if not task_id:
        return
    task_id = str(task_id).strip()
    if task_id in {"task_id", "task_ids"}:
        return
    existing = records.get(task_id)
    if existing is None:
        records[task_id] = record
        return
    # Preserve the richest task mapping and merge source hints.
    for key, value in record.items():
        if value not in (None, "") and existing.get(key) in (None, ""):
            existing[key] = value
    sources = set(str(existing.get("sources") or "").split(" | ")) if existing.get("sources") else set()
    if record.get("source"):
        sources.add(str(record["source"]))
    if sources:
        existing["sources"] = " | ".join(sorted(sources))


def _extract_from_task(task: dict, source: Path, records: dict[str, dict]) -> None:
    if not isinstance(task, dict):
        return
    batch_id = task.get("batch_id") or ""
    task_uid = task.get("task_uid") or ""
    for node_id, state in (task.get("node_states") or {}).items():
        if not isinstance(state, dict) or "video" not in str(node_id):
            continue
        task_id = str(state.get("task_id") or "").strip()
        if not task_id:
            continue
        _add_record(
            records,
            task_id,
            {
                "task_id": task_id,
                "batch_id": batch_id,
                "task_uid": task_uid,
                "row_index": task.get("row_index"),
                "task_name": task.get("task_name"),
                "pid": task.get("pid"),
                "owner": task.get("owner"),
                "node_id": node_id,
                "status": state.get("status"),
                "video_url": state.get("output_video_url") or task.get("video_url"),
                "video_local_path": state.get("output_video_local_path") or task.get("video_file_path"),
                "provider": state.get("provider") or task.get("video_provider"),
                "model_logical_key": state.get("model_logical_key") or task.get("video_model_logical_key"),
                "source": str(source),
            },
        )


def discover(
    project_root: Path,
    *,
    batch_id: str = "",
    include_fallback: bool = True,
    max_event_file_mb: float = 512.0,
) -> dict[str, dict]:
    records: dict[str, dict] = {}
    outputs = project_root / "outputs"
    batch_filter = str(batch_id or "").strip()

    for event_root in runtime_event_roots(project_root):
        for path in sorted(event_root.glob("*/worker_events.jsonl")):
            if batch_filter and path.parent.name != batch_filter:
                continue
            try:
                size_mb = path.stat().st_size / 1024 / 1024
            except OSError:
                continue
            if max_event_file_mb > 0 and size_mb > max_event_file_mb:
                print(f"skip oversized event file: {path} ({size_mb:.1f} MB)")
                continue
            for line_no, event in _iter_jsonl(path):
                event_batch_id = event.get("batch_id") or path.parent.name
                if event.get("type") == "task_updated":
                    _extract_from_task(event.get("task") or {}, path, records)
                    continue
                if event.get("type") != "log":
                    continue
                message = str(event.get("message") or "")
                if not any(hint in message for hint in VIDEO_HINTS):
                    continue
                for task_id in TASK_ID_PATTERN.findall(message):
                    _add_record(
                        records,
                        task_id,
                        {
                            "task_id": task_id,
                            "batch_id": event_batch_id,
                            "task_uid": event.get("task_uid"),
                            "row_index": None,
                            "task_name": None,
                            "pid": None,
                            "owner": None,
                            "node_id": None,
                            "status": None,
                            "video_url": None,
                            "video_local_path": None,
                            "provider": None,
                            "model_logical_key": None,
                            "source": f"{path}:{line_no}",
                            "log_message": message[:500],
                        },
                    )

    if not include_fallback:
        return records

    for path in sorted((outputs / "state_save_fallback").glob("*.json")):
        if batch_filter and not path.name.startswith(f"{batch_filter}_"):
            continue
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        for task in data.get("tasks") or []:
            _extract_from_task(task, path, records)

    # Some local fallback batch dirs may exist under outputs/batches.
    for path in sorted((outputs / "batches").glob("*/task_state.json")):
        if batch_filter and path.parent.name != batch_filter:
            continue
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        for task in data.get("tasks") or []:
            _extract_from_task(task, path, records)

    return records


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover persisted video task IDs from local runtime events and state snapshots.")
    parser.add_argument("--project-root", default=str(PROJECT_ROOT), help="veo3_batch_generator project root")
    parser.add_argument("--out-dir", default="", help="Output directory. Defaults to outputs/video_task_recovery")
    parser.add_argument("--batch-id", default="", help="Only scan one batch/runtime event directory")
    parser.add_argument("--skip-fallback", action="store_true", help="Skip state_save_fallback snapshots")
    parser.add_argument("--max-event-file-mb", type=float, default=512.0, help="Skip worker_events files larger than this. Use 0 to disable.")
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    records = discover(
        project_root,
        batch_id=str(args.batch_id or "").strip(),
        include_fallback=not bool(args.skip_fallback),
        max_event_file_mb=float(args.max_event_file_mb or 0),
    )
    out_dir = Path(args.out_dir) if args.out_dir else project_root / "outputs" / "video_task_recovery"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "discovered_video_task_ids.json"
    csv_path = out_dir / "discovered_video_task_ids.csv"
    items = sorted(records.values(), key=lambda item: (str(item.get("batch_id") or ""), int(item.get("row_index") or 0), str(item.get("task_id") or "")))
    json_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    fieldnames = [
        "task_id",
        "batch_id",
        "task_uid",
        "row_index",
        "task_name",
        "pid",
        "owner",
        "node_id",
        "status",
        "video_url",
        "video_local_path",
        "provider",
        "model_logical_key",
        "source",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(items)
    print(f"discovered unique video task ids: {len(items)}")
    print(f"json: {json_path}")
    print(f"csv: {csv_path}")


if __name__ == "__main__":
    main()
