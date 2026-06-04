from __future__ import annotations

from typing import Any

from app.api.provider_registry import get_image_provider, get_video_provider, model_display_name
from app.models.batch import TaskBatch
from app.models.task import TaskItem, TaskStatus, now_text
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
    ensure_task_node_states,
    workflow_nodes,
)


RESETTABLE_NODE_STATUSES = {
    NODE_STATUS_PENDING,
    NODE_STATUS_READY,
    NODE_STATUS_FAILED,
    NODE_STATUS_BLOCKED,
    NODE_STATUS_WAITING_INPUT,
}


def apply_runtime_provider_switch(
    batch: TaskBatch,
    tasks: list[TaskItem],
    override: dict[str, Any],
    workflow_definition: dict[str, Any],
) -> dict[str, int]:
    """Apply a runtime provider/model override to unfinished workflow nodes.

    The function intentionally preserves completed outputs and existing task_id
    chains by default, because those must continue polling with the provider
    that created them.
    """
    summary = {
        "tasks_seen": len(tasks),
        "tasks_updated": 0,
        "image_nodes_reset": 0,
        "video_nodes_reset": 0,
        "skipped_completed_nodes": 0,
        "skipped_task_id_nodes": 0,
        "skipped_running_nodes": 0,
    }
    force_restart_submitted = bool(override.get("force_restart_submitted"))
    apply_image = bool(override.get("apply_image"))
    apply_video = bool(override.get("apply_video"))

    image_provider_key = str(override.get("image_provider") or "").strip()
    image_model_key = str(override.get("image_model_logical_key") or "").strip()
    video_provider_key = str(override.get("video_provider") or "").strip()
    video_model_key = str(override.get("video_model_logical_key") or "").strip()

    image_display = ""
    video_display = ""
    if apply_image and image_provider_key and image_model_key:
        image_display = model_display_name(get_image_provider(image_provider_key), image_model_key)
    if apply_video and video_provider_key and video_model_key:
        video_display = model_display_name(get_video_provider(video_provider_key), video_model_key)

    nodes = list(workflow_nodes(workflow_definition))
    for task in tasks:
        ensure_task_node_states(task, workflow_definition)
        task_changed = False
        for node in nodes:
            node_id = str(node.get("node_id") or "")
            node_type = str(node.get("node_type") or "")
            if node_type == NODE_TYPE_IMAGE and not apply_image:
                continue
            if node_type == NODE_TYPE_VIDEO and not apply_video:
                continue
            state = task.node_states.get(node_id) or {}
            status = str(state.get("status") or NODE_STATUS_PENDING)

            if status in {NODE_STATUS_COMPLETED, NODE_STATUS_SKIPPED}:
                _stamp_existing_node_provider(state, task, node_type)
                summary["skipped_completed_nodes"] += 1
                continue

            if node_type == NODE_TYPE_IMAGE:
                old_task_id = str(state.get("task_id") or "")
                old_image_path = str(state.get("output_image_path") or "")
                old_image_url = str(state.get("output_image_url") or "")
                _apply_image_task_provider(task, image_provider_key, image_model_key, image_display)
                _reset_node_state(state)
                _stamp_node_provider(state, image_provider_key, image_model_key, image_display)
                if node_id == "image_stage_1":
                    if old_task_id and task.image_task_id == old_task_id:
                        task.image_task_id = None
                    if old_image_path and task.generated_image_path == old_image_path:
                        task.generated_image_path = None
                    if old_image_url and task.generated_image_url == old_image_url:
                        task.generated_image_url = None
                summary["image_nodes_reset"] += 1
                state["runtime_provider_switched_at"] = now_text()
                task_changed = True
                continue

            if state.get("task_id") and status not in RESETTABLE_NODE_STATUSES and not force_restart_submitted:
                _stamp_existing_node_provider(state, task, node_type)
                summary["skipped_task_id_nodes"] += 1
                continue
            if status in {NODE_STATUS_RUNNING, NODE_STATUS_SUBMITTED, NODE_STATUS_POLLING} and not force_restart_submitted:
                _stamp_existing_node_provider(state, task, node_type)
                summary["skipped_running_nodes"] += 1
                continue
            if status not in RESETTABLE_NODE_STATUSES and not force_restart_submitted:
                continue

            if node_type == NODE_TYPE_VIDEO:
                _apply_video_task_provider(task, video_provider_key, video_model_key, video_display)
                old_task_id = str(state.get("task_id") or "")
                _reset_node_state(state)
                _stamp_node_provider(state, video_provider_key, video_model_key, video_display)
                if old_task_id and task.video_task_id == old_task_id:
                    task.video_task_id = None
                if task.video_url and not task.video_file_path:
                    task.video_url = None
                task.video_status = ""
                task.video_poll_count = 0
                summary["video_nodes_reset"] += 1
            state["runtime_provider_switched_at"] = now_text()
            task_changed = True

        if task_changed:
            task.auto_retry_count = 0
            task.last_auto_retry_time = None
            task.last_auto_retry_stage = None
            task.last_auto_retry_reason = None
            if task.status != TaskStatus.VIDEO_DOWNLOADED:
                task.status = TaskStatus.PENDING
                task.error_message = None
            task.touch()
            summary["tasks_updated"] += 1

    _apply_batch_config_override(batch, override, image_display, video_display)
    return summary


