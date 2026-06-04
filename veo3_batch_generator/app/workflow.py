from __future__ import annotations

from copy import deepcopy
from typing import Any


WORKFLOW_VERSION_V2_DEFAULT = "v2_default_4_stage"
NODE_TYPE_IMAGE = "image_generation"
NODE_TYPE_VIDEO = "video_generation"

NODE_STATUS_PENDING = "PENDING"
NODE_STATUS_READY = "READY"
NODE_STATUS_RUNNING = "RUNNING"
NODE_STATUS_SUBMITTED = "SUBMITTED"
NODE_STATUS_POLLING = "POLLING"
NODE_STATUS_COMPLETED = "COMPLETED"
NODE_STATUS_FAILED = "FAILED"
NODE_STATUS_SKIPPED = "SKIPPED"
NODE_STATUS_BLOCKED = "BLOCKED"
NODE_STATUS_WAITING_INPUT = "WAITING_INPUT"

TERMINAL_NODE_STATUSES = {
    NODE_STATUS_COMPLETED,
    NODE_STATUS_FAILED,
    NODE_STATUS_SKIPPED,
    NODE_STATUS_BLOCKED,
    NODE_STATUS_WAITING_INPUT,
}


DEFAULT_V2_NODE_OVERRIDES: dict[str, dict[str, Any]] = {
    "image_stage_1": {
        "node_name": "产品图生图1",
        "node_type": NODE_TYPE_IMAGE,
        "prompt_field": "提示词【阶段1】",
        "input_refs": ["source_image_1"],
        "output_key": "image_1",
    },
    "video_stage_1": {
        "node_name": "图1生视频1",
        "node_type": NODE_TYPE_VIDEO,
        "prompt_field": "提示词【阶段2】",
        "input_refs": ["image_stage_1.output_image"],
        "output_key": "video_1",
    },
    "image_stage_2": {
        "node_name": "图1+产品图生图2",
        "node_type": NODE_TYPE_IMAGE,
        "prompt_field": "提示词【阶段3】",
        "input_refs": ["image_stage_1.output_image", "source_image_1"],
        "output_key": "image_2",
    },
    "video_stage_2": {
        "node_name": "图2生视频2",
        "node_type": NODE_TYPE_VIDEO,
        "prompt_field": "提示词【阶段4】",
        "input_refs": ["image_stage_2.output_image"],
        "output_key": "video_2",
    },
}


def default_workflow_definition() -> dict[str, Any]:
    return {
        "workflow_version": WORKFLOW_VERSION_V2_DEFAULT,
        "nodes": [
            {
                "node_id": "image_stage_1",
                "node_name": "产品图生图1",
                "node_type": NODE_TYPE_IMAGE,
                "prompt_field": "提示词【阶段1】",
                "input_refs": ["source_image_1"],
                "output_key": "image_1",
                "enabled": True,
                "allow_manual_run": True,
                "allow_retry": True,
            },
            {
                "node_id": "video_stage_1",
                "node_name": "图1生视频1",
                "node_type": NODE_TYPE_VIDEO,
                "prompt_field": "提示词【阶段2】",
                "input_refs": ["image_stage_1.output_image"],
                "output_key": "video_1",
                "enabled": True,
                "allow_manual_run": True,
                "allow_retry": True,
            },
            {
                "node_id": "image_stage_2",
                "node_name": "图1+产品图生图2",
                "node_type": NODE_TYPE_IMAGE,
                "prompt_field": "提示词【阶段3】",
                "input_refs": ["image_stage_1.output_image", "source_image_1"],
                "output_key": "image_2",
                "enabled": True,
                "allow_manual_run": True,
                "allow_retry": True,
            },
            {
                "node_id": "video_stage_2",
                "node_name": "图2生视频2",
                "node_type": NODE_TYPE_VIDEO,
                "prompt_field": "提示词【阶段4】",
                "input_refs": ["image_stage_2.output_image"],
                "output_key": "video_2",
                "enabled": True,
                "allow_manual_run": True,
                "allow_retry": True,
            },
        ],
    }


def normalize_workflow_definition(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return default_workflow_definition()
    nodes = value.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return default_workflow_definition()
    normalized = {
        "workflow_version": str(value.get("workflow_version") or WORKFLOW_VERSION_V2_DEFAULT),
        "nodes": [],
    }
    for item in nodes:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("node_id") or "").strip()
        node_type = str(item.get("node_type") or "").strip()
        if not node_id or node_type not in {NODE_TYPE_IMAGE, NODE_TYPE_VIDEO}:
            continue
        input_refs = item.get("input_refs")
        node = {
            "node_id": node_id,
            "node_name": str(item.get("node_name") or node_id),
            "node_type": node_type,
            "prompt_field": str(item.get("prompt_field") or ""),
            "input_refs": [str(ref) for ref in input_refs] if isinstance(input_refs, list) else [],
            "output_key": str(item.get("output_key") or node_id),
            "enabled": bool(item.get("enabled", True)),
            "allow_manual_run": bool(item.get("allow_manual_run", True)),
            "allow_retry": bool(item.get("allow_retry", True)),
        }
        if normalized["workflow_version"] == WORKFLOW_VERSION_V2_DEFAULT and node_id in DEFAULT_V2_NODE_OVERRIDES:
            preserve_flags = {
                "enabled": node["enabled"],
                "allow_manual_run": node["allow_manual_run"],
                "allow_retry": node["allow_retry"],
            }
            node.update(DEFAULT_V2_NODE_OVERRIDES[node_id])
            node.update(preserve_flags)
        normalized["nodes"].append(node)
    return normalized if normalized["nodes"] else default_workflow_definition()


