"""Node-based workflow executor (v2 engine).

This module replaces the hardcoded "image stage → video stage" pipeline with a
DAG-driven executor that walks the per-batch ``workflow_definition``.

Key properties:
- Nodes are independent units. A failure in one node only blocks downstream
  nodes that depend on its output; sibling branches keep executing.
- Image nodes run synchronously inside the calling thread (the BatchWorker
  pool sizes this). Video nodes submit + return immediately; polling happens
  in a separate loop managed by BatchWorker.
- Legacy state (``task.generated_image_path`` / ``task.video_url`` / etc.) is
  mirrored into / out of ``task.node_states`` so older batches and the
  existing Excel exporter / GUI keep working with zero behaviour change.
- All side effects (saving state, emitting signals, logging) go through
  callbacks injected by BatchWorker, so this module has no Qt dependency.

Only the ``WorkflowExecutor`` is intended for external consumption.
"""

from __future__ import annotations

import json
import hashlib
import shutil
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from app.api.provider_registry import (
    get_image_provider,
    get_video_provider,
    model_display_name,
)
from app.api.upload_api import upload_image_for_public_url
from app.config import AppConfig
from app.file_utils import (
    download_video_to_path,
    build_existing_video_archive_index,
    find_existing_stage_video_path,
    find_product_image_for_task,
    path_to_http_url,
    save_image_task_assets,
    save_video_task_metadata,
    stage_image_output_path,
    stage_prompt_txt_path,
    stage_video_metadata_path,
    stage_video_output_path,
    sync_task_urls_from_paths,
    validate_downloaded_file,
)
from app.models.task import TaskItem, TaskStatus, now_text
from app.task_logs import append_task_log
from app.workflow import (
    NODE_STATUS_BLOCKED,
    NODE_STATUS_COMPLETED,
    NODE_STATUS_FAILED,
    NODE_STATUS_PENDING,
    NODE_STATUS_POLLING,
    NODE_STATUS_READY,
    NODE_STATUS_RUNNING,
    NODE_STATUS_SKIPPED,
    NODE_STATUS_SUBMITTED,
    NODE_STATUS_WAITING_INPUT,
    NODE_TYPE_IMAGE,
    NODE_TYPE_VIDEO,
    TERMINAL_NODE_STATUSES,
    clone_workflow_definition,
    default_workflow_definition,
    ensure_task_node_states,
    get_node_state,
    get_task_prompt_for_node,
    resolve_node_input_images,
    workflow_nodes,
)


# Aliases of legacy task statuses → workflow node statuses, used during the
# one-time migration when loading a pre-v2 batch.
_LEGACY_FAILURE_STATUSES = {
    TaskStatus.FAILED_IMAGE_API,
    TaskStatus.FAILED_VIDEO_API,
    TaskStatus.FAILED_UNKNOWN,
    TaskStatus.VIDEO_FAILED,
    TaskStatus.VIDEO_TIMEOUT,
}


@dataclass
class NodeRunOutcome:
    """Result of running a single node — used by callers (BatchWorker, manual
    re-run buttons, ...) to decide what to do next.
    """
    success: bool
    status: str
    error_message: Optional[str] = None
    submitted: bool = False  # video node submitted, awaiting poll
    completed: bool = False  # node finished and produced final output
    blocked: bool = False    # dependencies not satisfied


_IMAGE_ACTIVE_PROVIDER_STATUSES = {
    "queued",
    "queue",
    "pending",
    "submitted",
    "in_progress",
    "processing",
    "running",
    "timeout",
    "poll_timeout",
}


_IMAGE_FAILED_PROVIDER_STATUSES = {
    "failed",
    "failure",
    "error",
    "cancelled",
    "canceled",
}


_NON_RETRYABLE_AUTH_OR_QUOTA_PATTERNS = (
    "insufficient_user_quota",
    "insufficient quota",
    "quota_exceeded",
    "用户剩余额度",
    "预扣费额度失败",
    "余额不足",
    "额度不足",
    "invalid token",
    "invalid api key",
    "unauthorized",
    "authentication",
    "鉴权",
    "无效的令牌",
    "api key",
)


def _api_key_fingerprint(api_key: str) -> str:
    text = str(api_key or "").strip()
    if not text:
        return ""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    if len(text) <= 8:
        return f"****#{digest}"
    return f"{text[:6]}****{text[-4:]}#{digest}"


def _is_non_retryable_auth_or_quota_error(message: Any) -> bool:
    text = str(message or "").lower()
    if not text:
        return False
    if "http 401" in text or "http 403" in text:
        return True
    return any(pattern.lower() in text for pattern in _NON_RETRYABLE_AUTH_OR_QUOTA_PATTERNS)


def _is_retryable_poll_error(message: Any) -> bool:
    text = str(message or "").lower()
    if not text:
        return False
    patterns = (
        "request too frequent",
        "too frequent",
        "rate limit",
        "too many requests",
        "try later",
        "temporary",
        "timeout",
        "timed out",
        "bad gateway",
        "http 408",
        "http 429",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
        "http 520",
        "http 521",
        "http 522",
        "http 523",
        "http 524",
        "\u8bf7\u6c42\u8fc7\u4e8e\u9891\u7e41",
        "\u8bf7\u7a0d\u540e",
        "\u7a0d\u540e\u518d\u8bd5",
        "\u9891\u7e41",
    )
    return any(pattern.lower() in text for pattern in patterns)


_TERMINAL_FAILED_STATUSES = {"failed", "failure", "error", "cancelled", "canceled"}


def _status_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _raw_response_contains_terminal_failed(raw: Any) -> bool:
    if isinstance(raw, dict):
        for key in ("status", "state", "task_status"):
            if _status_text(raw.get(key)) in _TERMINAL_FAILED_STATUSES:
                return True
        for key in ("data", "result", "task"):
            if _raw_response_contains_terminal_failed(raw.get(key)):
                return True
    elif isinstance(raw, list):
        return any(_raw_response_contains_terminal_failed(item) for item in raw)
    return False


def _poll_result_is_explicit_terminal_failed(poll_result: Any) -> bool:
    if _status_text(getattr(poll_result, "status", None)) in _TERMINAL_FAILED_STATUSES:
        return True
    return _raw_response_contains_terminal_failed(getattr(poll_result, "raw_response", None))


def _video_state_has_confirmed_terminal_failure(state: dict) -> bool:
    if bool(state.get("terminal_failure_confirmed")):
        return True
    return _raw_response_contains_terminal_failed(state.get("raw_response"))


# Type aliases for the callback bag injected by BatchWorker.
LogCallback = Callable[[str, str, Optional[TaskItem]], None]
SaveEmitCallback = Callable[[TaskItem, bool], None]
TaskStatusCallback = Callable[[TaskItem, str, Optional[str], bool], None]