def _apply_image_task_provider(task: TaskItem, provider_key: str, model_key: str, display_name: str) -> None:
    if provider_key:
        task.image_provider = provider_key
    if model_key:
        task.image_model_logical_key = model_key
    if display_name:
        task.image_model_display = display_name
    task.image_status = ""


def _apply_video_task_provider(task: TaskItem, provider_key: str, model_key: str, display_name: str) -> None:
    if provider_key:
        task.video_provider = provider_key
    if model_key:
        task.video_model_logical_key = model_key
    if display_name:
        task.video_model_display = display_name


def _reset_node_state(state: dict[str, Any]) -> None:
    state["status"] = NODE_STATUS_PENDING
    state["error_message"] = None
    state["started_at"] = None
    state["ended_at"] = None
    state["poll_count"] = 0
    state["manual_poll_count"] = 0
    state["task_id"] = None
    state["output_image_path"] = None
    state["output_image_url"] = None
    state["output_video_url"] = None
    state["output_video_local_path"] = None
    state["raw_response"] = None
    state["auto_retry_count"] = 0
    state["auto_retrying"] = False
    state["last_auto_retry_time"] = None
    state["last_auto_retry_reason"] = None


def _stamp_node_provider(state: dict[str, Any], provider_key: str, model_key: str, display_name: str) -> None:
    if provider_key:
        state["provider"] = provider_key
    if model_key:
        state["model_logical_key"] = model_key
    if display_name:
        state["model_display"] = display_name


def _stamp_existing_node_provider(state: dict[str, Any], task: TaskItem, node_type: str) -> None:
    if node_type == NODE_TYPE_IMAGE:
        if not state.get("provider") and task.image_provider:
            state["provider"] = task.image_provider
        if not state.get("model_logical_key") and task.image_model_logical_key:
            state["model_logical_key"] = task.image_model_logical_key
        if not state.get("model_display") and task.image_model_display:
            state["model_display"] = task.image_model_display
    elif node_type == NODE_TYPE_VIDEO:
        if not state.get("provider") and task.video_provider:
            state["provider"] = task.video_provider
        if not state.get("model_logical_key") and task.video_model_logical_key:
            state["model_logical_key"] = task.video_model_logical_key
        if not state.get("model_display") and task.video_model_display:
            state["model_display"] = task.video_model_display


def _apply_batch_config_override(
    batch: TaskBatch,
    override: dict[str, Any],
    image_display: str,
    video_display: str,
) -> None:
    config = dict(batch.batch_config or {})
    if override.get("apply_image"):
        for key in ["image_provider", "image_model_logical_key", "image_api_key", "image_api_base_url"]:
            if key in override and override[key] not in (None, ""):
                config[key] = override[key]
        if image_display:
            config["image_model_display_name"] = image_display
            batch.image_model_display_name = image_display
        if override.get("image_provider"):
            batch.image_provider = str(override["image_provider"])
    if override.get("apply_video"):
        for key in ["video_provider", "video_model_logical_key", "video_api_key", "video_api_base_url"]:
            if key in override and override[key] not in (None, ""):
                config[key] = override[key]
        if video_display:
            config["video_model_display_name"] = video_display
            batch.video_model_display_name = video_display
        if override.get("video_provider"):
            batch.video_provider = str(override["video_provider"])
    history = list(config.get("runtime_provider_switch_history") or [])
    history.append(
        {
            "switched_at": now_text(),
            "apply_image": bool(override.get("apply_image")),
            "apply_video": bool(override.get("apply_video")),
            "image_provider": override.get("image_provider"),
            "image_model_logical_key": override.get("image_model_logical_key"),
            "video_provider": override.get("video_provider"),
            "video_model_logical_key": override.get("video_model_logical_key"),
            "force_restart_submitted": bool(override.get("force_restart_submitted")),
        }
    )
    config["runtime_provider_switch_history"] = history[-20:]
    batch.batch_config = config
