from __future__ import annotations

from typing import Any, Optional


def _walk(obj: Any, parts: list[str]) -> Any:
    current = obj
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def first_value(data: Any, paths: list[str]) -> Optional[str]:
    for path in paths:
        value = _walk(data, path.split("."))
        if value:
            return str(value)
    return None


def extract_image_b64(data: Any) -> Optional[str]:
    return first_value(
        data,
        [
            "b64",
            "b64_json",
            "base64",
            "image_base64",
            "data.0.b64_json",
            "data.0.b64",
            "data.0.base64",
            "data.0.image_base64",
            "data.b64_json",
            "data.image_base64",
        ],
    )


def extract_image_url(data: Any) -> Optional[str]:
    return first_value(
        data,
        [
            "url",
            "image_url",
            "data.0.url",
            "data.0.image_url",
            "data.image_url",
            "data.url",
            "result.url",
            "result.image_url",
        ],
    )


def extract_task_id(data: Any) -> Optional[str]:
    return first_value(data, ["task_id", "id", "data.task_id", "data.id", "result.task_id"])


def extract_status(data: Any) -> Optional[str]:
    return first_value(data, ["status", "data.status", "data.0.status", "result.status"])


def extract_error_message(data: Any) -> Optional[str]:
    return first_value(data, ["error", "message", "msg", "data.error", "data.message", "result.error"])


def extract_video_url(data: Any) -> Optional[str]:
    return first_value(
        data,
        [
            "video_url",
            "url",
            "output_url",
            "result_url",
            "data.video_url",
            "data.url",
            "data.0.video_url",
            "data.0.url",
            "data.result.video_url",
            "data.result.url",
            "data.output_url",
            "data.result_url",
            "data.output.0",
            "data.output.0.url",
            "data.output.0.video_url",
            "output.0",
            "output.0.url",
            "output.0.video_url",
            "video.0",
            "videos.0.url",
            "result.video_url",
            "result.url",
            "result.output_url",
            "result.result_url",
        ],
    )
