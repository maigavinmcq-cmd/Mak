from __future__ import annotations

import json
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from app.models.batch import TaskBatch
from app.models.task import TaskItem, TaskStatus
from app.workflow import clone_workflow_definition, default_workflow_definition


class BatchManager:
    def __init__(self, software_log_root: str | Path, root_dir_name: str = "batches") -> None:
        self.software_log_root = Path(software_log_root)
        self.root_dir_name = root_dir_name or "batches"
        self.root = self.software_log_root / self.root_dir_name
        self.index_path = self.root / "batch_index.json"
        self.ensure()

    def reconfigure(self, software_log_root: str | Path, root_dir_name: str = "batches") -> None:
        self.software_log_root = Path(software_log_root)
        self.root_dir_name = root_dir_name or "batches"
        self.root = self.software_log_root / self.root_dir_name
        self.index_path = self.root / "batch_index.json"
        self.ensure()

    def ensure(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.root = self.software_log_root / "project_outputs" / self.root_dir_name
            self.index_path = self.root / "batch_index.json"
            self.root.mkdir(parents=True, exist_ok=True)
        if not self.index_path.exists():
            self.save_index(self.repair_index_from_dirs([], save=False))

    def batch_dir(self, batch_id: str) -> Path:
        return self.root / batch_id

    def task_state_path(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / "task_state.json"

    def batch_info_path(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / "batch_info.json"

    def exports_dir(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / "exports"

    def log_path(self, batch_id: str) -> Path:
        return self.batch_dir(batch_id) / "run.log"

    def load_index(self) -> list[TaskBatch]:
        batches = self._read_index_file(self.index_path)
        if batches is None:
            batches = self._read_index_file(self.index_path.with_suffix(self.index_path.suffix + ".bak")) or []
        batches = self.repair_index_from_dirs(batches, save=True)
        return sorted(batches, key=lambda item: item.imported_at or item.batch_id, reverse=True)

    def save_index(self, batches: list[TaskBatch]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        ordered = sorted(batches, key=lambda item: item.imported_at or item.batch_id, reverse=True)
        self._write_json_atomic(self.index_path, {"batches": [batch.to_dict() for batch in ordered]})

    def _read_index_file(self, path: Path) -> list[TaskBatch] | None:
        try:
            if not path.exists() or path.stat().st_size == 0:
                return []
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return [TaskBatch.from_dict(item) for item in data.get("batches", []) if isinstance(item, dict)]

    def _write_json_atomic(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(payload, ensure_ascii=False, indent=2)
        for attempt in range(5):
            tmp_path = self._unique_tmp_json_path(path)
            try:
                tmp_path.write_text(content, encoding="utf-8")
                if path.exists() and path.stat().st_size > 0:
                    try:
                        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
                    except OSError:
                        pass
                tmp_path.replace(path)
                return
            except (FileNotFoundError, PermissionError):
                if attempt >= 4:
                    # Network shares can briefly lock or remove temp files
                    # during os.replace. A direct write is less ideal than the
                    # atomic path above, but preserving the batch record is more
                    # important than crashing the worker process.
                    path.write_text(content, encoding="utf-8")
                    return
                time.sleep(0.2 * (attempt + 1))
            finally:
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except OSError:
                    pass

    def _unique_tmp_json_path(self, path: Path) -> Path:
        stamp = int(time.time() * 1000)
        return path.with_name(
            f"{path.name}.{os.getpid()}.{threading.get_ident()}.{stamp}.tmp"
        )

    def repair_index_from_dirs(self, batches: list[TaskBatch] | None = None, save: bool = True) -> list[TaskBatch]:
        existing: dict[str, TaskBatch] = {}
        changed = False
        for batch in batches or []:
            if not batch.batch_id:
                changed = True
                continue
            if self._is_deleted_or_empty_index_record(batch):
                changed = True
                continue
            if batch.batch_id:
                existing[batch.batch_id] = batch
        try:
            children = list(self.root.iterdir()) if self.root.exists() else []
        except OSError:
            children = []
        for child in children:
            if not child.is_dir() or child.name in existing:
                continue
            batch = self._batch_from_dir(child)
            if batch and batch.batch_id:
                existing[batch.batch_id] = batch
                changed = True
        repaired = sorted(existing.values(), key=lambda item: item.imported_at or item.batch_id, reverse=True)
        if changed and save:
            self.save_index(repaired)
        return repaired

    def _is_deleted_or_empty_index_record(self, batch: TaskBatch) -> bool:
        folder = self.batch_dir(batch.batch_id)
        if (folder / ".deleted").exists():
            return True
        info_path = folder / "batch_info.json"
        state_path = folder / "task_state.json"
        try:
            if info_path.exists() and info_path.stat().st_size > 0:
                return False
            if state_path.exists() and state_path.stat().st_size > 0:
                data = json.loads(state_path.read_text(encoding="utf-8"))
                tasks = data.get("tasks", []) if isinstance(data, dict) else []
                return len(tasks) <= 0 and int(batch.task_count or 0) <= 0
        except (OSError, json.JSONDecodeError):
            return int(batch.task_count or 0) <= 0
        if folder.exists() and int(batch.task_count or 0) <= 0:
            return True
        return False

    def _batch_from_dir(self, folder: Path) -> TaskBatch | None:
        if (folder / ".deleted").exists():
            return None
        info_path = folder / "batch_info.json"
        try:
            if info_path.exists() and info_path.stat().st_size > 0:
                batch = TaskBatch.from_dict(json.loads(info_path.read_text(encoding="utf-8")))
                if not batch.batch_id:
                    batch.batch_id = folder.name
                return batch
        except (OSError, json.JSONDecodeError):
            pass
        task_count = 0
        state_path = folder / "task_state.json"
        try:
            if state_path.exists() and state_path.stat().st_size > 0:
                data = json.loads(state_path.read_text(encoding="utf-8"))
                task_count = len(data.get("tasks", [])) if isinstance(data, dict) else 0
        except (OSError, json.JSONDecodeError):
            task_count = 0
        if task_count <= 0:
            return None
        try:
            imported_at = datetime.fromtimestamp(folder.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        except OSError:
            imported_at = ""
        return TaskBatch(
            batch_id=folder.name,
            batch_name=self.default_batch_name(folder.name),
            source_excel_path="",
            imported_at=imported_at,
            task_count=task_count,
        )

    def existing_batch_ids(self) -> set[str]:
        ids = {batch.batch_id for batch in self.load_index() if batch.batch_id}
        try:
            ids.update(child.name for child in self.root.iterdir() if child.is_dir())
        except OSError:
            pass
        return ids

    def unique_batch_id(self, preferred: str | None = None) -> str:
        base = str(preferred or "").strip() or datetime.now().strftime("%Y-%m-%d_%H%M%S")
        existing = self.existing_batch_ids()
        if base not in existing and not self.batch_dir(base).exists():
            return base
        index = 1
        while True:
            candidate = f"{base}_{index:03d}"
            if candidate not in existing and not self.batch_dir(candidate).exists():
                return candidate
            index += 1

    def default_batch_name(self, batch_id: str) -> str:
        return f"批次_{batch_id}"

    def create_batch(
        self,
        tasks: list[TaskItem],
        source_excel_path: str | Path,
        batch_name: str | None = None,
        batch_config: dict | None = None,
        batch_id: str | None = None,
        workflow_definition: dict | None = None,
    ) -> TaskBatch:
        requested_batch_id = str(batch_id or "").strip()
        batch_id = self.unique_batch_id(requested_batch_id or None)
        if requested_batch_id and batch_id != requested_batch_id and batch_name == self.default_batch_name(requested_batch_id):
            batch_name = self.default_batch_name(batch_id)
        imported_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        config = dict(batch_config or {})
        snapshot = clone_workflow_definition(workflow_definition) if workflow_definition else clone_workflow_definition(default_workflow_definition())
        batch = TaskBatch(
            batch_id=batch_id,
            batch_name=batch_name or self.default_batch_name(batch_id),
            source_excel_path=str(source_excel_path),
            imported_at=imported_at,
            image_provider=str(config.get("image_provider") or ""),
            image_model_display_name=str(config.get("image_model_display_name") or ""),
            video_provider=str(config.get("video_provider") or ""),
            video_model_display_name=str(config.get("video_model_display_name") or ""),
            batch_config=config,
            workflow_definition=snapshot,
            workflow_version=str(snapshot.get("workflow_version") or ""),
        )
        self.batch_dir(batch_id).mkdir(parents=True, exist_ok=True)
        self.exports_dir(batch_id).mkdir(parents=True, exist_ok=True)
        batch = self.update_stats(batch, tasks, save=False)
        self.save_batch(batch)
        self.upsert_index(batch)
        return batch

    def resolve_batch_workflow(self, batch_id: str) -> dict:
        """Return the workflow_definition snapshot for a batch.

        If the batch was created before v2, lazily attach the current default
        workflow snapshot to its batch_info.json so subsequent runs are stable.
        """
        batch = self.load_batch(batch_id)
        if not batch:
            return clone_workflow_definition(default_workflow_definition())
        if isinstance(batch.workflow_definition, dict) and batch.workflow_definition.get("nodes"):
            snapshot = clone_workflow_definition(batch.workflow_definition)
            if snapshot != batch.workflow_definition:
                batch.workflow_definition = snapshot
                batch.workflow_version = str(snapshot.get("workflow_version") or "")
                try:
                    self.save_batch(batch)
                    self.upsert_index(batch)
                except OSError:
                    pass
            return clone_workflow_definition(snapshot)
        snapshot = clone_workflow_definition(default_workflow_definition())
        batch.workflow_definition = snapshot
        batch.workflow_version = str(snapshot.get("workflow_version") or "")
        try:
            self.save_batch(batch)
            self.upsert_index(batch)
        except OSError:
            pass
        return clone_workflow_definition(snapshot)

    def load_batch(self, batch_id: str) -> TaskBatch | None:
        info_path = self.batch_info_path(batch_id)
        try:
            if info_path.exists() and info_path.stat().st_size > 0:
                return TaskBatch.from_dict(json.loads(info_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            return None
        return next((batch for batch in self.load_index() if batch.batch_id == batch_id), None)

    def save_batch(self, batch: TaskBatch) -> None:
        self.batch_dir(batch.batch_id).mkdir(parents=True, exist_ok=True)
        self._write_json_atomic(self.batch_info_path(batch.batch_id), batch.to_dict())

    def upsert_index(self, batch: TaskBatch) -> None:
        batches = self.load_index()
        updated = False
        for index, item in enumerate(batches):
            if item.batch_id == batch.batch_id:
                merged = item.to_dict()
                merged.update(batch.to_dict())
                batches[index] = TaskBatch.from_dict(merged)
                updated = True
                break
        if not updated:
            batches.append(batch)
        self.save_index(batches)

    def rename_batch(self, batch_id: str, batch_name: str) -> TaskBatch | None:
        batch = self.load_batch(batch_id)
        if not batch:
            return None
        batch.batch_name = batch_name.strip() or self.default_batch_name(batch_id)
        self.save_batch(batch)
        self.upsert_index(batch)
        return batch

    def update_remark(self, batch_id: str, remark: str) -> TaskBatch | None:
        batch = self.load_batch(batch_id)
        if not batch:
            return None
        batch.remark = remark
        self.save_batch(batch)
        self.upsert_index(batch)
        return batch

    def update_batch(self, batch_id: str, tasks: list[TaskItem], status_override: str | None = None) -> TaskBatch | None:
        batch = self.load_batch(batch_id)
        if not batch:
            return None
        batch = self.update_stats(batch, tasks, status_override=status_override, save=True)
        self.upsert_index(batch)
        return batch

    def update_stats(self, batch: TaskBatch, tasks: list[TaskItem], status_override: str | None = None, save: bool = True) -> TaskBatch:
        total = len(tasks)
        completed = sum(1 for task in tasks if TaskStatus.is_done(task.status))
        skipped = sum(1 for task in tasks if task.status.startswith("SKIPPED"))
        failed = sum(1 for task in tasks if task.status.startswith("FAILED") or task.status == TaskStatus.VIDEO_FAILED)
        timeout = sum(1 for task in tasks if task.status == TaskStatus.VIDEO_TIMEOUT)
        polling = sum(1 for task in tasks if task.status == TaskStatus.VIDEO_POLLING)
        pending = sum(1 for task in tasks if task.status == TaskStatus.PENDING)
        running = sum(1 for task in tasks if TaskStatus.is_running(task.status))
        batch.task_count = total
        batch.completed_count = completed
        batch.failed_count = failed
        batch.skipped_count = skipped
        batch.polling_count = polling
        batch.pending_count = pending
        batch.timeout_count = timeout
        batch.progress_percent = round(completed / total * 100, 2) if total else 0.0
        batch.success_rate = round(completed / total * 100, 2) if total else 0.0
        batch.status = status_override or self._infer_status(total, completed, failed, skipped, timeout, pending, running)
        if save:
            self.save_batch(batch)
        return batch

    @staticmethod
    def _infer_status(total: int, completed: int, failed: int, skipped: int, timeout: int, pending: int, running: int) -> str:
        if total == 0 or pending == total:
            return "CREATED"
        if running:
            return "RUNNING"
        ended = completed + failed + skipped + timeout
        if ended >= total:
            if completed == total and not (failed or skipped or timeout):
                return "COMPLETED"
            if completed == 0 and failed + timeout >= total:
                return "FAILED"
            return "PARTIAL_FAILED"
        if failed or skipped or timeout:
            return "PARTIAL_FAILED"
        return "RUNNING"

    def latest_batch_id(self) -> str:
        batches = self.load_index()
        return batches[0].batch_id if batches else ""

    def remove_batch_record(self, batch_id: str, remove_batch_files: bool = False) -> None:
        self.save_index([batch for batch in self.load_index() if batch.batch_id != batch_id])
        if remove_batch_files:
            shutil.rmtree(self.batch_dir(batch_id), ignore_errors=True)
            return
        try:
            self.batch_dir(batch_id).mkdir(parents=True, exist_ok=True)
            (self.batch_dir(batch_id) / ".deleted").write_text(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
        except OSError:
            pass
        for path in [self.batch_info_path(batch_id), self.task_state_path(batch_id)]:
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