def workflow_nodes(workflow_definition: Any) -> list[dict[str, Any]]:
    return list(normalize_workflow_definition(workflow_definition).get("nodes") or [])


def node_state_template(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "node_id": str(node.get("node_id") or ""),
        "node_name": str(node.get("node_name") or node.get("node_id") or ""),
        "node_type": str(node.get("node_type") or ""),
        "status": NODE_STATUS_PENDING,
        "input_images": [],
        "manual_input_images": [],
        "manual_input_urls": [],
        "output_image_path": None,
        "output_image_url": None,
        "output_video_url": None,
        "output_video_local_path": None,
        "task_id": None,
        "poll_count": 0,
        "manual_poll_count": 0,
        "error_message": None,
        "started_at": None,
        "ended_at": None,
        "raw_response": None,
    }


def ensure_task_node_states(task: Any, workflow_definition: Any) -> dict[str, dict[str, Any]]:
    existing = getattr(task, "node_states", None)
    if not isinstance(existing, dict):
        existing = {}
    node_states: dict[str, dict[str, Any]] = {}
    for node in workflow_nodes(workflow_definition):
        node_id = str(node.get("node_id") or "")
        state = existing.get(node_id)
        template = node_state_template(node)
        if isinstance(state, dict):
            merged = dict(template)
            merged.update(state)
            merged["node_id"] = node_id
            merged["node_name"] = str(node.get("node_name") or merged.get("node_name") or node_id)
            merged["node_type"] = str(node.get("node_type") or merged.get("node_type") or "")
            node_states[node_id] = merged
        else:
            node_states[node_id] = template
    setattr(task, "node_states", node_states)
    if not getattr(task, "workflow_version", ""):
        setattr(task, "workflow_version", normalize_workflow_definition(workflow_definition)["workflow_version"])
    return node_states


def get_node_state(task: Any, node_id: str, workflow_definition: Any | None = None) -> dict[str, Any]:
    if workflow_definition is not None:
        ensure_task_node_states(task, workflow_definition)
    states = getattr(task, "node_states", {}) or {}
    state = states.get(node_id)
    return state if isinstance(state, dict) else {}


def set_node_status(task: Any, node_id: str, status: str, error_message: str | None = None) -> None:
    states = getattr(task, "node_states", {}) or {}
    state = states.get(node_id)
    if not isinstance(state, dict):
        return
    state["status"] = status
    if error_message is not None:
        state["error_message"] = error_message
    if hasattr(task, "touch"):
        task.touch()


def _prompt_attr_for_field(prompt_field: str) -> str:
    mapping = {
        "提示词【阶段1】": "prompt_stage_1",
        "提示词【阶段2】": "prompt_stage_2",
        "提示词【阶段3】": "prompt_stage_3",
        "提示词【阶段4】": "prompt_stage_4",
    }
    return mapping.get(str(prompt_field or "").strip(), "")


def get_task_prompt_for_node(task: Any, node: dict[str, Any]) -> str:
    attr = _prompt_attr_for_field(str(node.get("prompt_field") or ""))
    value = str(getattr(task, attr, "") or "").strip() if attr else ""
    if value:
        return value
    node_id = str(node.get("node_id") or "")
    if node_id == "image_stage_1":
        return str(getattr(task, "image_prompt", "") or "").strip()
    if node_id == "video_stage_1":
        return str(getattr(task, "video_prompt", "") or "").strip()
    return ""


def resolve_node_input_images(task: Any, node: dict[str, Any]) -> tuple[list[str], list[str]]:
    inputs: list[str] = []
    missing: list[str] = []
    for ref in list(node.get("input_refs") or []):
        ref = str(ref)
        if ref == "source_image_1":
            source = str(getattr(task, "product_image_path", "") or getattr(task, "product_image_url", "") or "").strip()
            if source:
                inputs.append(source)
            else:
                missing.append(ref)
            continue
        if ref.endswith(".output_image"):
            node_id = ref.split(".", 1)[0]
            state = get_node_state(task, node_id)
            value = str(state.get("output_image_path") or state.get("output_image_url") or "").strip()
            if value:
                inputs.append(value)
            else:
                missing.append(ref)
            continue
        missing.append(ref)

    state = get_node_state(task, str(node.get("node_id") or ""))
    for value in list(state.get("manual_input_images") or []) + list(state.get("manual_input_urls") or []):
        text = str(value or "").strip()
        if text and text not in inputs:
            inputs.append(text)
    return inputs, missing


def task_flow_progress_text(task: Any, workflow_definition: Any) -> str:
    ensure_task_node_states(task, workflow_definition)
    pieces: list[str] = []
    for index, node in enumerate(workflow_nodes(workflow_definition), start=1):
        state = get_node_state(task, str(node.get("node_id") or ""))
        status = str(state.get("status") or NODE_STATUS_PENDING)
        pieces.append(f"阶段{index}:{short_node_status(status)}")
    return " | ".join(pieces)


def short_node_status(status: str) -> str:
    mapping = {
        NODE_STATUS_PENDING: "待执行",
        NODE_STATUS_READY: "可执行",
        NODE_STATUS_RUNNING: "处理中",
        NODE_STATUS_SUBMITTED: "已提交",
        NODE_STATUS_POLLING: "轮询中",
        NODE_STATUS_COMPLETED: "完成",
        NODE_STATUS_FAILED: "失败",
        NODE_STATUS_SKIPPED: "跳过",
        NODE_STATUS_BLOCKED: "阻塞",
        NODE_STATUS_WAITING_INPUT: "缺输入",
    }
    return mapping.get(str(status or ""), str(status or ""))


def clone_workflow_definition(value: Any) -> dict[str, Any]:
    return deepcopy(normalize_workflow_definition(value))
