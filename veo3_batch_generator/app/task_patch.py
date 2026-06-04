from __future__ import annotations

from typing import Any

from app.models.task import TaskItem, TaskStatus
from app.workflow import NODE_STATUS_COMPLETED


DURABLE_TASK_FIELDS = {
    "task_uid",
    "task_name",
    "pid",
    "owner",
    "netdisk_path",
    "netdisk_original_path",
    "netdisk_http_path",
    "image_prompt",
    "video_prompt",
    "prompt_stage_1",
    "prompt_stage_2",
    "prompt_stage_3",
    "prompt_stage_4",
    "workflow_version",
    "product_image_filename",
    "product_image_path",
    "product_image_url",
    "product_image_source_type",
    "generated_image_path",
    "generated_image_url",
    "image_task_id",
    "image_raw_response",
    "image_provider",
    "image_model_logical_key",
    "image_model_display",
    "video_url",
    "video_file_path",
    "video_task_id",
    "video_provider",
    "video_model_logical_key",
    "video_model_display",
    "video_submit_time",
    "video_poll_start_time",
    "video_poll_end_time",
    "video_raw_response",
    "last_manual_poll_time",
    "last_manual_poll_result",
    "last_auto_retry_time",
    "last_auto_retry_stage",
    "last_auto_retry_reason",
    "task_added_date",
    "batch_date",
    "batch_id",
    "batch_name",
    "imported_at",
    "source_excel_path",
    "created_at",
    "started_at",
    "ended_at",
}


DURABLE_NODE_FIELDS = {
    "input_images",
    "manual_input_images",
    "manual_input_urls",
    "output_image_path",
    "output_image_url",
    "output_video_url",
    "output_video_local_path",
    "task_id",
    "raw_response",
    "provider",
    "model_logical_key",
    "model_display",
    "started_at",
    "ended_at",
}


def merge_task_patch(task: TaskItem, patch: dict[str, Any]) -> None:
    """Merge a compact worker-process patch into a full TaskItem.

    Worker events intentionally carry only a compact subset of a task. Some
    fields in those patches can be empty because the worker is reporting a
    transient state, not because a durable business value should be deleted.
    Preserve non-empty durable fields unless the patch provides a replacement.
    """

    for field, value in patch.items():
        if field == "node_states":
            _merge_node_states(task, value)
            continue
        if field not in TaskItem.model_fields:
            continue
        if field == "status" and _should_preserve_downloaded_status(task, value, patch):
            continue
        if field in DURABLE_TASK_FIELDS and _is_empty(value) and not _is_empty(getattr(task, field, None)):
            continue
        setattr(task, field, value)


def _merge_node_states(task: TaskItem, value: Any) -> None:
    if not isinstance(value, dict):
        return
    node_states = task.node_states or {}
    for node_id, node_patch in value.items():
        if not isinstance(node_patch, dict):
            continue
        current = node_states.get(node_id)
        if not isinstance(current, dict):
            current = {}
        for field, patch_value in node_patch.items():
            if field in DURABLE_NODE_FIELDS and _is_empty(patch_value) and not _is_empty(current.get(field)):
                continue
            current[field] = patch_value
        node_states[node_id] = current
    task.node_states = node_states


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value == "":
        return True
    if isinstance(value, (list, tuple, set, dict)) and len(value) == 0:
        return True
    return False


def _should_preserve_downloaded_status(task: TaskItem, incoming_status: Any, patch: dict[str, Any]) -> bool:
    if task.status != TaskStatus.VIDEO_DOWNLOADED:
        return False
    if incoming_status == TaskStatus.VIDEO_DOWNLOADED:
        return False
    return _has_archived_video_evidence(task, patch)


def _has_archived_video_evidence(task: TaskItem, patch: dict[str, Any]) -> bool:
    if not _is_empty(getattr(task, "video_file_path", None)):
        return True
    if not _is_empty(patch.get("video_file_path")):
        return True
    for state in (task.node_states or {}).values():
        if not isinstance(state, dict):
            continue
        if not _is_empty(state.get("output_video_local_path")):
            return True
        if state.get("status") == NODE_STATUS_COMPLETED and state.get("download_status") == "DOWNLOADED":
            return True
    node_patches = patch.get("node_states")
    if isinstance(node_patches, dict):
        for state in node_patches.values():
            if not isinstance(state, dict):
                continue
            if not _is_empty(state.get("output_video_local_path")):
                return True
            if state.get("status") == NODE_STATUS_COMPLETED and state.get("download_status") == "DOWNLOADED":
                return True
    return False
