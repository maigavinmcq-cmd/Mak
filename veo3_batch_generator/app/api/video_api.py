from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter

from app.api.response_parser import extract_error_message, extract_status, extract_task_id, extract_video_url
from app.file_utils import file_to_data_url


@dataclass
class VideoSubmitResult:
    success: bool
    task_id: Optional[str] = None
    video_url: Optional[str] = None
    raw_response: Any = None
    error_message: Optional[str] = None


@dataclass
class VideoPollResult:
    success: bool
    finished: bool = False
    failed: bool = False
    retryable_failure: bool = False
    status: Optional[str] = None
    video_url: Optional[str] = None
    raw_response: Any = None
    error_message: Optional[str] = None


RETRYABLE_HTTP_STATUSES = {408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
_THREAD_LOCAL = threading.local()


def _session() -> requests.Session:
    session = getattr(_THREAD_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=100, pool_maxsize=100)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        _THREAD_LOCAL.session = session
    return session


def _short_error(raw: Any, limit: int = 600) -> str:
    if isinstance(raw, dict):
        error = raw.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("code")
            if message:
                return str(message)[:limit]
        message = raw.get("message") or raw.get("msg")
        if message:
            return str(message)[:limit]
        return str(raw)[:limit]
    text = str(raw or "").strip()
    lower = text.lower()
    if "error 524" in lower or "error code: 524" in lower:
        return "Cloudflare Error 524: xibapi.com origin timed out; will retry/poll again."
    if "<title>" in lower and "</title>" in lower:
        start = lower.find("<title>") + len("<title>")
        end = lower.find("</title>", start)
        title = text[start:end].strip()
        if title:
            return title[:limit]
    return text[:limit]


def _json_or_text(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return _short_error(response.text)


def _retryable(status_code: int) -> bool:
    return status_code in RETRYABLE_HTTP_STATUSES


def _is_data_or_url(value: str) -> bool:
    return value.startswith(("http://", "https://", "data:image"))


def is_retryable_video_failure(message: Any) -> bool:
    text = str(message or "")
    if "参数或鉴权错误" in text or "Invalid token" in text or "无效的令牌" in text:
        return False
    patterns = [
        "生成过程中出现异常，请重新发起请求",
        "重新发起请求",
        "task_not_exist",
        "not_exist",
        "not found",
        "任务不存在",
        "temporary",
        "timeout",
        "timed out",
        "try again",
    ]
    return any(pattern.lower() in text.lower() for pattern in patterns)


def submit_video_task(
    image_source: str,
    video_prompt: str,
    api_key: str,
    base_url: str = "https://xibapi.com",
    model: str = "veo_3_1-fast-fl",
    orientation: str = "portrait",
    resolution: str = "1080x1920",
    retry_count: int = 3,
    retry_interval_seconds: int = 5,
    timeout: int = 120,
) -> VideoSubmitResult:
    del orientation
    if not api_key:
        return VideoSubmitResult(False, error_message="VIDEO_API_KEY 为空")
    if not image_source:
        return VideoSubmitResult(False, error_message="生成图片为空，无法提交视频任务")
    if not video_prompt.strip():
        return VideoSubmitResult(False, error_message="视频提示词为空")

    if _is_data_or_url(image_source):
        image_value = image_source
    else:
        local_path = Path(image_source)
        if not local_path.exists():
            return VideoSubmitResult(False, error_message=f"生成图片文件不存在：{local_path}")
        image_value = file_to_data_url(local_path)

    url = f"{base_url.rstrip('/')}/v1/videos"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model, "prompt": video_prompt, "size": resolution, "images": [image_value]}
    retry_count = max(1, retry_count)
    last_error = ""

    for attempt in range(1, retry_count + 1):
        try:
            response = _session().post(url, headers=headers, json=payload, timeout=timeout)
            raw = _json_or_text(response)
            if response.status_code in {400, 401, 403, 405, 422}:
                return VideoSubmitResult(False, raw_response=raw, error_message=f"xibapi 视频提交参数或鉴权错误：HTTP {response.status_code} {raw}")
            if not response.ok:
                last_error = f"xibapi 视频提交 HTTP {response.status_code}: {raw}"
                if _retryable(response.status_code) and attempt < retry_count:
                    import time

                    time.sleep(retry_interval_seconds * attempt)
                    continue
                return VideoSubmitResult(False, raw_response=raw, error_message=last_error)
            if not isinstance(raw, dict):
                return VideoSubmitResult(False, raw_response=raw, error_message="xibapi 视频提交返回非 JSON")
            task_id = extract_task_id(raw)
            video_url = extract_video_url(raw)
            if task_id or video_url:
                return VideoSubmitResult(True, task_id=task_id, video_url=video_url, raw_response=raw)
            return VideoSubmitResult(False, raw_response=raw, error_message="xibapi 视频提交返回字段缺失：未找到 id/task_id")
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = f"xibapi 视频提交网络异常：{exc}"
            if attempt < retry_count:
                import time

                time.sleep(retry_interval_seconds * attempt)
                continue
            return VideoSubmitResult(False, error_message=last_error)
        except Exception as exc:
            return VideoSubmitResult(False, error_message=f"xibapi 视频提交未知异常：{exc}")

    return VideoSubmitResult(False, error_message=last_error or "xibapi 视频提交失败")


def poll_video_task(
    task_id: str,
    api_key: str,
    base_url: str = "https://xibapi.com",
) -> VideoPollResult:
    if not task_id:
        return VideoPollResult(False, failed=True, error_message="video_task_id 为空，无法轮询")
    if not api_key:
        return VideoPollResult(False, failed=True, error_message="VIDEO_API_KEY 为空")

    url = f"{base_url.rstrip('/')}/v1/videos/{task_id}"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        response = _session().get(url, headers=headers, timeout=60)
        raw = _json_or_text(response)
        if not response.ok:
            if _retryable(response.status_code):
                return VideoPollResult(True, finished=False, failed=False, status=f"HTTP_{response.status_code}", raw_response=raw)
            message = f"xibapi 视频任务查询失败：HTTP {response.status_code} {raw}"
            return VideoPollResult(
                False,
                failed=True,
                retryable_failure=response.status_code not in {401, 403} or is_retryable_video_failure(raw),
                status=f"HTTP_{response.status_code}",
                raw_response=raw,
                error_message=message,
            )
        if not isinstance(raw, dict):
            return VideoPollResult(False, failed=True, raw_response=raw, error_message="xibapi 视频任务查询返回非 JSON")
        status = (extract_status(raw) or "").lower()
        if status == "completed":
            video_url = extract_video_url(raw)
            if not video_url:
                return VideoPollResult(False, failed=True, status=status, raw_response=raw, error_message="视频任务已完成但 video_url 为空")
            return VideoPollResult(True, finished=True, status=status, video_url=video_url, raw_response=raw)
        if status == "failed":
            message = extract_error_message(raw) or raw
            return VideoPollResult(
                False,
                failed=True,
                retryable_failure=is_retryable_video_failure(message),
                status=status,
                raw_response=raw,
                error_message=f"xibapi 视频任务失败：{message}",
            )
        return VideoPollResult(True, finished=False, failed=False, status=status or "unknown", raw_response=raw)
    except (requests.Timeout, requests.ConnectionError) as exc:
        return VideoPollResult(True, finished=False, failed=False, status="network_retry", error_message=f"网络异常，稍后继续轮询：{exc}")
    except Exception as exc:
        return VideoPollResult(False, failed=True, error_message=f"xibapi 视频任务轮询异常：{exc}")


# Backward-compatible alias used by older imports.
create_video_task = submit_video_task
