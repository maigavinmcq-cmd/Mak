from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import openpyxl


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.batch_manager import BatchManager
from app.config import load_config
from app.models.task import TaskItem, TaskLogEntry, TaskStatus, now_text
from app.task_manager import TaskManager
from app.workflow import NODE_STATUS_SUBMITTED, NODE_TYPE_VIDEO, ensure_task_node_states


TASK_ID_PATTERN = re.compile(r"\b(?:v|task)_[A-Za-z0-9_-]+\b")


def _task_ids_from_text(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    return TASK_ID_PATTERN.findall(text)


def read_xlsx_task_ids(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for sheet in workbook.worksheets:
        headers = []
        for row_index, row in enumerate(sheet.iter_rows(values_only=True), start=1):
            values = ["" if cell is None else str(cell).strip() for cell in row]
            if row_index == 1:
                headers = [value.lower() for value in values]
            for col_index, value in enumerate(values, start=1):
                for task_id in _task_ids_from_text(value):
                    if task_id in seen:
                        continue
                    seen.add(task_id)
                    records.append(
                        {
                            "task_id": task_id,
                            "row_index": str(len(records) + 1),
                            "task_name": f"RECOVER_{task_id}",
                            "pid": task_id,
                            "owner": "",
                            "source": str(path),
                            "source_sheet": sheet.title,
                            "source_row": str(row_index),
                            "source_column": headers[col_index - 1] if col_index - 1 < len(headers) else str(col_index),
                        }
                    )
    return records


def read_csv_task_ids(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for index, row in enumerate(reader, start=1):
            task_id = str(row.get("task_id") or "").strip()
            if not task_id:
                for value in row.values():
                    ids = _task_ids_from_text(value)
                    if ids:
                        task_id = ids[0]
                        break
            if not task_id or task_id in seen:
                continue
            seen.add(task_id)
            records.append(
                {
                    "task_id": task_id,
                    "row_index": str(row.get("row_index") or index),
                    "task_name": str(row.get("task_name") or f"RECOVER_{task_id}"),
                    "pid": str(row.get("pid") or task_id),
                    "owner": str(row.get("owner") or ""),
                    "source": str(path),
                }
            )
    return records


def recovery_workflow_definition() -> dict[str, Any]:
    return {
        "workflow_version": "video_task_recovery_v1",
        "nodes": [
            {
                "node_id": "video_stage_1",
                "node_name": "视频任务恢复下载",
                "node_type": NODE_TYPE_VIDEO,
                "prompt_field": "",
                "input_refs": [],
                "output_key": "video_1",
                "enabled": True,
                "allow_manual_run": False,
                "allow_retry": False,
            }
        ],
    }


def build_tasks(records: list[dict[str, str]], batch_id: str, batch_name: str, source_excel: Path, config) -> list[TaskItem]:
    workflow = recovery_workflow_definition()
    imported_at = now_text()
    tasks: list[TaskItem] = []
    for index, record in enumerate(records, start=1):
        task_id = str(record["task_id"]).strip()
        row_index = int(record.get("row_index") or index)
        task = TaskItem(
            row_index=row_index,
            task_name=str(record.get("task_name") or f"RECOVER_{task_id}"),
            pid=str(record.get("pid") or task_id),
            owner=str(record.get("owner") or ""),
            netdisk_path="",
            image_prompt="",
            video_prompt="",
            status=TaskStatus.VIDEO_POLLING,
            video_task_id=task_id,
            video_provider=str(config.video_provider or "jimmy_veo"),
            video_model_logical_key=str(config.video_model_logical_key or "veo_3_1_fast"),
            video_status=NODE_STATUS_SUBMITTED,
            batch_id=batch_id,
            batch_name=batch_name,
            imported_at=imported_at,
            source_excel_path=str(source_excel),
            task_logs=[
                TaskLogEntry(
                    level="INFO",
                    category="SYSTEM",
                    node_id="video_stage_1",
                    message=f"恢复批次导入视频 task_id={task_id}，等待轮询和下载",
                    detail=str(record.get("source") or ""),
                )
            ],
        )
        ensure_task_node_states(task, workflow)
        state = task.node_states["video_stage_1"]
        state["status"] = NODE_STATUS_SUBMITTED
        state["task_id"] = task_id
        state["provider"] = task.video_provider
        state["model_logical_key"] = task.video_model_logical_key
        state["poll_count"] = 0
        state["started_at"] = imported_at
        state["error_message"] = None
        tasks.append(task)
    return tasks


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a recovery batch from existing video task_ids.")
    parser.add_argument("--source-excel", required=True)
    parser.add_argument("--fallback-csv", default="")
    parser.add_argument("--batch-id", default="")
    parser.add_argument("--batch-name", default="")
    args = parser.parse_args()

    source_excel = Path(args.source_excel)
    records = read_xlsx_task_ids(source_excel)
    source_used = source_excel
    fallback_used = False
    if not records and args.fallback_csv:
        fallback = Path(args.fallback_csv)
        records = read_csv_task_ids(fallback)
        source_used = fallback
        fallback_used = True

    if not records:
        print(json.dumps({"ok": False, "error": "no_task_ids_found", "source_excel": str(source_excel)}, ensure_ascii=False))
        return 2

    config = load_config()
    batch_manager = BatchManager(config.software_log_root, config.batch_root_dir_name)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    preferred_batch_id = args.batch_id or f"recover_video_task_ids_{stamp}"
    batch_id = batch_manager.unique_batch_id(preferred_batch_id)
    batch_name = args.batch_name or f"视频task_id恢复下载_{stamp}"
    workflow = recovery_workflow_definition()
    batch_config = {
        "video_provider": str(config.video_provider or "jimmy_veo"),
        "video_model_logical_key": str(config.video_model_logical_key or "veo_3_1_fast"),
        "video_api_key": str(config.video_api_key or ""),
        "video_api_base_url": str(config.video_api_base_url or ""),
        "video_download_root": str(config.video_download_root),
        "software_log_root": str(config.software_log_root),
        "auto_download_video": True,
        "enable_stage_based_workflow": True,
        "workflow_engine_version": "video_task_recovery_v1",
        "auto_retry_failed_workflow_enabled": False,
        "auto_retry_image_nodes": False,
        "auto_retry_video_nodes": False,
        "auto_retry_video_download": False,
        "poll_interval_seconds": int(config.poll_interval_seconds or 20),
        "poll_concurrency": min(10, max(1, int(config.poll_concurrency or 10))),
        "request_timeout_seconds": int(config.request_timeout_seconds or 600),
        "download_concurrency": int(config.download_concurrency or 50),
    }

    tasks = build_tasks(records, batch_id, batch_name, source_excel, config)
    batch = batch_manager.create_batch(
        tasks,
        source_excel_path=str(source_excel),
        batch_name=batch_name,
        batch_config=batch_config,
        batch_id=batch_id,
        workflow_definition=workflow,
    )
    manager = TaskManager(batch_manager.task_state_path(batch.batch_id))
    manager.configure_batch_context(batch.batch_id, batch.batch_name, batch.imported_at, batch.source_excel_path)
    manager.configure_defaults("", "", batch_config["video_provider"], batch_config["video_model_logical_key"])
    manager.set_tasks(tasks, preserve_existing=False)
    batch = batch_manager.update_batch(batch.batch_id, tasks, status_override="POLLING") or batch
    run_log = batch_manager.log_path(batch.batch_id)
    run_log.parent.mkdir(parents=True, exist_ok=True)
    run_log.write_text(
        f"[INFO] recovery batch created at {now_text()}\n"
        f"[INFO] source_excel={source_excel}\n"
        f"[INFO] source_used={source_used}\n"
        f"[INFO] fallback_used={fallback_used}\n"
        f"[INFO] task_ids={len(records)}\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "ok": True,
                "batch_id": batch.batch_id,
                "batch_name": batch.batch_name,
                "task_count": len(tasks),
                "batch_dir": str(batch_manager.batch_dir(batch.batch_id)),
                "state_path": str(batch_manager.task_state_path(batch.batch_id)),
                "source_excel_count": len(read_xlsx_task_ids(source_excel)),
                "fallback_used": fallback_used,
                "source_used": str(source_used),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