class WorkflowExecutor:
    """DAG executor that drives node-based task execution.

    Lifecycle per BatchWorker run:
        executor = WorkflowExecutor(config, workflow_def, log=..., save_emit=...)
        for task in targets:
            executor.prepare_task(task)              # source image + node migration
            outcomes = executor.run_ready_image_nodes(task)
            outcomes += executor.submit_ready_video_nodes(task)
        # polling loop separately polls executor.collect_polling_nodes(...)
        # downloads handled by executor.download_node(...) once a video completes
    """

    def __init__(
        self,
        config: AppConfig,
        workflow_definition: dict | None = None,
        *,
        log_callback: Optional[LogCallback] = None,
        save_emit_callback: Optional[SaveEmitCallback] = None,
        regenerate_existing_images: bool = False,
        stop_event: Optional[threading.Event] = None,
        pause_event: Optional[threading.Event] = None,
    ) -> None:
        self.config = config
        self.workflow_definition = clone_workflow_definition(workflow_definition or default_workflow_definition())
        self._log_callback = log_callback
        self._save_emit_callback = save_emit_callback
        self.regenerate_existing_images = bool(regenerate_existing_images)
        self._stop_event = stop_event or threading.Event()
        self._pause_event = pause_event or threading.Event()
        if not self._pause_event.is_set():
            self._pause_event.set()
        self._task_locks: dict[str, threading.Lock] = {}
        self._task_locks_guard = threading.Lock()
        self._archived_video_index: dict[str, str] | None = None
        # Throttling: cap how often we forward "in progress" state changes to
        # the GUI/save layer for the same task. Terminal transitions always
        # flow through immediately. Without this the Qt signal queue and disk
        # saves get hammered on every minor mutation (RUNNING → POLLING →
        # poll_count++ → ...), freezing the UI on large batches.
        self._save_emit_min_interval_seconds = 1.0
        self._last_save_emit_monotonic: dict[str, float] = {}
        self._emit_guard = threading.Lock()

    def set_archived_video_index(self, index: dict[str, str] | None) -> None:
        self._archived_video_index = dict(index or {})

    def build_archived_video_index(self, tasks) -> dict[str, str]:
        index = build_existing_video_archive_index(self.config.video_download_root, tasks, self.config.group_by_owner)
        self.set_archived_video_index(index)
        return index

    # ------------------------------------------------------------------ #
    # Workflow access helpers
    # ------------------------------------------------------------------ #
    def nodes(self) -> list[dict[str, Any]]:
        return list(workflow_nodes(self.workflow_definition))

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        for node in self.nodes():
            if str(node.get("node_id") or "") == node_id:
                return node
        return None

    def _node_lock(self, task: TaskItem) -> threading.Lock:
        key = str(getattr(task, "task_uid", "") or f"{task.pid}::row_{task.row_index}")
        with self._task_locks_guard:
            lock = self._task_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._task_locks[key] = lock
            return lock

    # ------------------------------------------------------------------ #
    # Task preparation: source image + legacy migration
    # ------------------------------------------------------------------ #
    def prepare_task(self, task: TaskItem) -> tuple[bool, Optional[str]]:
        """Make sure the task has source_image_1 + per-node states.

        Returns (ok, error_message). When ok is False the task has been marked
        SKIPPED or FAILED and should not progress to node execution.
        """
        ensure_task_node_states(task, self.workflow_definition)
        self.migrate_legacy_task(task)
        self.hydrate_legacy_video_url_for_download(task)
        self.repair_stale_node_inputs(task)

        if not task.product_image_path and not task.product_image_url:
            product_image, skipped_status, error = find_product_image_for_task(task, self.config)
            if skipped_status:
                # Mark all image nodes as WAITING_INPUT so the UI can explain
                # why the task didn't progress. Don't kill the task overall
                # status — let aggregate_task_status() decide what to surface.
                for node in self.nodes():
                    state = task.node_states.get(node["node_id"])
                    if state and state.get("status") in {NODE_STATUS_PENDING, NODE_STATUS_BLOCKED, NODE_STATUS_READY}:
                        state["status"] = NODE_STATUS_WAITING_INPUT
                        state["error_message"] = error or "缺少源图（产品白底图）"
                task.set_status(skipped_status, error)
                self._save_emit(task, force=True)
                return False, error
            task.product_image_path = product_image
            sync_task_urls_from_paths(task, self.config, prefer_local_generated=False, prefer_local_video=False)
            self._save_emit(task, force=False)
        return True, None

    def migrate_legacy_task(self, task: TaskItem) -> None:
        """One-time, idempotent migration of pre-v2 task state into node_states.

        Maps the old image / video fields onto the corresponding v2 nodes so
        that resumed batches don't lose their progress.
        """
        states = task.node_states or {}

        # image_stage_1 ← legacy image fields
        img1 = states.get("image_stage_1")
        if isinstance(img1, dict) and (task.generated_image_path or task.generated_image_url):
            img1["status"] = NODE_STATUS_COMPLETED
            if task.generated_image_path:
                img1["output_image_path"] = task.generated_image_path
            elif img1.get("output_image_path"):
                task.generated_image_path = img1.get("output_image_path")
            if task.generated_image_url:
                img1["output_image_url"] = task.generated_image_url
            elif img1.get("output_image_url"):
                task.generated_image_url = img1.get("output_image_url")
            if task.image_task_id:
                img1["task_id"] = task.image_task_id
            elif img1.get("task_id"):
                task.image_task_id = img1.get("task_id")
            if task.image_raw_response is not None:
                img1["raw_response"] = task.image_raw_response
            img1["ended_at"] = img1.get("ended_at") or task.ended_at or now_text()
        elif isinstance(img1, dict) and img1.get("status") in {None, "", NODE_STATUS_PENDING}:
            if task.status == TaskStatus.FAILED_IMAGE_API:
                img1["status"] = NODE_STATUS_FAILED
                img1["error_message"] = task.error_message or "图生图失败（旧版状态）"

        # video_stage_1 ← legacy video fields (submitted / completed / failed)
        vid1 = states.get("video_stage_1")
        if isinstance(vid1, dict) and task.video_url:
            vid1["status"] = NODE_STATUS_COMPLETED
            vid1["output_video_url"] = task.video_url
            if task.video_file_path:
                vid1["output_video_local_path"] = task.video_file_path
            elif vid1.get("output_video_local_path"):
                task.video_file_path = vid1.get("output_video_local_path")
            if task.video_task_id:
                vid1["task_id"] = task.video_task_id
            elif vid1.get("task_id"):
                task.video_task_id = vid1.get("task_id")
            if task.video_raw_response is not None:
                vid1["raw_response"] = task.video_raw_response
            vid1["ended_at"] = vid1.get("ended_at") or task.video_poll_end_time or task.ended_at
        elif isinstance(vid1, dict) and vid1.get("status") in {None, "", NODE_STATUS_PENDING}:
            if task.video_task_id:
                vid1["status"] = NODE_STATUS_SUBMITTED
                vid1["task_id"] = task.video_task_id
                vid1["poll_count"] = int(task.video_poll_count or 0)
            elif task.status in _LEGACY_FAILURE_STATUSES and task.error_message:
                vid1["status"] = NODE_STATUS_FAILED
                vid1["error_message"] = task.error_message

    def hydrate_legacy_video_url_for_download(self, task: TaskItem) -> int:
        """Mirror a legacy task.video_url into the matching video node.

        Some batches have a video URL already persisted in the legacy table
        fields while the v2 node state is still READY / WAITING / PENDING.
        Without this hydration the workflow downloader never sees the URL and
        the GUI keeps showing the task as waiting.
        """
        video_url = str(task.video_url or "").strip()
        if not video_url:
            return 0
        video_nodes = [node for node in self.nodes() if str(node.get("node_type")) == NODE_TYPE_VIDEO]
        if not video_nodes:
            return 0

        target_node = None
        task_id = str(task.video_task_id or "").strip()
        if task_id:
            for node in video_nodes:
                state = task.node_states.get(str(node.get("node_id") or "")) or {}
                if str(state.get("task_id") or "").strip() == task_id:
                    target_node = node
                    break
        if target_node is None:
            target_node = next((node for node in video_nodes if str(node.get("node_id") or "") == "video_stage_1"), video_nodes[0])

        node_id = str(target_node.get("node_id") or "")
        state = task.node_states.get(node_id) or {}
        if state.get("output_video_url") == video_url and state.get("status") == NODE_STATUS_COMPLETED:
            return 0

        state["status"] = NODE_STATUS_COMPLETED
        state["output_video_url"] = video_url
        if task.video_file_path:
            state["output_video_local_path"] = task.video_file_path
        if task_id:
            state["task_id"] = task_id
        state["error_message"] = None
        state["ended_at"] = state.get("ended_at") or task.video_poll_end_time or task.ended_at or now_text()
        self._mirror_video_node_to_legacy_fields(task, node_id, state)
        return 1

    def repair_stale_node_inputs(self, task: TaskItem) -> int:
        """Clear submitted/completed node state when the workflow input graph changed.

        This specifically protects the corrected v2 default flow: video_stage_1
        must consume image_stage_1.output_image. Older broken snapshots had
        video_stage_1 consuming source_image_1, which means their stored task_id
        could represent a video made from the product image. If a node recorded
        concrete input_images and they no longer match the normalized workflow,
        reset that node so it re-enters the queue with the correct dependency.
        """
        repaired = 0
        for node in self.nodes():
            node_id = str(node.get("node_id") or "")
            state = get_node_state(task, node_id)
            if not state:
                continue
            status = str(state.get("status") or NODE_STATUS_PENDING)
            if status in {NODE_STATUS_PENDING, NODE_STATUS_READY, NODE_STATUS_WAITING_INPUT, NODE_STATUS_BLOCKED}:
                continue
            recorded_inputs = [str(value or "").strip() for value in list(state.get("input_images") or []) if str(value or "").strip()]
            if not recorded_inputs:
                continue
            expected_inputs, missing = resolve_node_input_images(task, node)
            expected_inputs = [str(value or "").strip() for value in expected_inputs if str(value or "").strip()]
            stale = bool(missing) or set(recorded_inputs) != set(expected_inputs)
            if not stale:
                continue
            if (
                str(node.get("node_type") or "") == NODE_TYPE_IMAGE
                and self.complete_image_node_from_existing_output(task, node_id, state)
            ):
                continue
            self._reset_node_due_to_input_change(task, node, state, recorded_inputs, expected_inputs, missing)
            repaired += 1
        if repaired:
            self._save_emit(task, force=True)
        return repaired

    def _reset_node_due_to_input_change(
        self,
        task: TaskItem,
        node: dict,
        state: dict,
        recorded_inputs: list[str],
        expected_inputs: list[str],
        missing: list[str],
    ) -> None:
        node_id = str(node.get("node_id") or "")
        old_task_id = state.get("task_id")
        old_video_url = state.get("output_video_url")
        old_video_path = state.get("output_video_local_path")
        old_image_path = state.get("output_image_path")
        old_image_url = state.get("output_image_url")
        state["status"] = NODE_STATUS_PENDING
        state["task_id"] = None
        state["poll_count"] = 0
        state["output_image_path"] = None
        state["output_image_url"] = None
        state["output_video_url"] = None
        state["output_video_local_path"] = None
        state["raw_response"] = None
        state["started_at"] = None
        state["ended_at"] = None
        state["error_message"] = "流程依赖已更新，已清除旧提交，等待按新流程重新执行"
        if node_id == "video_stage_1":
            if old_task_id and task.video_task_id == old_task_id:
                task.video_task_id = None
            if old_video_url and task.video_url == old_video_url:
                task.video_url = None
            if old_video_path and task.video_file_path == old_video_path:
                task.video_file_path = None
            task.video_status = ""
            task.video_poll_count = 0
            task.video_poll_start_time = None
            task.video_poll_end_time = None
        if node_id == "image_stage_1":
            if old_image_path and task.generated_image_path == old_image_path:
                task.generated_image_path = None
            if old_image_url and task.generated_image_url == old_image_url:
                task.generated_image_url = None
            task.image_status = ""
        task.error_message = None
        self._log(
            "INFO",
            f"[{node_id}] PID={task.pid} row={task.row_index} 输入依赖已修正，旧节点状态已重置；"
            f"old_inputs={recorded_inputs}, expected_inputs={expected_inputs}, missing={missing}",
            task,
        )

    def repair_task_without_source_lookup(self, task: TaskItem) -> int:
        ensure_task_node_states(task, self.workflow_definition)
        self.migrate_legacy_task(task)
        hydrated = self.hydrate_legacy_video_url_for_download(task)
        stale = self.repair_stale_node_inputs(task)
        return hydrated + stale + self.recover_interrupted_running_nodes(task)

    def repair_archived_video_paths(self, task: TaskItem) -> int:
        """Recover downloaded video paths that were not persisted in state.

        A large batch can finish downloading files but fail to flush the
        corresponding task_state update to a network share before restart. The
        durable business truth is the validated archived mp4 on disk, so repair
        node/top-level state from the batch download directory before deciding a
        task still needs another download.
        """

        ensure_task_node_states(task, self.workflow_definition)
        repaired = 0
        for node in self.nodes():
            if str(node.get("node_type")) != NODE_TYPE_VIDEO:
                continue
            node_id = str(node.get("node_id") or "")
            state = get_node_state(task, node_id)
            video_url = str(state.get("output_video_url") or "").strip()
            if not video_url:
                continue

            existing_path = str(state.get("output_video_local_path") or "").strip()
            if not existing_path and node_id == "video_stage_1":
                existing_path = str(task.video_file_path or "").strip()
            if existing_path:
                ok, _message = validate_downloaded_file(existing_path, expected_kind="video")
                if ok:
                    state["output_video_local_path"] = existing_path
                    state["download_status"] = "DOWNLOADED"
                    state["status"] = NODE_STATUS_COMPLETED
                    state["auto_retrying"] = False
                    state["error_message"] = None
                    self._mirror_video_node_to_legacy_fields(task, node_id, state)
                    task.set_status(self.aggregate_task_status(task))
                    continue
                state["output_video_local_path"] = None

            found = find_existing_stage_video_path(
                self.config.video_download_root,
                task,
                node_id,
                self.config.group_by_owner,
                video_url=video_url,
                task_id=state.get("task_id"),
                archive_index=self._archived_video_index,
            )
            if not found:
                for fallback_root in [
                    Path.home() / "Downloads" / "Veo3下载视频",
                    Path.home() / "Downloads" / "Veo3涓嬭浇瑙嗛",
                ]:
                    found = find_existing_stage_video_path(
                        fallback_root,
                        task,
                        node_id,
                        self.config.group_by_owner,
                        video_url=video_url,
                        task_id=state.get("task_id"),
                    )
                    if found:
                        break
            if not found:
                continue

            state["output_video_local_path"] = str(found)
            state["download_status"] = "DOWNLOADED"
            state["last_download_error"] = None
            state["status"] = NODE_STATUS_COMPLETED
            state["auto_retrying"] = False
            state["error_message"] = None
            state["ended_at"] = state.get("ended_at") or now_text()
            self._mirror_video_node_to_legacy_fields(task, node_id, state)
            task.set_status(self.aggregate_task_status(task))
            repaired += 1

        if repaired:
            self._save_emit(task, force=True)
        return repaired

    def recover_interrupted_running_nodes(self, task: TaskItem) -> int:
        """Recover persisted RUNNING nodes left behind by an interrupted worker.

        RUNNING is only valid while a worker thread is actively inside the API
        call. Once a batch is reloaded or a new worker run starts, a persisted
        RUNNING node has no live thread attached. Leaving it as-is makes the
        scheduler skip it forever.
        """
        ensure_task_node_states(task, self.workflow_definition)
        recovered = 0
        for node in self.nodes():
            node_id = str(node.get("node_id") or "")
            state = task.node_states.get(node_id) or {}
            if str(state.get("status") or NODE_STATUS_PENDING) != NODE_STATUS_RUNNING:
                continue

            node_type = str(node.get("node_type") or "")
            if state.get("output_image_path") or state.get("output_image_url"):
                state["status"] = NODE_STATUS_COMPLETED
                state["auto_retrying"] = False
            elif state.get("output_video_url") or state.get("output_video_local_path"):
                state["status"] = NODE_STATUS_COMPLETED
                state["auto_retrying"] = False
            elif state.get("task_id"):
                state["status"] = NODE_STATUS_SUBMITTED
                state["error_message"] = None
            else:
                state["status"] = self.compute_runnable_status(task, node)
                if state["status"] == NODE_STATUS_READY:
                    state["error_message"] = None

            state["recovered_from_interrupted_running"] = True
            state["last_recovered_at"] = now_text()
            if not state.get("task_id") and node_type in {NODE_TYPE_IMAGE, NODE_TYPE_VIDEO}:
                state["started_at"] = None
            recovered += 1

        if recovered:
            task.set_status(self.aggregate_task_status(task))
            self._save_emit(task, force=True)
        return recovered

    # ------------------------------------------------------------------ #
    # Dependency resolution
    # ------------------------------------------------------------------ #
    def compute_runnable_status(self, task: TaskItem, node: dict) -> str:
        """Inspect the node's input refs and return one of:

        - NODE_STATUS_READY        all dependencies satisfied
        - NODE_STATUS_BLOCKED      a dependency is in a terminal non-COMPLETED state
        - NODE_STATUS_WAITING_INPUT some dependency is still PENDING / RUNNING
        """
        for ref in list(node.get("input_refs") or []):
            ref = str(ref)
            if ref == "source_image_1":
                if not (task.product_image_path or task.product_image_url):
                    return NODE_STATUS_WAITING_INPUT
                continue
            if not ref.endswith(".output_image"):
                return NODE_STATUS_WAITING_INPUT
            dep_node_id = ref.split(".", 1)[0]
            dep_state = get_node_state(task, dep_node_id)
            dep_status = str(dep_state.get("status") or NODE_STATUS_PENDING)
            if dep_status == NODE_STATUS_COMPLETED:
                if not (dep_state.get("output_image_path") or dep_state.get("output_image_url")):
                    return NODE_STATUS_BLOCKED
                continue
            if dep_status in {NODE_STATUS_FAILED, NODE_STATUS_SKIPPED, NODE_STATUS_BLOCKED, NODE_STATUS_WAITING_INPUT}:
                return NODE_STATUS_BLOCKED
            return NODE_STATUS_WAITING_INPUT
        return NODE_STATUS_READY

    def refresh_runnable_statuses(self, task: TaskItem) -> None:
        """Recompute READY / BLOCKED / WAITING_INPUT for all non-terminal nodes."""
        for node in self.nodes():
            state = task.node_states.get(node["node_id"])
            if not state:
                continue
            current = str(state.get("status") or NODE_STATUS_PENDING)
            if current in TERMINAL_NODE_STATUSES or current in {NODE_STATUS_RUNNING, NODE_STATUS_SUBMITTED, NODE_STATUS_POLLING}:
                continue
            new_status = self.compute_runnable_status(task, node)
            if new_status != current:
                state["status"] = new_status
                # Clear lingering BLOCKED error if we became READY again.
                if new_status == NODE_STATUS_READY:
                    state["error_message"] = None

    # ------------------------------------------------------------------ #
    # Image node execution
    # ------------------------------------------------------------------ #
    def run_image_node(self, task: TaskItem, node: dict) -> NodeRunOutcome:
        """Execute or advance a single image-generation node.

        Async image providers submit once and then return ``submitted`` so the
        BatchWorker polling loop can keep advancing the task without occupying
        an image generation worker thread.
        """
        if self._stop_event.is_set():
            return NodeRunOutcome(False, NODE_STATUS_PENDING, error_message="stop requested")
        self._wait_if_paused()

        node_id = str(node.get("node_id") or "")
        state = get_node_state(task, node_id)

        # Short-circuit if this node already has a usable output unless the
        # user explicitly asked to regenerate.
        existing_path = str(state.get("output_image_path") or "").strip()
        existing_url = str(state.get("output_image_url") or "").strip()
        if not self.regenerate_existing_images and (existing_path or existing_url):
            if existing_path:
                try:
                    existing_ok, _message = validate_downloaded_file(existing_path, expected_kind="image")
                    if not existing_ok:
                        existing_path = ""
                except OSError:
                    pass
            if existing_path or existing_url.startswith(("http://", "https://", "data:image")):
                state["status"] = NODE_STATUS_COMPLETED
                state["error_message"] = None
                state["auto_retrying"] = False
                self._mirror_image_node_to_legacy_fields(task, node_id, state)
                self._save_emit(task, force=False)
                return NodeRunOutcome(True, NODE_STATUS_COMPLETED, completed=True)

        # Check inputs.
        inputs, missing = resolve_node_input_images(task, node)
        if missing:
            state["status"] = NODE_STATUS_BLOCKED
            state["error_message"] = f"缺少输入图片：{', '.join(missing)}"
            self._save_emit(task, force=False)
            return NodeRunOutcome(False, NODE_STATUS_BLOCKED, error_message=state["error_message"], blocked=True)
        prompt = get_task_prompt_for_node(task, node)
        if not prompt:
            state["status"] = NODE_STATUS_WAITING_INPUT
            state["error_message"] = f"缺少提示词：{node.get('prompt_field') or node_id}"
            self._save_emit(task, force=False)
            return NodeRunOutcome(False, NODE_STATUS_WAITING_INPUT, error_message=state["error_message"])

        # Mark as RUNNING and emit once before the blocking API call so the
        # task table immediately shows "图生图进行中" instead of looking stuck.
        state["status"] = NODE_STATUS_RUNNING
        state["started_at"] = state.get("started_at") or now_text()
        state["error_message"] = None
        state["auto_retrying"] = False
        state["input_images"] = list(inputs)
        self._save_emit(task, force=False)
        self._log("INFO", f"[{node_id}] PID={task.pid} row={task.row_index} start image generation", task)

        # Resolve provider. Existing task_id chains keep their node-level
        # provider so polling continues against the platform that created the
        # task. Fresh submissions must use the current run config; otherwise
        # stale node state from a previous provider switch can keep submitting
        # to an unavailable old API.
        existing_task_id_for_provider = str(state.get("task_id") or "").strip()
        if existing_task_id_for_provider:
            image_provider_key = str(state.get("provider") or task.image_provider or self.config.image_provider)
            image_model_key = str(state.get("model_logical_key") or task.image_model_logical_key or self.config.image_model_logical_key)
        else:
            image_provider_key = str(self.config.image_provider or task.image_provider or state.get("provider") or "")
            image_model_key = str(self.config.image_model_logical_key or task.image_model_logical_key or state.get("model_logical_key") or "")
        try:
            provider = get_image_provider(image_provider_key)
        except Exception as exc:
            return self._fail_node(task, node_id, state, f"图生图平台不可用：{exc}")
        image_model_display = model_display_name(provider, image_model_key)
        try:
            image_api_model = str(provider.get_model_option(image_model_key).get("provider_value") or "")
        except Exception:
            image_api_model = ""
        state["provider"] = image_provider_key
        state["model_logical_key"] = image_model_key
        state["model_display"] = image_model_display
        task.image_provider = image_provider_key
        task.image_model_logical_key = image_model_key
        task.image_model_display = image_model_display
        self._log(
            "INFO",
            f"[{node_id}] PID={task.pid} row={task.row_index} image provider={image_provider_key} model={image_model_key} api_model={image_api_model} display={image_model_display}",
            task,
        )

        output_path = stage_image_output_path(self.config.image_assets_root, task, node_id)
        primary_input = inputs[0] if inputs else ""
        input_image_urls = self._resolved_input_urls_for_node(task, node)
        requires_reference_image = bool(node.get("input_refs"))
        if requires_reference_image and not input_image_urls and str(image_provider_key) == "hellobabygo_image":
            self._log(
                "WARNING",
                f"[{node_id}] PID={task.pid} row={task.row_index} no public product image URL resolved for HelloBabyGo image request; inputs={len(inputs)}",
                task,
            )

        try:
            existing_task_id = str(state.get("task_id") or "").strip()
            provider_is_async = bool(getattr(provider, "supports_async_image_tasks", False))
            if provider_is_async and existing_task_id and not self.regenerate_existing_images:
                poll_image_task_once = getattr(provider, "poll_image_task_once", None)
                if not callable(poll_image_task_once):
                    return self._fail_node(task, node_id, state, "Image provider does not implement async image polling")
                state["status"] = NODE_STATUS_POLLING
                state["error_message"] = None
                self._mirror_image_node_to_legacy_fields(task, node_id, state)
                self._save_emit(task, force=False)
                self._log("INFO", f"[{node_id}] PID={task.pid} row={task.row_index} poll image task_id={existing_task_id}", task)
                result = poll_image_task_once(
                    existing_task_id,
                    self.config.image_api_key,
                    extra_params={
                        "output_path": str(output_path),
                        "base_url": self.config.image_api_base_url,
                        "timeout": self.config.request_timeout_seconds,
                        "retry_count": self.config.retry_count,
                        "retry_interval_seconds": self.config.retry_interval_seconds,
                        "input_images": list(inputs),
                        "input_image_urls": input_image_urls,
                        "requires_reference_image": requires_reference_image,
                        "node_id": node_id,
                        "stop_event": self._stop_event,
                    },
                )
            elif provider_is_async:
                submit_image_task = getattr(provider, "submit_image_task", None)
                if not callable(submit_image_task):
                    return self._fail_node(task, node_id, state, "Image provider does not implement async image submission")
                result = submit_image_task(
                    product_image_path=primary_input,
                    prompt=prompt,
                    model_logical_key=image_model_key,
                    api_key=self.config.image_api_key,
                    extra_params={
                        "output_path": str(output_path),
                        "base_url": self.config.image_api_base_url,
                        "size": self.config.image_size,
                        "retry_count": self.config.retry_count,
                        "retry_interval_seconds": self.config.retry_interval_seconds,
                        "timeout": self.config.request_timeout_seconds,
                        "image_upload_api_url": self.config.image_upload_api_url,
                        "image_upload_api_key": self.config.image_upload_api_key,
                        "image_upload_file_field": self.config.image_upload_file_field,
                        "input_images": list(inputs),
                        "input_image_urls": input_image_urls,
                        "requires_reference_image": requires_reference_image,
                        "node_id": node_id,
                        "stop_event": self._stop_event,
                    },
                )
            else:
                poll_image_task = getattr(provider, "poll_image_task", None)
                if existing_task_id and not self.regenerate_existing_images and callable(poll_image_task):
                    self._record_image_node_task_id(task, node_id, state, existing_task_id, state.get("raw_response"))
                    self._log("INFO", f"[{node_id}] PID={task.pid} row={task.row_index} resume image polling, task_id={existing_task_id}", task)
                    result = poll_image_task(
                        existing_task_id,
                        self.config.image_api_key,
                        extra_params={
                            "output_path": str(output_path),
                            "base_url": self.config.image_api_base_url,
                            "timeout": self.config.request_timeout_seconds,
                            "image_upload_api_url": self.config.image_upload_api_url,
                            "image_upload_api_key": self.config.image_upload_api_key,
                            "image_upload_file_field": self.config.image_upload_file_field,
                            "poll_interval_seconds": self.config.poll_interval_seconds,
                            "max_poll_count": self.config.max_poll_count,
                            "stop_event": self._stop_event,
                        },
                    )
                else:
                    # The provider's generate_image signature only accepts a single
                    # product_image_path today. Extra inputs are passed via extra_params
                    # so multi-image-aware providers can pick them up; legacy providers
                    # ignore them safely.
                    result = provider.generate_image(
                        product_image_path=primary_input,
                        prompt=prompt,
                        model_logical_key=image_model_key,
                        api_key=self.config.image_api_key,
                        extra_params={
                            "output_path": str(output_path),
                            "base_url": self.config.image_api_base_url,
                            "size": self.config.image_size,
                            "retry_count": self.config.retry_count,
                            "retry_interval_seconds": self.config.retry_interval_seconds,
                            "timeout": self.config.request_timeout_seconds,
                            "poll_interval_seconds": self.config.poll_interval_seconds,
                            "max_poll_count": self.config.max_poll_count,
                            "image_upload_api_url": self.config.image_upload_api_url,
                            "image_upload_api_key": self.config.image_upload_api_key,
                            "image_upload_file_field": self.config.image_upload_file_field,
                            "input_images": list(inputs),
                            "input_image_urls": input_image_urls,
                            "requires_reference_image": requires_reference_image,
                            "node_id": node_id,
                            "stop_event": self._stop_event,
                            "on_task_id": lambda task_id, raw=None: self._record_image_node_task_id(task, node_id, state, task_id, raw),
                        },
                    )
        except Exception as exc:
            self._log("ERROR", f"[{node_id}] PID={task.pid} row={task.row_index} image provider raised: {exc}\n{traceback.format_exc()}", task)
            return self._fail_node(task, node_id, state, f"图生图调用异常：{exc}")

        state["raw_response"] = getattr(result, "raw_response", None)
        result_task_id = getattr(result, "task_id", None)
        if result_task_id:
            state["task_id"] = result_task_id
        if not getattr(result, "success", False):
            result_status = str(getattr(result, "status", "") or "").lower()
            if state.get("task_id") and result_status in _IMAGE_ACTIVE_PROVIDER_STATUSES:
                state["status"] = NODE_STATUS_SUBMITTED if result_status in {"queued", "pending", ""} else NODE_STATUS_POLLING
                state["provider_status"] = result_status or state.get("provider_status") or "queued"
                state["last_poll_error"] = getattr(result, "error_message", None)
                state["error_message"] = None
                self._save_prompt_to_disk(task, node, prompt)
                self._mirror_image_node_to_legacy_fields(task, node_id, state)
                self._save_emit(task, force=True)
                self._log("INFO", f"[{node_id}] PID={task.pid} row={task.row_index} image submitted task_id={state.get('task_id')} status={state.get('provider_status')}", task)
                return NodeRunOutcome(True, str(state["status"]), submitted=True)
            if state.get("task_id") and result_status in _IMAGE_FAILED_PROVIDER_STATUSES:
                state["provider_status"] = result_status
            if self._stop_event.is_set():
                if state.get("task_id"):
                    state["status"] = NODE_STATUS_POLLING
                    state["error_message"] = None
                    self._mirror_image_node_to_legacy_fields(task, node_id, state)
                    self._save_emit(task, force=True)
                    self._log("INFO", f"[{node_id}] PID={task.pid} row={task.row_index} image polling stopped before completion; task_id saved for resume", task)
                    return NodeRunOutcome(False, NODE_STATUS_POLLING, submitted=True)
                state["status"] = NODE_STATUS_PENDING
                state["error_message"] = None
                self._save_emit(task, force=False)
                return NodeRunOutcome(False, NODE_STATUS_PENDING, error_message="stop requested")
            err = getattr(result, "error_message", None) or "图生图失败"
            return self._fail_node(task, node_id, state, err)

        state["output_image_path"] = getattr(result, "image_path", None)
        state["output_image_url"] = getattr(result, "image_url", None)
        state["task_id"] = result_task_id or state.get("task_id")
        state["status"] = NODE_STATUS_COMPLETED
        state["ended_at"] = now_text()
        state["error_message"] = None
        state["auto_retrying"] = False
        self._save_prompt_to_disk(task, node, prompt)
        self._mirror_image_node_to_legacy_fields(task, node_id, state)
        # Per-node completion: rely on aggregate-status change detection in
        # _save_emit to force-save only when the whole task crosses a terminal
        # boundary. Avoids hammering the disk on every node terminal.
        self._save_emit(task, force=False)
        self._refresh_image_assets_metadata(task)
        self._log("INFO", f"[{node_id}] PID={task.pid} row={task.row_index} image done -> {state['output_image_path'] or state['output_image_url']}", task)
        return NodeRunOutcome(True, NODE_STATUS_COMPLETED, completed=True)

    def _handle_video_download_failure(
        self,
        task: TaskItem,
        node_id: str,
        state: dict,
        primary_exc: Exception,
        fallback_exc: Exception,
    ) -> NodeRunOutcome:
        max_attempts = max(1, int(getattr(self.config, "retry_count", 1) or 1))
        attempts = int(state.get("download_attempt_count") or 0)
        auto_retry_download = bool(getattr(self.config, "auto_retry_video_download", False))
        state["last_download_error"] = f"primary={primary_exc}; fallback={fallback_exc}"
        state["error_message"] = f"Video download failed: {state['last_download_error']}"

        if auto_retry_download and attempts >= max_attempts and state.get("task_id"):
            state["output_video_url"] = None
            state["download_status"] = "WAITING_NEW_URL"
            state["status"] = NODE_STATUS_SUBMITTED
            state["error_message"] = (
                f"Video download failed {attempts}/{max_attempts} times; "
                "repolling task_id for a fresh URL"
            )
            self._mirror_video_node_to_legacy_fields(task, node_id, state)
            self._save_emit(task, force=True)
            self._log(
                "WARNING",
                f"[{node_id}] PID={task.pid} row={task.row_index} download failed repeatedly; repoll task_id for a fresh URL",
                task,
            )
            return NodeRunOutcome(False, NODE_STATUS_SUBMITTED, submitted=True, error_message=state["error_message"])

        if auto_retry_download:
            state["download_status"] = "RETRYING"
            self._mirror_video_node_to_legacy_fields(task, node_id, state)
            self._save_emit(task, force=True)
            self._log(
                "WARNING",
                f"[{node_id}] PID={task.pid} row={task.row_index} download failed; will retry: {state['last_download_error']}",
                task,
            )
            return NodeRunOutcome(False, state.get("status") or NODE_STATUS_COMPLETED, error_message=state["error_message"])

        state["download_status"] = "FAILED"
        state["status"] = NODE_STATUS_FAILED
        self._mirror_video_node_to_legacy_fields(task, node_id, state)
        self._save_emit(task, force=True)
        return NodeRunOutcome(False, NODE_STATUS_FAILED, error_message=state["error_message"])

    # ------------------------------------------------------------------ #
    # Video node execution: submit + poll + download
    # ------------------------------------------------------------------ #
    def submit_video_node(self, task: TaskItem, node: dict) -> NodeRunOutcome:
        if self._stop_event.is_set():
            return NodeRunOutcome(False, NODE_STATUS_PENDING, error_message="stop requested")
        self._wait_if_paused()

        node_id = str(node.get("node_id") or "")
        state = get_node_state(task, node_id)

        # Already submitted? Just hand off to the poll loop.
        if state.get("task_id") and not state.get("output_video_url"):
            state["status"] = NODE_STATUS_SUBMITTED
            self._mirror_video_node_to_legacy_fields(task, node_id, state)
            self._save_emit(task, force=False)
            return NodeRunOutcome(True, NODE_STATUS_SUBMITTED, submitted=True)
        if state.get("output_video_url"):
            state["status"] = NODE_STATUS_COMPLETED
            self._mirror_video_node_to_legacy_fields(task, node_id, state)
            self._save_emit(task, force=False)
            return NodeRunOutcome(True, NODE_STATUS_COMPLETED, completed=True)

        inputs, missing = resolve_node_input_images(task, node)
        if missing:
            state["status"] = NODE_STATUS_BLOCKED
            state["error_message"] = f"缺少输入图片：{', '.join(missing)}"
            self._save_emit(task, force=False)
            return NodeRunOutcome(False, NODE_STATUS_BLOCKED, error_message=state["error_message"], blocked=True)
        prompt = get_task_prompt_for_node(task, node)
        if not prompt:
            state["status"] = NODE_STATUS_WAITING_INPUT
            state["error_message"] = f"缺少提示词：{node.get('prompt_field') or node_id}"
            self._save_emit(task, force=False)
            return NodeRunOutcome(False, NODE_STATUS_WAITING_INPUT, error_message=state["error_message"])

        # Mark RUNNING and emit once before the blocking submit call so the UI
        # does not stay on the previous task status.
        state["status"] = NODE_STATUS_RUNNING
        state["started_at"] = state.get("started_at") or now_text()
        state["error_message"] = None
        state["auto_retrying"] = False
        state["input_images"] = list(inputs)
        self._save_emit(task, force=False)
        self._log("INFO", f"[{node_id}] PID={task.pid} row={task.row_index} submit video task", task)

        existing_task_id_for_provider = str(state.get("task_id") or "").strip()
        if existing_task_id_for_provider:
            video_provider_key = str(state.get("provider") or task.video_provider or self.config.video_provider)
            video_model_key = str(state.get("model_logical_key") or task.video_model_logical_key or self.config.video_model_logical_key)
        else:
            video_provider_key = str(self.config.video_provider or task.video_provider or state.get("provider") or "")
            video_model_key = str(self.config.video_model_logical_key or task.video_model_logical_key or state.get("model_logical_key") or "")
        try:
            provider = get_video_provider(video_provider_key)
        except Exception as exc:
            return self._fail_node(task, node_id, state, f"图生视频平台不可用：{exc}")
        video_model_display = model_display_name(provider, video_model_key)
        state["provider"] = video_provider_key
        state["model_logical_key"] = video_model_key
        state["model_display"] = video_model_display
        try:
            video_api_model = str(provider.get_model_option(video_model_key).get("provider_value") or video_model_key)
        except Exception:
            video_api_model = video_model_key
        state["api_key_fingerprint"] = _api_key_fingerprint(self.config.video_api_key)
        task.video_provider = video_provider_key
        task.video_model_logical_key = video_model_key
        task.video_model_display = video_model_display

        image_source = self._preferred_video_image_source(inputs, provider, task=task, node=node, video_state=state)
        if not image_source:
            if state.get("remote_image_upload_error"):
                return self._fail_node(task, node_id, state, str(state.get("remote_image_upload_error")))
            return self._fail_node(task, node_id, state, "未找到可用的视频输入图（本地路径与远程 URL 均为空）")

        self._log(
            "INFO",
            f"[{node_id}] PID={task.pid} row={task.row_index} video provider={video_provider_key} "
            f"model={video_model_key} api_model={video_api_model} base={self.config.video_api_base_url} "
            f"key={state.get('api_key_fingerprint') or ''}",
            task,
        )

        try:
            submit_result = provider.submit_video_task(
                image_source=image_source,
                prompt=prompt,
                model_logical_key=video_model_key,
                api_key=self.config.video_api_key,
                extra_params={
                    "base_url": self.config.video_api_base_url,
                    "orientation": self.config.video_orientation,
                    "resolution": self.config.video_resolution,
                    "retry_count": self.config.retry_count,
                    "retry_interval_seconds": self.config.retry_interval_seconds,
                    "timeout": self.config.request_timeout_seconds,
                    "node_id": node_id,
                    "input_images": list(inputs),
                    "input_image_urls": self._resolved_input_urls_for_node(task, node),
                },
            )
        except Exception as exc:
            self._log("ERROR", f"[{node_id}] PID={task.pid} row={task.row_index} video submit raised: {exc}\n{traceback.format_exc()}", task)
            return self._fail_node(task, node_id, state, f"视频提交调用异常：{exc}")

        state["raw_response"] = getattr(submit_result, "raw_response", None)
        if not getattr(submit_result, "success", False):
            err = getattr(submit_result, "error_message", None) or "视频任务提交失败"
            return self._fail_node(task, node_id, state, err)

        video_url = getattr(submit_result, "video_url", None)
        task_id = getattr(submit_result, "task_id", None)
        if video_url and not task_id:
            # Some providers return the URL immediately. Mark completed.
            state["status"] = NODE_STATUS_COMPLETED
            state["output_video_url"] = video_url
            state["ended_at"] = now_text()
            state["auto_retrying"] = False
            self._mirror_video_node_to_legacy_fields(task, node_id, state)
            self._save_prompt_to_disk(task, node, prompt)
            self._save_emit(task, force=False)
            self._log("INFO", f"[{node_id}] PID={task.pid} row={task.row_index} video ready inline: {video_url}", task)
            return NodeRunOutcome(True, NODE_STATUS_COMPLETED, completed=True)
        if not task_id:
            return self._fail_node(task, node_id, state, "未获取到 video_task_id")

        state["status"] = NODE_STATUS_SUBMITTED
        state["task_id"] = task_id
        state["error_message"] = None
        self._mirror_video_node_to_legacy_fields(task, node_id, state)
        self._save_prompt_to_disk(task, node, prompt)
        # Force-save here: task_id is API-side state we MUST persist before
        # the next poll cycle, otherwise a crash loses the submission.
        self._save_emit(task, force=True)
        self._log("INFO", f"[{node_id}] PID={task.pid} row={task.row_index} video submitted task_id={task_id}", task)
        return NodeRunOutcome(True, NODE_STATUS_SUBMITTED, submitted=True)

    def poll_video_node(self, task: TaskItem, node: dict) -> NodeRunOutcome:
        if self._stop_event.is_set():
            return NodeRunOutcome(False, NODE_STATUS_POLLING, error_message="stop requested")
        node_id = str(node.get("node_id") or "")
        state = get_node_state(task, node_id)
        task_id = str(state.get("task_id") or "").strip()
        if not task_id:
            return NodeRunOutcome(False, NODE_STATUS_PENDING, error_message="task_id 为空")
        if state.get("output_video_url"):
            state["status"] = NODE_STATUS_COMPLETED
            state["auto_retrying"] = False
            self._save_emit(task, force=False)
            return NodeRunOutcome(True, NODE_STATUS_COMPLETED, completed=True)

        state["poll_count"] = int(state.get("poll_count") or 0) + 1
        state["status"] = NODE_STATUS_POLLING
        self._mirror_video_node_to_legacy_fields(task, node_id, state)
        # Skip the per-poll emit: the actual outcome below (completed / still
        # polling / failed) will fire a properly throttled save_emit. Avoids
        # 1 GUI signal per video per poll-cycle which used to swamp the queue.

        task.video_provider = task.video_provider or self.config.video_provider
        video_provider_key = str(state.get("provider") or task.video_provider or self.config.video_provider)
        provider = get_video_provider(video_provider_key)
        state["provider"] = video_provider_key
        try:
            poll_result = provider.poll_video_task(
                task_id=task_id,
                api_key=self.config.video_api_key,
                extra_params={
                    "base_url": self.config.video_api_base_url,
                    "timeout": self.config.request_timeout_seconds,
                },
            )
        except Exception as exc:
            self._log("ERROR", f"[{node_id}] PID={task.pid} row={task.row_index} poll raised: {exc}", task)
            return NodeRunOutcome(False, NODE_STATUS_POLLING, error_message=str(exc))

        state["raw_response"] = getattr(poll_result, "raw_response", None)
        if getattr(poll_result, "finished", False) and getattr(poll_result, "video_url", None):
            state["status"] = NODE_STATUS_COMPLETED
            state["output_video_url"] = poll_result.video_url
            state["ended_at"] = now_text()
            state["error_message"] = None
            state["auto_retrying"] = False
            state["terminal_failure_confirmed"] = False
            self._clear_poll_delay(state)
            self._mirror_video_node_to_legacy_fields(task, node_id, state)
            # Force-save: a successful poll result is the only place we
            # observe the video URL, and the BatchWorker will hand off to the
            # download executor right after this returns.
            self._save_emit(task, force=True)
            self._log("INFO", f"[{node_id}] PID={task.pid} row={task.row_index} video completed: {poll_result.video_url}", task)
            return NodeRunOutcome(True, NODE_STATUS_COMPLETED, completed=True)
        if getattr(poll_result, "retryable_failure", False):
            err = getattr(poll_result, "error_message", None) or "video polling transient error"
            state["status"] = NODE_STATUS_POLLING
            state["error_message"] = err
            state["terminal_failure_confirmed"] = False
            self._apply_poll_delay(state, err)
            self._mirror_video_node_to_legacy_fields(task, node_id, state)
            self._save_emit(task, force=False)
            return NodeRunOutcome(True, NODE_STATUS_POLLING, submitted=True)
        if getattr(poll_result, "failed", False):
            err = getattr(poll_result, "error_message", None) or "视频任务失败"
            if not _poll_result_is_explicit_terminal_failed(poll_result):
                state["status"] = NODE_STATUS_POLLING
                state["error_message"] = err
                state["terminal_failure_confirmed"] = False
                self._apply_poll_delay(state, err)
                self._mirror_video_node_to_legacy_fields(task, node_id, state)
                self._save_emit(task, force=False)
                self._log(
                    "WARNING",
                    f"[{node_id}] PID={task.pid} row={task.row_index} poll returned non-terminal error, keep polling existing task_id={task_id}: {err}",
                    task,
                )
                return NodeRunOutcome(True, NODE_STATUS_POLLING, submitted=True)
            state["terminal_failure_confirmed"] = True
            return self._fail_node(task, node_id, state, err)
        # still in progress — only emit if the per-task throttle allows it.
        # The status didn't change (POLLING → POLLING) so this is almost
        # always a no-op pass to the GUI.
        state["status"] = NODE_STATUS_POLLING
        state["terminal_failure_confirmed"] = False
        self._clear_poll_delay(state)
        self._mirror_video_node_to_legacy_fields(task, node_id, state)
        self._save_emit(task, force=False)
        return NodeRunOutcome(True, NODE_STATUS_POLLING, submitted=True)

    def _apply_poll_delay(self, state: dict, reason: Any) -> None:
        step = 5
        current = max(0, int(state.get("poll_rate_limit_backoff_seconds") or 0))
        backoff = current + step
        base_interval = max(0, int(getattr(self.config, "poll_interval_seconds", 0) or 0))
        state["poll_rate_limit_backoff_seconds"] = backoff
        state["next_poll_after_ts"] = time.time() + base_interval + backoff
        state["poll_delay_reason"] = str(reason or "")[:500]

    @staticmethod
    def _clear_poll_delay(state: dict) -> None:
        state["poll_rate_limit_backoff_seconds"] = 0
        state.pop("next_poll_after_ts", None)
        state.pop("poll_delay_reason", None)

    def download_node(self, task: TaskItem, node: dict) -> NodeRunOutcome:
        """Download the completed video for a video node (best effort).

        Falls back to a local Downloads/Veo3下载视频 path if the primary archive
        root is unreachable, matching the legacy worker behaviour.
        """
        node_id = str(node.get("node_id") or "")
        state = get_node_state(task, node_id)
        video_url = str(state.get("output_video_url") or "").strip()
        if not video_url:
            return NodeRunOutcome(False, state.get("status") or "", error_message="尚无视频链接，无法下载")
        existing_video_path = str(state.get("output_video_local_path") or "").strip()
        if existing_video_path:
            ok, message = validate_downloaded_file(existing_video_path, expected_kind="video")
            if ok:
                state["download_status"] = "DOWNLOADED"
                state["auto_retrying"] = False
                self._mirror_video_node_to_legacy_fields(task, node_id, state)
                self._save_emit(task, force=True)
                return NodeRunOutcome(True, NODE_STATUS_COMPLETED, completed=True)
            state["output_video_local_path"] = None
            state["error_message"] = f"已归档视频校验失败，准备重新下载：{message}"

        archived = find_existing_stage_video_path(
            self.config.video_download_root,
            task,
            node_id,
            self.config.group_by_owner,
            video_url=video_url,
            task_id=state.get("task_id"),
        )
        if archived:
            state["output_video_local_path"] = str(archived)
            state["download_status"] = "DOWNLOADED"
            state["error_message"] = None
            state["auto_retrying"] = False
            state["ended_at"] = state.get("ended_at") or now_text()
            self._mirror_video_node_to_legacy_fields(task, node_id, state)
            self._save_emit(task, force=True)
            self._log("INFO", f"[{node_id}] PID={task.pid} row={task.row_index} found archived video, no re-download needed: {archived}", task)
            return NodeRunOutcome(True, NODE_STATUS_COMPLETED, completed=True)

        state["download_status"] = "DOWNLOADING"
        state["download_attempt_count"] = int(state.get("download_attempt_count") or 0) + 1
        state["last_download_time"] = now_text()
        self._mirror_video_node_to_legacy_fields(task, node_id, state)
        self._save_emit(task, force=False)

        save_path = stage_video_output_path(
            self.config.video_download_root,
            task,
            node_id,
            self.config.group_by_owner,
            video_url=video_url,
            task_id=state.get("task_id"),
        )
        try:
            path = self._download_video_with_provider(task, state, video_url, save_path)
        except Exception as primary_exc:
            fallback_root = Path.home() / "Downloads" / "Veo3下载视频"
            try:
                fallback_path = stage_video_output_path(
                    fallback_root,
                    task,
                    node_id,
                    self.config.group_by_owner,
                    video_url=video_url,
                    task_id=state.get("task_id"),
                )
                path = self._download_video_with_provider(task, state, video_url, fallback_path)
                self._log("INFO", f"[{node_id}] PID={task.pid} row={task.row_index} 主目录下载失败，已存至本地兜底：{path}", task)
            except Exception as fallback_exc:
                state["error_message"] = f"视频下载失败：primary={primary_exc}; fallback={fallback_exc}"
                return self._handle_video_download_failure(task, node_id, state, primary_exc, fallback_exc)

        state["output_video_local_path"] = path
        state["download_status"] = "DOWNLOADED"
        state["error_message"] = None
        state["auto_retrying"] = False
        state["ended_at"] = state.get("ended_at") or now_text()
        self._save_video_metadata_to_disk(task, node, state)
        self._mirror_video_node_to_legacy_fields(task, node_id, state)
        # Force-save: file path is durable proof of success and worth flushing.
        self._save_emit(task, force=True)
        self._log("INFO", f"[{node_id}] PID={task.pid} row={task.row_index} 视频已下载至：{path}", task)
        return NodeRunOutcome(True, NODE_STATUS_COMPLETED, completed=True)

    # ------------------------------------------------------------------ #
    # Queries used by BatchWorker's run loop
    # ------------------------------------------------------------------ #
    def find_ready_image_nodes(self, task: TaskItem) -> list[dict]:
        out: list[dict] = []
        for node in self.nodes():
            if str(node.get("node_type")) != NODE_TYPE_IMAGE or not bool(node.get("enabled", True)):
                continue
            state = task.node_states.get(node["node_id"]) or {}
            current = str(state.get("status") or NODE_STATUS_PENDING)
            if current in {NODE_STATUS_BLOCKED, NODE_STATUS_WAITING_INPUT} and state.get("error_message"):
                continue
            if current in TERMINAL_NODE_STATUSES:
                # Allow READY-only retries: terminal SKIPPED/BLOCKED nodes are
                # re-evaluated on every iteration because their dependencies
                # may have unblocked.
                if current not in {NODE_STATUS_BLOCKED, NODE_STATUS_WAITING_INPUT}:
                    continue
            if current in {NODE_STATUS_SUBMITTED, NODE_STATUS_POLLING}:
                continue
            if current in {NODE_STATUS_RUNNING}:
                continue
            if self.compute_runnable_status(task, node) == NODE_STATUS_READY:
                out.append(node)
        return out

    def find_ready_video_nodes(self, task: TaskItem) -> list[dict]:
        out: list[dict] = []
        for node in self.nodes():
            if str(node.get("node_type")) != NODE_TYPE_VIDEO or not bool(node.get("enabled", True)):
                continue
            state = task.node_states.get(node["node_id"]) or {}
            current = str(state.get("status") or NODE_STATUS_PENDING)
            if current in {NODE_STATUS_BLOCKED, NODE_STATUS_WAITING_INPUT} and state.get("error_message"):
                continue
            if current in {NODE_STATUS_COMPLETED, NODE_STATUS_SKIPPED}:
                continue
            if current in {NODE_STATUS_RUNNING, NODE_STATUS_SUBMITTED, NODE_STATUS_POLLING}:
                continue
            if current == NODE_STATUS_FAILED:
                # Don't auto-retry failed nodes here; user must explicitly retry.
                continue
            if self.compute_runnable_status(task, node) == NODE_STATUS_READY:
                out.append(node)
        return out

    def find_polling_image_nodes(self, task: TaskItem) -> list[dict]:
        out: list[dict] = []
        for node in self.nodes():
            if str(node.get("node_type")) != NODE_TYPE_IMAGE:
                continue
            state = task.node_states.get(node["node_id"]) or {}
            if not state.get("task_id"):
                continue
            if state.get("output_image_path") or state.get("output_image_url"):
                continue
            if str(state.get("status") or "") in {NODE_STATUS_SUBMITTED, NODE_STATUS_POLLING, NODE_STATUS_RUNNING}:
                out.append(node)
        return out

    def find_polling_nodes(self, task: TaskItem) -> list[dict]:
        out: list[dict] = []
        now = time.time()
        for node in self.nodes():
            if str(node.get("node_type")) != NODE_TYPE_VIDEO:
                continue
            state = task.node_states.get(node["node_id"]) or {}
            if not state.get("task_id") or state.get("output_video_url"):
                continue
            try:
                next_poll_after = float(state.get("next_poll_after_ts") or 0)
            except (TypeError, ValueError):
                next_poll_after = 0
            if next_poll_after > now:
                continue
            if str(state.get("status") or "") in {NODE_STATUS_SUBMITTED, NODE_STATUS_POLLING, NODE_STATUS_RUNNING}:
                out.append(node)
        return out

    def find_downloadable_nodes(self, task: TaskItem) -> list[dict]:
        out: list[dict] = []
        for node in self.nodes():
            if str(node.get("node_type")) != NODE_TYPE_VIDEO:
                continue
            state = task.node_states.get(node["node_id"]) or {}
            if state.get("output_video_url") and not state.get("output_video_local_path"):
                out.append(node)
        return out

    def has_actionable_nodes(self, task: TaskItem) -> bool:
        return bool(
            self.find_ready_image_nodes(task)
            or self.find_ready_video_nodes(task)
            or self.find_downloadable_nodes(task)
        )

    def aggregate_task_status(self, task: TaskItem) -> str:
        """Roll per-node states up into the overall task.status field.

        Used to keep the existing GUI / stats / batch_manager.update_stats
        working without changes during this transition.
        """
        self.cleanup_inactive_retry_markers(task)
        expected_nodes = self._expected_nodes_for_task(task)
        statuses = [
            str((task.node_states.get(node["node_id"]) or {}).get("status") or NODE_STATUS_PENDING)
            for node in expected_nodes
        ]
        if not statuses:
            return task.status
        if any(s == NODE_STATUS_FAILED for s in statuses):
            if any(
                str((task.node_states.get(node["node_id"]) or {}).get("status") or "") == NODE_STATUS_FAILED
                and self._can_auto_retry_node(node, task.node_states.get(node["node_id"]) or {})
                for node in expected_nodes
            ):
                return TaskStatus.AUTO_RETRYING
            return TaskStatus.FAILED_RETRY_EXHAUSTED

        if any(
            bool((task.node_states.get(node["node_id"]) or {}).get("auto_retrying"))
            and str((task.node_states.get(node["node_id"]) or {}).get("status") or "") != NODE_STATUS_COMPLETED
            for node in expected_nodes
        ):
            return TaskStatus.AUTO_RETRYING

        if any(
            str((task.node_states.get(node["node_id"]) or {}).get("download_status") or "") == "DOWNLOADING"
            for node in expected_nodes
        ):
            return TaskStatus.VIDEO_DOWNLOADING

        waiting_video = False
        waiting_image = False
        for node in expected_nodes:
            state = task.node_states.get(node["node_id"]) or {}
            node_status = str(state.get("status") or NODE_STATUS_PENDING)
            if node_status not in {NODE_STATUS_POLLING, NODE_STATUS_SUBMITTED}:
                continue
            if str(node.get("node_type")) == NODE_TYPE_VIDEO:
                waiting_video = True
            elif str(node.get("node_type")) == NODE_TYPE_IMAGE:
                waiting_image = True
        if waiting_video:
            return TaskStatus.VIDEO_POLLING
        if waiting_image:
            return TaskStatus.GENERATING_IMAGE
        if any(s == NODE_STATUS_RUNNING for s in statuses):
            running_video = any(
                str(node.get("node_type")) == NODE_TYPE_VIDEO
                and str((task.node_states.get(node["node_id"]) or {}).get("status") or "") == NODE_STATUS_RUNNING
                for node in self.nodes()
            )
            if running_video:
                return TaskStatus.VIDEO_SUBMITTING
            return TaskStatus.GENERATING_IMAGE

        expected_video_nodes = [
            node for node in expected_nodes if str(node.get("node_type")) == NODE_TYPE_VIDEO
        ]
        if expected_video_nodes:
            if any(self._video_node_needs_download(task, node) for node in expected_video_nodes):
                return TaskStatus.VIDEO_DOWNLOAD_PENDING
            if all(self._video_node_archived(task, node) for node in expected_video_nodes):
                return TaskStatus.VIDEO_DOWNLOADED

        if any(s in {NODE_STATUS_BLOCKED, NODE_STATUS_WAITING_INPUT} for s in statuses):
            return TaskStatus.WORKFLOW_WAITING_INPUT

        if all(s == NODE_STATUS_SKIPPED for s in statuses):
            return TaskStatus.SKIPPED_EMPTY_PROMPT

        if expected_video_nodes:
            return TaskStatus.WORKFLOW_RUNNING

        if all(s == NODE_STATUS_COMPLETED for s in statuses):
            return TaskStatus.IMAGE_DONE
        if any(s in {NODE_STATUS_COMPLETED, NODE_STATUS_READY, NODE_STATUS_PENDING} for s in statuses):
            return TaskStatus.WORKFLOW_RUNNING
        return TaskStatus.PENDING

    def cleanup_inactive_retry_markers(self, task: TaskItem) -> None:
        """Clear stale retry markers once a node has made forward progress.

        ``task_logs`` intentionally keep the full history, including old image
        submit failures. These marker fields are different: they drive the
        current UI/export "last retry reason" surface and should not keep
        showing an image failure after that image node has completed and the
        task has moved on to video polling.
        """

        active_retry = False
        progress_statuses = {
            NODE_STATUS_RUNNING,
            NODE_STATUS_SUBMITTED,
            NODE_STATUS_POLLING,
            NODE_STATUS_COMPLETED,
        }
        for node in self._expected_nodes_for_task(task):
            state = task.node_states.get(str(node.get("node_id") or "")) or {}
            status = str(state.get("status") or NODE_STATUS_PENDING)
            if bool(state.get("auto_retrying")) and status in {NODE_STATUS_PENDING, NODE_STATUS_READY}:
                active_retry = True
                continue
            if status in progress_statuses:
                state["auto_retrying"] = False
                state["last_auto_retry_reason"] = None
                continue
            if bool(state.get("auto_retrying")) and status != NODE_STATUS_COMPLETED:
                active_retry = True

        if not active_retry:
            task.last_auto_retry_stage = None
            task.last_auto_retry_reason = None

    def _expected_nodes_for_task(self, task: TaskItem) -> list[dict]:
        expected: list[dict] = []
        for node in self.nodes():
            if not bool(node.get("enabled", True)):
                continue
            node_id = str(node.get("node_id") or "")
            state = task.node_states.get(node_id) or {}
            prompt = get_task_prompt_for_node(task, node)
            status = str(state.get("status") or NODE_STATUS_PENDING)
            has_runtime_state = bool(
                state.get("task_id")
                or state.get("output_image_path")
                or state.get("output_image_url")
                or state.get("output_video_url")
                or state.get("output_video_local_path")
                or state.get("started_at")
                or status in {NODE_STATUS_RUNNING, NODE_STATUS_SUBMITTED, NODE_STATUS_POLLING, NODE_STATUS_COMPLETED, NODE_STATUS_FAILED}
            )
            if prompt or has_runtime_state:
                expected.append(node)
        return expected

    def _video_node_archived(self, task: TaskItem, node: dict) -> bool:
        state = task.node_states.get(node["node_id"]) or {}
        return bool(state.get("output_video_local_path"))

    def _video_node_needs_download(self, task: TaskItem, node: dict) -> bool:
        state = task.node_states.get(node["node_id"]) or {}
        return bool(state.get("output_video_url") and not state.get("output_video_local_path"))

    def _can_auto_retry_node(self, node: dict, state: dict) -> bool:
        if not bool(node.get("allow_retry", True)):
            return False
        node_type = str(node.get("node_type") or "")
        if node_type == NODE_TYPE_IMAGE and not bool(getattr(self.config, "auto_retry_image_nodes", False)):
            return False
        if node_type == NODE_TYPE_VIDEO and _is_non_retryable_auth_or_quota_error(state.get("error_message")):
            return False
        if node_type == NODE_TYPE_VIDEO:
            if not state.get("task_id"):
                retry_count = int(state.get("auto_retry_count") or 0)
                return retry_count < max(0, int(getattr(self.config, "retry_count", 0) or 0))
            if not _video_state_has_confirmed_terminal_failure(state):
                return False
            if not bool(getattr(self.config, "auto_retry_failed_workflow_enabled", False)):
                return False
            if not bool(getattr(self.config, "auto_retry_video_nodes", False)):
                return False
        elif not bool(getattr(self.config, "auto_retry_failed_workflow_enabled", False)):
            return False
        retry_count = int(state.get("auto_retry_count") or 0)
        return retry_count < max(0, int(getattr(self.config, "retry_count", 0) or 0))

    def has_default_video_submit_retry_node(self, task: TaskItem) -> bool:
        for node in self.nodes():
            if str(node.get("node_type") or "") != NODE_TYPE_VIDEO:
                continue
            node_id = str(node.get("node_id") or "")
            state = task.node_states.get(node_id) or {}
            if str(state.get("status") or "") != NODE_STATUS_FAILED:
                continue
            if state.get("task_id"):
                continue
            if self._can_auto_retry_node(node, state):
                return True
        return False

    def reset_failed_nodes_for_auto_retry(self, task: TaskItem) -> int:
        """Reset retryable FAILED nodes so the scheduler can requeue them."""
        reset_count = 0
        for node in self.nodes():
            node_id = str(node.get("node_id") or "")
            state = task.node_states.get(node_id) or {}
            if str(state.get("status") or "") != NODE_STATUS_FAILED:
                continue
            if (
                str(node.get("node_type") or "") == NODE_TYPE_IMAGE
                and self.complete_image_node_from_existing_output(task, node_id, state)
            ):
                self.refresh_runnable_statuses(task)
                task.set_status(self.aggregate_task_status(task))
                reset_count += 1
                self._save_emit(task, force=True)
                self._log(
                    "INFO",
                    f"[AUTO_RETRY] PID={task.pid} row={task.row_index} node={node_id} 已检测到已有图片输出，保留图片并跳过重新生图",
                    task,
                )
                continue
            if not self._can_auto_retry_node(node, state):
                continue
            previous_retry_count = int(state.get("auto_retry_count") or 0)
            reason = state.get("error_message") or task.error_message or "node failed"
            outcome = self.manual_reset_node(task, node_id)
            if not outcome.success:
                continue
            state = task.node_states.get(node_id) or {}
            state["auto_retry_count"] = previous_retry_count + 1
            state["last_auto_retry_time"] = now_text()
            state["last_auto_retry_reason"] = reason
            state["auto_retrying"] = True
            state["status"] = self.compute_runnable_status(task, node)
            task.auto_retry_count = max(int(task.auto_retry_count or 0), previous_retry_count + 1)
            task.last_auto_retry_time = state["last_auto_retry_time"]
            task.last_auto_retry_stage = node_id
            task.last_auto_retry_reason = reason
            task.error_message = None
            task.set_status(TaskStatus.AUTO_RETRYING)
            reset_count += 1
            self._save_emit(task, force=True)
            self._log(
                "INFO",
                f"[AUTO_RETRY] PID={task.pid} row={task.row_index} node={node_id} retry={state['auto_retry_count']}/{self.config.retry_count} "
                f"next_provider={state.get('provider') or ''} next_model={state.get('model_logical_key') or ''} previous_reason={reason}",
                task,
            )
        return reset_count

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #
    def _refresh_reset_node_provider_from_config(self, task: TaskItem, node: dict, state: dict) -> None:
        """Refresh provider/model after a node reset.

        Runtime API switching updates the worker config and task defaults, but
        failed node states may still carry the old provider/model. Execution
        intentionally gives node-level values priority to preserve submitted
        task polling chains, so retry resets must stamp the current config onto
        nodes that will be submitted again.
        """
        node_type = str(node.get("node_type") or "")
        if node_type == NODE_TYPE_IMAGE:
            provider_key = str(self.config.image_provider or task.image_provider or "").strip()
            model_key = str(self.config.image_model_logical_key or task.image_model_logical_key or "").strip()
            display_name = ""
            if provider_key and model_key:
                try:
                    display_name = model_display_name(get_image_provider(provider_key), model_key)
                except Exception:
                    display_name = str(task.image_model_display or "")
            if provider_key:
                state["provider"] = provider_key
                task.image_provider = provider_key
            if model_key:
                state["model_logical_key"] = model_key
                task.image_model_logical_key = model_key
            if display_name:
                state["model_display"] = display_name
                task.image_model_display = display_name
            return

        if node_type == NODE_TYPE_VIDEO:
            provider_key = str(self.config.video_provider or task.video_provider or "").strip()
            model_key = str(self.config.video_model_logical_key or task.video_model_logical_key or "").strip()
            display_name = ""
            if provider_key and model_key:
                try:
                    display_name = model_display_name(get_video_provider(provider_key), model_key)
                except Exception:
                    display_name = str(task.video_model_display or "")
            if provider_key:
                state["provider"] = provider_key
                task.video_provider = provider_key
            if model_key:
                state["model_logical_key"] = model_key
                task.video_model_logical_key = model_key
            if display_name:
                state["model_display"] = display_name
                task.video_model_display = display_name

    def complete_image_node_from_existing_output(self, task: TaskItem, node_id: str, state: dict | None = None) -> bool:
        """Promote an image node with an existing output back to COMPLETED.

        Failed-video retries must not clear already-paid image outputs. When a
        failed or stale image node still has a generated path/URL, preserve it
        and let downstream video nodes retry from that image.
        """
        node = self.get_node(node_id)
        if not node or str(node.get("node_type") or "") != NODE_TYPE_IMAGE:
            return False
        state = state if isinstance(state, dict) else get_node_state(task, node_id)
        if not state:
            return False
        output_path = str(state.get("output_image_path") or "").strip()
        output_url = str(state.get("output_image_url") or "").strip()
        if node_id == "image_stage_1":
            output_path = output_path or str(task.generated_image_path or "").strip()
            output_url = output_url or str(task.generated_image_url or "").strip()
        if not (output_path or output_url):
            return False
        if output_path:
            state["output_image_path"] = output_path
        if output_url:
            state["output_image_url"] = output_url
        state["status"] = NODE_STATUS_COMPLETED
        state["error_message"] = None
        state["auto_retrying"] = False
        state["ended_at"] = state.get("ended_at") or now_text()
        self._mirror_image_node_to_legacy_fields(task, node_id, state)
        return True

    def _wait_if_paused(self) -> None:
        while not self._pause_event.is_set() and not self._stop_event.is_set():
            time.sleep(0.2)

    def _log(self, level: str, message: str, task: Optional[TaskItem] = None) -> None:
        if self._log_callback is None:
            if task is not None:
                append_task_log(task, level, None, message)
            return
        try:
            self._log_callback(level, message, task)
        except Exception:
            pass

    def _save_emit(self, task: TaskItem, force: bool = False) -> None:
        # Keep aggregated task.status in sync so the rest of the app keeps
        # rendering the right thing.
        new_status = self.aggregate_task_status(task)
        status_changed = bool(new_status and task.status != new_status)
        if status_changed:
            task.set_status(new_status)
        if self._save_emit_callback is None:
            return
        # Throttle non-forced emits per task to avoid flooding the Qt signal
        # queue and the disk on tight loops like video polling. Forced emits
        # (terminal transitions) and any time the aggregate status flips go
        # through immediately so the GUI never misses a meaningful update.
        if not force and not status_changed:
            key = str(getattr(task, "task_uid", "") or f"{task.pid}::row_{task.row_index}")
            now = time.monotonic()
            with self._emit_guard:
                last = self._last_save_emit_monotonic.get(key, 0.0)
                if now - last < self._save_emit_min_interval_seconds:
                    return
                self._last_save_emit_monotonic[key] = now
        try:
            self._save_emit_callback(task, force)
        except Exception:
            pass

    def _fail_node(self, task: TaskItem, node_id: str, state: dict, error: str, final_status_hint: str | None = None) -> NodeRunOutcome:
        state["status"] = NODE_STATUS_FAILED
        state["error_message"] = error
        state["ended_at"] = now_text()
        if final_status_hint:
            task.error_message = error
            task.set_status(final_status_hint, error)
        else:
            task.error_message = error
        self._mirror_video_node_to_legacy_fields(task, node_id, state)
        self._mirror_image_node_to_legacy_fields(task, node_id, state)
        # Force-save: failures are durable state worth flushing so the user
        # always sees the failure even if the worker crashes immediately after.
        # The signal-flood mitigation lives in _save_emit's per-task throttle;
        # forced calls bypass it on purpose.
        self._save_emit(task, force=True)
        self._log("ERROR", f"[{node_id}] PID={task.pid} row={task.row_index} {error}", task)
        return NodeRunOutcome(False, NODE_STATUS_FAILED, error_message=error)

    def _record_image_node_task_id(self, task: TaskItem, node_id: str, state: dict, task_id: str | None, raw_response: Any = None) -> None:
        """Persist async image task_id immediately after provider submission.

        Image providers may keep polling internally for minutes. If the app is
        closed during that wait, losing the provider-side task_id would force a
        paid re-submit. This callback records the task_id before the blocking
        provider call returns.
        """
        task_id = str(task_id or "").strip()
        if not task_id:
            return
        is_new = state.get("task_id") != task_id
        state["task_id"] = task_id
        state["status"] = NODE_STATUS_POLLING
        state["error_message"] = None
        if raw_response is not None:
            state["raw_response"] = raw_response
        self._mirror_image_node_to_legacy_fields(task, node_id, state)
        self._save_emit(task, force=True)
        if is_new:
            self._log("INFO", f"[{node_id}] PID={task.pid} row={task.row_index} image task_id saved: {task_id}", task)

    def _mirror_image_node_to_legacy_fields(self, task: TaskItem, node_id: str, state: dict) -> None:
        """Keep the old ``generated_image_*`` / ``image_*`` fields populated.

        The GUI table, manual-resume code paths and exports still read them, so
        the v1 → v2 transition needs to be invisible to those consumers.
        """
        if node_id != "image_stage_1":
            return
        path = state.get("output_image_path")
        url = state.get("output_image_url")
        if path:
            task.generated_image_path = path
        if url:
            task.generated_image_url = url
        task_id = state.get("task_id")
        if task_id:
            task.image_task_id = task_id
        raw = state.get("raw_response")
        if raw is not None:
            task.image_raw_response = raw
        node_status = state.get("status")
        if node_status in {NODE_STATUS_RUNNING, NODE_STATUS_SUBMITTED, NODE_STATUS_POLLING}:
            task.image_status = TaskStatus.GENERATING_IMAGE
        if node_status == NODE_STATUS_COMPLETED:
            task.image_status = TaskStatus.IMAGE_DONE

    def _mirror_video_node_to_legacy_fields(self, task: TaskItem, node_id: str, state: dict) -> None:
        if node_id != "video_stage_1":
            return
        if state.get("task_id"):
            task.video_task_id = state.get("task_id")
        if state.get("output_video_url"):
            task.video_url = state.get("output_video_url")
        if state.get("output_video_local_path"):
            task.video_file_path = state.get("output_video_local_path")
        if state.get("download_status"):
            task.video_download_status = str(state.get("download_status") or "")
        if state.get("download_attempt_count") is not None:
            task.video_download_attempt_count = int(state.get("download_attempt_count") or 0)
        if state.get("last_download_time"):
            task.last_video_download_time = state.get("last_download_time")
        if state.get("last_download_error"):
            task.last_video_download_error = state.get("last_download_error")
        if state.get("poll_count") is not None:
            task.video_poll_count = int(state.get("poll_count") or 0)
        raw = state.get("raw_response")
        if raw is not None:
            task.video_raw_response = raw
        node_status = state.get("status")
        if node_status == NODE_STATUS_SUBMITTED:
            task.video_status = TaskStatus.VIDEO_SUBMITTED
        elif node_status == NODE_STATUS_POLLING:
            task.video_status = TaskStatus.VIDEO_POLLING
        elif node_status == NODE_STATUS_COMPLETED:
            task.video_status = TaskStatus.COMPLETED

    def _save_prompt_to_disk(self, task: TaskItem, node: dict, prompt: str) -> None:
        if not self.config.auto_save_image_assets:
            return
        try:
            target = stage_prompt_txt_path(self.config.image_assets_root, task, str(node.get("prompt_field") or node.get("node_id") or ""))
            target.write_text(prompt or "", encoding="utf-8")
        except Exception:
            pass

    def _save_video_metadata_to_disk(self, task: TaskItem, node: dict, state: dict) -> None:
        if not self.config.auto_save_image_assets:
            return
        try:
            target = stage_video_metadata_path(self.config.image_assets_root, task, str(node.get("node_id") or ""))
            payload = {
                "node_id": node.get("node_id"),
                "node_name": node.get("node_name"),
                "prompt_field": node.get("prompt_field"),
                "input_images": list(state.get("input_images") or []),
                "output_video_url": state.get("output_video_url"),
                "output_video_local_path": state.get("output_video_local_path"),
                "task_id": state.get("task_id"),
                "poll_count": state.get("poll_count"),
                "started_at": state.get("started_at"),
                "ended_at": state.get("ended_at"),
                "task_uid": task.task_uid,
                "batch_id": task.batch_id,
                "pid": task.pid,
                "row_index": task.row_index,
                "owner": task.owner,
            }
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _refresh_image_assets_metadata(self, task: TaskItem) -> None:
        if not self.config.auto_save_image_assets:
            return
        try:
            save_image_task_assets(task, self.config.image_assets_root)
        except Exception:
            pass

    def _resolved_input_urls_for_node(self, task: TaskItem, node: dict) -> list[str]:
        """Return HTTP/data URL companions for a node's resolved inputs.

        The workflow resolver intentionally prefers local files for legacy
        providers. Some providers, including HelloBabyGo image generation,
        work better with public URLs, so we pass these companion URLs through
        extra_params without changing the generic resolver semantics.
        """
        urls: list[str] = []

        def add(value: Any) -> None:
            text = str(value or "").strip()
            if not text:
                return
            if text.lower().startswith(("http://", "https://", "data:image")):
                candidate = text
            else:
                candidate = path_to_http_url(text, self.config)
            if candidate and candidate not in urls:
                urls.append(candidate)

        for ref in list(node.get("input_refs") or []):
            ref = str(ref)
            if ref == "source_image_1":
                add(getattr(task, "product_image_url", "") or getattr(task, "product_image_path", ""))
                continue
            if ref.endswith(".output_image"):
                dep_node_id = ref.split(".", 1)[0]
                dep_state = get_node_state(task, dep_node_id)
                add(dep_state.get("output_image_url") or dep_state.get("output_image_path"))

        state = get_node_state(task, str(node.get("node_id") or ""))
        for value in list(state.get("manual_input_urls") or []):
            add(value)
        return urls

    def _download_video_with_provider(self, task: TaskItem, state: dict, video_url: str, save_path: Path) -> str:
        task_id = str(state.get("task_id") or getattr(task, "video_task_id", "") or "").strip()
        provider = get_video_provider(task.video_provider or self.config.video_provider)
        downloader = getattr(provider, "download_video_content", None)
        if callable(downloader) and task_id and str(video_url or "").rstrip("/").endswith("/content"):
            return str(
                downloader(
                    task_id=task_id,
                    api_key=self.config.video_api_key,
                    output_path=save_path,
                    extra_params={
                        "base_url": self.config.video_api_base_url,
                        "timeout": max(180, int(self.config.request_timeout_seconds)),
                    },
                )
            )
        path, _ = download_video_to_path(
            video_url,
            save_path,
            timeout=max(180, int(self.config.request_timeout_seconds)),
            retry_count=max(1, int(self.config.retry_count)),
            retry_interval_seconds=max(1, int(self.config.retry_interval_seconds)),
        )
        return path

    def _preferred_video_image_source(
        self,
        inputs: list[str],
        video_provider,
        *,
        task: TaskItem | None = None,
        node: dict | None = None,
        video_state: dict | None = None,
    ) -> str:
        """Pick the best image_source for video submission.

        Mirrors BatchWorker._preferred_video_image_source but operates over the
        resolved input list. Provider may set ``requires_remote_image_url`` to
        force fallback to remote URLs.
        """
        if not inputs:
            return ""
        requires_remote = bool(getattr(video_provider, "requires_remote_image_url", False))
        if requires_remote:
            # Prefer an existing public URL. If the image stage only produced
            # a local/netdisk file, upload it to OSS so remote-only providers
            # such as JimmyAI can fetch it.
            for value in inputs:
                v = str(value or "").strip()
                if v.lower().startswith(("http://", "https://")):
                    return v
            for value in inputs:
                v = str(value or "").strip()
                if not v or v.lower().startswith("data:image"):
                    continue
                uploaded = self._upload_local_video_input_for_remote_provider(
                    v,
                    task=task,
                    node=node,
                    video_state=video_state,
                )
                if uploaded:
                    return uploaded
            if video_state is not None and not video_state.get("remote_image_upload_error"):
                video_state["remote_image_upload_error"] = (
                    "Remote-image provider requires a public image URL, but no local image could be uploaded."
                )
            return ""
        # Local-first: pick the first existing local path, otherwise the first
        # non-empty value.
        for value in inputs:
            v = str(value or "").strip()
            if not v:
                continue
            if v.lower().startswith(("http://", "https://", "data:image")):
                continue
            try:
                if Path(v).exists():
                    return v
            except OSError:
                return v
        for value in inputs:
            v = str(value or "").strip()
            if v:
                return v
        return ""

    def _remote_video_input_upload_api_url(self) -> str:
        return str(getattr(self.config, "image_upload_api_url", "") or "").strip() or "oss://sora2-mission"

    def _upload_local_video_input_for_remote_provider(
        self,
        local_path: str,
        *,
        task: TaskItem | None,
        node: dict | None,
        video_state: dict | None,
    ) -> str:
        path_text = str(local_path or "").strip()
        if not path_text or path_text.lower().startswith(("http://", "https://", "data:image")):
            return ""
        try:
            if not Path(path_text).exists():
                error = f"OSS fallback upload failed: local image does not exist: {path_text}"
                if video_state is not None:
                    video_state["remote_image_upload_error"] = error
                return ""
        except OSError:
            # UNC/network shares can occasionally fail stat. Let the uploader
            # reopen the file and provide the concrete error if it is really
            # unavailable.
            pass

        try:
            timeout = max(30, int(getattr(self.config, "request_timeout_seconds", 120) or 120))
        except (TypeError, ValueError):
            timeout = 120
        upload_url = self._remote_video_input_upload_api_url()
        api_key = str(
            getattr(self.config, "image_upload_api_key", "")
            or getattr(self.config, "image_api_key", "")
            or ""
        )
        file_field = str(getattr(self.config, "image_upload_file_field", "") or "file")
        public_url, raw_response, error = upload_image_for_public_url(
            path_text,
            upload_url,
            api_key=api_key,
            file_field=file_field,
            timeout=timeout,
        )
        if error or not public_url:
            message = f"OSS fallback upload failed: {error or 'upload API returned no URL'}"
            if video_state is not None:
                video_state["remote_image_upload_error"] = message
                video_state["remote_image_upload_raw_response"] = raw_response
            if task is not None:
                self._log("ERROR", f"[{str((node or {}).get('node_id') or 'video')}] PID={task.pid} row={task.row_index} {message}", task)
            return ""

        if video_state is not None:
            video_state.pop("remote_image_upload_error", None)
            video_state["remote_image_url"] = public_url
            video_state["remote_image_upload_raw_response"] = raw_response
        if task is not None:
            self._cache_uploaded_video_input_url(task, node or {}, path_text, public_url)
            self._log(
                "INFO",
                f"[{str((node or {}).get('node_id') or 'video')}] PID={task.pid} row={task.row_index} "
                "OSS fallback uploaded local image for remote video provider",
                task,
            )
            self._save_emit(task, force=True)
        return public_url

    def _cache_uploaded_video_input_url(self, task: TaskItem, node: dict, local_path: str, public_url: str) -> None:
        def same_path(left: Any, right: Any) -> bool:
            return str(left or "").strip().replace("/", "\\").lower() == str(right or "").strip().replace("/", "\\").lower()

        if same_path(getattr(task, "generated_image_path", ""), local_path):
            task.generated_image_url = public_url
        if same_path(getattr(task, "product_image_path", ""), local_path):
            task.product_image_url = public_url

        for ref in list((node or {}).get("input_refs") or []):
            ref_text = str(ref or "")
            if ref_text == "source_image_1" and same_path(getattr(task, "product_image_path", ""), local_path):
                task.product_image_url = public_url
                continue
            if not ref_text.endswith(".output_image"):
                continue
            dep_node_id = ref_text.split(".", 1)[0]
            dep_state = get_node_state(task, dep_node_id)
            if same_path(dep_state.get("output_image_path"), local_path):
                dep_state["output_image_url"] = public_url
                if dep_node_id == "image_stage_1":
                    task.generated_image_url = public_url

    # ------------------------------------------------------------------ #
    # Manual operations (used by GUI right-click menu in the next round)
    # ------------------------------------------------------------------ #
    def manual_skip_node(self, task: TaskItem, node_id: str) -> NodeRunOutcome:
        state = get_node_state(task, node_id)
        if not state:
            return NodeRunOutcome(False, "", error_message=f"未知节点：{node_id}")
        state["status"] = NODE_STATUS_SKIPPED
        state["error_message"] = "用户已跳过该节点"
        state["ended_at"] = now_text()
        self._save_emit(task, force=True)
        return NodeRunOutcome(True, NODE_STATUS_SKIPPED)

    def manual_reset_node(self, task: TaskItem, node_id: str) -> NodeRunOutcome:
        """Reset a node back to PENDING so the scheduler picks it up again.

        Used by 重试此节点 and similar UI buttons.
        """
        state = get_node_state(task, node_id)
        if not state:
            return NodeRunOutcome(False, "", error_message=f"未知节点：{node_id}")
        state["status"] = NODE_STATUS_PENDING
        state["error_message"] = None
        state["started_at"] = None
        state["ended_at"] = None
        state["poll_count"] = 0
        state["task_id"] = None
        # Outputs intentionally cleared so the node re-runs fresh.
        state["output_image_path"] = None
        state["output_image_url"] = None
        state["output_video_url"] = None
        state["output_video_local_path"] = None
        state["raw_response"] = None
        node = self.get_node(node_id)
        if node:
            self._refresh_reset_node_provider_from_config(task, node, state)
        self._save_emit(task, force=True)
        return NodeRunOutcome(True, NODE_STATUS_PENDING)

    def manual_attach_input(self, task: TaskItem, node_id: str, images: list[str], urls: list[str]) -> NodeRunOutcome:
        """Add user-specified images / URLs to a node's manual input list.

        Stored on the node state and merged in resolve_node_input_images().
        """
        state = get_node_state(task, node_id)
        if not state:
            return NodeRunOutcome(False, "", error_message=f"未知节点：{node_id}")
        existing_imgs = list(state.get("manual_input_images") or [])
        existing_urls = list(state.get("manual_input_urls") or [])
        for img in images or []:
            img = str(img or "").strip()
            if img and img not in existing_imgs:
                existing_imgs.append(img)
        for url in urls or []:
            url = str(url or "").strip()
            if url and url not in existing_urls:
                existing_urls.append(url)
        state["manual_input_images"] = existing_imgs
        state["manual_input_urls"] = existing_urls
        self._save_emit(task, force=True)
        return NodeRunOutcome(True, str(state.get("status") or NODE_STATUS_PENDING))

    def manual_clear_input(self, task: TaskItem, node_id: str) -> NodeRunOutcome:
        state = get_node_state(task, node_id)
        if not state:
            return NodeRunOutcome(False, "", error_message=f"未知节点：{node_id}")
        state["manual_input_images"] = []
        state["manual_input_urls"] = []
        self._save_emit(task, force=True)
        return NodeRunOutcome(True, str(state.get("status") or NODE_STATUS_PENDING))


def auto_migrate_tasks_to_workflow(tasks: list[TaskItem], workflow_definition: dict) -> int:
    """Bulk version of WorkflowExecutor.migrate_legacy_task, suitable for
    use at batch-load time *without* needing a full executor instance.

    Returns the number of tasks that had their states materialized.
    """
    migrated = 0
    for task in tasks:
        ensure_task_node_states(task, workflow_definition)
        states = task.node_states or {}
        changed = False
        img1 = states.get("image_stage_1")
        if isinstance(img1, dict) and img1.get("status") in {None, "", NODE_STATUS_PENDING}:
            if task.generated_image_path or task.generated_image_url:
                img1["status"] = NODE_STATUS_COMPLETED
                img1["output_image_path"] = task.generated_image_path
                img1["output_image_url"] = task.generated_image_url
                img1["task_id"] = task.image_task_id
                changed = True
            elif task.status == TaskStatus.FAILED_IMAGE_API:
                img1["status"] = NODE_STATUS_FAILED
                img1["error_message"] = task.error_message or "图生图失败（旧版状态）"
                changed = True
        vid1 = states.get("video_stage_1")
        if isinstance(vid1, dict) and vid1.get("status") in {None, "", NODE_STATUS_PENDING}:
            if task.video_url:
                vid1["status"] = NODE_STATUS_COMPLETED
                vid1["output_video_url"] = task.video_url
                vid1["output_video_local_path"] = task.video_file_path
                vid1["task_id"] = task.video_task_id
                changed = True
            elif task.video_task_id:
                vid1["status"] = NODE_STATUS_SUBMITTED
                vid1["task_id"] = task.video_task_id
                vid1["poll_count"] = int(task.video_poll_count or 0)
                changed = True
            elif task.status in _LEGACY_FAILURE_STATUSES and task.error_message:
                vid1["status"] = NODE_STATUS_FAILED
                vid1["error_message"] = task.error_message
                changed = True
        if changed:
            migrated += 1
            if hasattr(task, "touch"):
                task.touch()
    return migrated
