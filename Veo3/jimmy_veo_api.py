import importlib.util
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

import requests


# =========================
# Top Config
# =========================
# 直接修改这里即可。
PROVIDER = "jimmy"

# 可选值：
# - "submit": 只提交任务
# - "query": 只查询任务
# - "submit_and_wait": 提交后轮询到完成或失败
ACTION = "submit_and_wait"

API_KEY = "replace_with_your_api_key"
PROMPT = "Let the first and last frames transition naturally with smooth camera motion."
MODEL = "veo_3_1_fast"
ORIENTATION = "landscape"
RESOLUTION = "1080p"

# 二选一：
# 1. 如果你已经有图片 URL，直接填写 URL
# 2. 如果你只有本地图片，填写本地路径，脚本会先上传 OSS 再转换成 URL
FIRST_FRAME_URL = ""
LAST_FRAME_URL = ""
FIRST_FRAME_LOCAL_PATH = ""
LAST_FRAME_LOCAL_PATH = ""

# 查询模式下使用
QUERY_TASK_ID = ""

# submit_and_wait 模式下使用
POLL_INTERVAL_SECONDS = 5
MAX_WAIT_SECONDS = 1800


# =========================
# 平台配置
# =========================
PROVIDER_CONFIG = {
    "jimmy": {
        "base_url": "https://www.jimmyai.cn",
        "create_path": "/api/open-api/v1/veo/frames",
        "status_path": "/api/open-api/v1/videos/{task_id}",
        "success_code": 20000,
        "default_timeout_submit": 120,
        "default_timeout_query": 60,
    }
}

OSS_UPLOADER_PATH = Path(__file__).resolve().parents[1] / "Sora2-mission" / "xv_gui" / "providers" / "oss_uploader.py"


def _build_success_result(message: str, data: Optional[Dict[str, Any]] = None, **extra: Any) -> Dict[str, Any]:
    result = {
        "success": True,
        "message": message,
        "data": data or {},
    }
    result.update(extra)
    return result


def _build_error_result(stage: str, exc: Exception, **extra: Any) -> Dict[str, Any]:
    result = {
        "success": False,
        "stage": stage,
        "message": str(exc),
        "error_type": type(exc).__name__,
        "data": {},
    }
    result.update(extra)
    return result


def _safe_call(stage: str, func, *args, **kwargs) -> Dict[str, Any]:
    try:
        return func(*args, **kwargs)
    except Exception as exc:
        return _build_error_result(
            stage,
            exc,
            traceback=_truncate_text(traceback.format_exc(), 4000),
        )


def _truncate_text(value: str, limit: int = 2000) -> str:
    return value if len(value) <= limit else f"{value[:limit]} ... [truncated {len(value) - limit} chars]"


def _is_http_url(value: str) -> bool:
    return value.strip().lower().startswith(("http://", "https://"))


def _print_json(title: str, data: Dict[str, Any]) -> None:
    print(f"\n{title}")
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _build_auth_headers(api_key: str) -> Dict[str, str]:
    api_key = api_key.strip()
    if not api_key:
        raise ValueError("API key cannot be empty.")
    return {"Authorization": f"Bearer {api_key}"}


def _build_json_headers(api_key: str) -> Dict[str, str]:
    headers = _build_auth_headers(api_key)
    headers["Content-Type"] = "application/json"
    return headers


def _build_http_error_message(stage: str, response: requests.Response) -> str:
    status_code = response.status_code
    response_text = _truncate_text((response.text or "").strip())
    if status_code in {401, 403}:
        prefix = f"{stage} failed: API key or permission error ({status_code})."
    elif status_code in {400, 422}:
        prefix = f"{stage} failed: request parameter error ({status_code})."
    elif status_code == 404:
        prefix = f"{stage} failed: endpoint not found (404)."
    elif status_code == 405:
        prefix = f"{stage} failed: request method not allowed (405)."
    elif 500 <= status_code < 600:
        prefix = f"{stage} failed: server error ({status_code})."
    else:
        prefix = f"{stage} failed: HTTP {status_code}."
    return f"{prefix} response={response_text}" if response_text else prefix


def _request_with_retry(
    method: str,
    url: str,
    *,
    headers: Dict[str, str],
    json_body: Optional[Dict[str, Any]] = None,
    timeout: int = 60,
    max_attempts: int = 3,
) -> requests.Response:
    last_error: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.request(method=method, url=url, headers=headers, json=json_body, timeout=timeout)
            if response.status_code >= 400:
                raise RuntimeError(_build_http_error_message("HTTP request", response))
            return response
        except RuntimeError:
            raise
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            last_error = exc
            if attempt >= max_attempts:
                raise RuntimeError(f"Network request failed after {max_attempts} attempts: {exc}") from exc
            time.sleep(min(2 ** (attempt - 1), 10))
    raise RuntimeError(f"Request failed: {last_error}") if last_error else RuntimeError("Request failed: unknown error.")


def _load_oss_uploader():
    module_dirs = [str(OSS_UPLOADER_PATH.parent), str(OSS_UPLOADER_PATH.parent.parent.parent)]
    for module_dir in module_dirs:
        if module_dir not in sys.path:
            sys.path.insert(0, module_dir)

    spec = importlib.util.spec_from_file_location("provider_oss_uploader", OSS_UPLOADER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load OSS uploader module: {OSS_UPLOADER_PATH}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.upload_file_and_sign_url


def _upload_local_file_to_url_impl(local_file_path: str) -> Dict[str, Any]:
    local_path = Path((local_file_path or "").strip())
    if not local_path.exists() or not local_path.is_file():
        raise ValueError(f"Local file not found: {local_file_path}")

    uploader = _load_oss_uploader()
    file_url = uploader(str(local_path))
    return _build_success_result(
        "Local file uploaded successfully.",
        data={
            "local_file_path": str(local_path),
            "file_url": file_url,
        },
    )


def upload_local_file_to_url(local_file_path: str) -> Dict[str, Any]:
    """
    对外公开函数。

    调用示例：
    result = upload_local_file_to_url(r"C:\\images\\frame1.png")
    if result["success"]:
        print(result["data"]["file_url"])
    """
    return _safe_call("upload_local_file_to_url", _upload_local_file_to_url_impl, local_file_path)


def _prepare_file_url_impl(file_url: str = "", local_file_path: str = "") -> Dict[str, Any]:
    if _is_http_url(file_url):
        return _build_success_result(
            "Use existing remote URL.",
            data={
                "file_url": file_url.strip(),
                "source": "remote_url",
            },
        )

    if local_file_path.strip():
        upload_result = upload_local_file_to_url(local_file_path.strip())
        if not upload_result["success"]:
            return upload_result
        return _build_success_result(
            "Converted local file to remote URL.",
            data={
                "file_url": upload_result["data"]["file_url"],
                "source": "local_upload",
                "local_file_path": local_file_path.strip(),
            },
        )

    return _build_success_result(
        "No file URL or local path provided.",
        data={
            "file_url": "",
            "source": "empty",
        },
    )


def prepare_file_url(file_url: str = "", local_file_path: str = "") -> Dict[str, Any]:
    """
    对外公开函数。

    调用示例：
    first_frame_result = prepare_file_url(
        file_url=FIRST_FRAME_URL,
        local_file_path=FIRST_FRAME_LOCAL_PATH,
    )
    """
    return _safe_call("prepare_file_url", _prepare_file_url_impl, file_url, local_file_path)


def _validate_jimmy_submit_params(
    prompt: str,
    orientation: str,
    resolution: str,
    first_frame_url: str,
    last_frame_url: str,
) -> None:
    if not prompt.strip():
        raise ValueError("Prompt cannot be empty.")
    if orientation not in {"landscape", "portrait"}:
        raise ValueError("orientation must be landscape or portrait.")
    if resolution not in {"720p", "1080p", "4k"}:
        raise ValueError("resolution must be one of: 720p, 1080p, 4k.")
    if not _is_http_url(first_frame_url):
        raise ValueError("Jimmy requires first_frame_url to be a valid http/https image URL.")
    if last_frame_url.strip() and not _is_http_url(last_frame_url):
        raise ValueError("Jimmy requires last_frame_url to be a valid http/https image URL.")


def _submit_task_jimmy_impl(
    api_key: str,
    prompt: str,
    first_frame_url: str,
    last_frame_url: str = "",
    orientation: str = "landscape",
    resolution: str = "1080p",
    model: str = "veo_3_1_fast",
) -> Dict[str, Any]:
    config = PROVIDER_CONFIG["jimmy"]
    _validate_jimmy_submit_params(prompt, orientation, resolution, first_frame_url, last_frame_url)

    payload = {
        "model": model,
        "prompt": prompt.strip(),
        "orientation": orientation,
        "first_frame_url": first_frame_url.strip(),
        "last_frame_url": last_frame_url.strip(),
        "resolution": resolution,
    }

    url = f"{config['base_url']}{config['create_path']}"
    response = _request_with_retry(
        "POST",
        url,
        headers=_build_json_headers(api_key),
        json_body=payload,
        timeout=config["default_timeout_submit"],
    )

    body = response.json()
    code = body.get("code")
    data = body.get("data")
    msg = body.get("msg")
    if code != config["success_code"] or not isinstance(data, dict):
        raise RuntimeError(f"Submit task failed: code={code}, msg={msg}, body={body}")
    if not str(data.get("task_id") or "").strip():
        raise RuntimeError(f"Submit task failed: missing data.task_id, body={body}")

    return _build_success_result(
        "Task submitted successfully.",
        data=data,
        provider="jimmy",
        request_payload=payload,
    )


def _query_task_jimmy_impl(api_key: str, task_id: str) -> Dict[str, Any]:
    config = PROVIDER_CONFIG["jimmy"]
    task_id = task_id.strip()
    if not task_id:
        raise ValueError("task_id cannot be empty.")

    url = f"{config['base_url']}{config['status_path'].format(task_id=task_id)}"
    response = _request_with_retry(
        "GET",
        url,
        headers=_build_auth_headers(api_key),
        timeout=config["default_timeout_query"],
    )

    body = response.json()
    code = body.get("code")
    data = body.get("data")
    msg = body.get("msg")
    if code != config["success_code"] or not isinstance(data, dict):
        raise RuntimeError(f"Query task failed: code={code}, msg={msg}, body={body}")

    return _build_success_result(
        "Task queried successfully.",
        data=data,
        provider="jimmy",
        task_id=task_id,
    )


def submit_video_task(
    provider: str,
    api_key: str,
    prompt: str,
    first_frame_url: str,
    last_frame_url: str = "",
    orientation: str = "landscape",
    resolution: str = "1080p",
    model: str = "veo_3_1_fast",
) -> Dict[str, Any]:
    """
    对外公开函数。

    调用示例：
    submit_result = submit_video_task(
        provider="jimmy",
        api_key=API_KEY,
        prompt=PROMPT,
        first_frame_url="https://example.com/first.png",
        last_frame_url="https://example.com/last.png",
        orientation="landscape",
        resolution="1080p",
        model="veo_3_1_fast",
    )
    """

    def _impl():
        provider_name = provider.strip().lower()
        if provider_name == "jimmy":
            return _submit_task_jimmy_impl(
                api_key=api_key,
                prompt=prompt,
                first_frame_url=first_frame_url,
                last_frame_url=last_frame_url,
                orientation=orientation,
                resolution=resolution,
                model=model,
            )
        raise ValueError(f"Unsupported provider: {provider}")

    return _safe_call("submit_video_task", _impl)


def query_video_task(provider: str, api_key: str, task_id: str) -> Dict[str, Any]:
    """
    对外公开函数。

    调用示例：
    query_result = query_video_task(
        provider="jimmy",
        api_key=API_KEY,
        task_id="your_task_id",
    )
    """

    def _impl():
        provider_name = provider.strip().lower()
        if provider_name == "jimmy":
            return _query_task_jimmy_impl(api_key=api_key, task_id=task_id)
        raise ValueError(f"Unsupported provider: {provider}")

    return _safe_call("query_video_task", _impl)


def wait_video_task(
    provider: str,
    api_key: str,
    task_id: str,
    poll_interval_seconds: int = 5,
    max_wait_seconds: int = 1800,
) -> Dict[str, Any]:
    """
    对外公开函数。

    调用示例：
    wait_result = wait_video_task(
        provider="jimmy",
        api_key=API_KEY,
        task_id="your_task_id",
        poll_interval_seconds=5,
        max_wait_seconds=1800,
    )
    """

    def _impl():
        start_time = time.time()
        poll_count = 0

        while True:
            poll_count += 1
            query_result = query_video_task(provider=provider, api_key=api_key, task_id=task_id)
            if not query_result["success"]:
                return query_result

            data = query_result["data"]
            status = str(data.get("status") or "").strip().lower()
            if status in {"completed", "failed"}:
                return _build_success_result(
                    "Task polling finished.",
                    data=data,
                    provider=provider,
                    task_id=task_id,
                    final_status=status,
                    poll_count=poll_count,
                )

            if time.time() - start_time > max_wait_seconds:
                raise TimeoutError(f"Polling timed out after {max_wait_seconds} seconds, task_id={task_id}")

            time.sleep(max(1, poll_interval_seconds))

    return _safe_call("wait_video_task", _impl)


def submit_video_task_from_config() -> Dict[str, Any]:
    """
    顶层辅助函数。
    用于 submit 模式，直接读取本文件顶部配置区。
    """

    def _impl():
        first_frame_result = prepare_file_url(file_url=FIRST_FRAME_URL, local_file_path=FIRST_FRAME_LOCAL_PATH)
        if not first_frame_result["success"]:
            return first_frame_result

        last_frame_result = prepare_file_url(file_url=LAST_FRAME_URL, local_file_path=LAST_FRAME_LOCAL_PATH)
        if not last_frame_result["success"]:
            return last_frame_result

        first_frame_url = first_frame_result["data"].get("file_url", "")
        last_frame_url = last_frame_result["data"].get("file_url", "")

        if not first_frame_url:
            raise ValueError("First frame is required. Please provide FIRST_FRAME_URL or FIRST_FRAME_LOCAL_PATH.")

        return submit_video_task(
            provider=PROVIDER,
            api_key=API_KEY,
            prompt=PROMPT,
            first_frame_url=first_frame_url,
            last_frame_url=last_frame_url,
            orientation=ORIENTATION,
            resolution=RESOLUTION,
            model=MODEL,
        )

    return _safe_call("submit_video_task_from_config", _impl)


def query_video_task_from_config() -> Dict[str, Any]:
    """
    顶层辅助函数。
    用于 query 模式。
    """

    def _impl():
        if not QUERY_TASK_ID.strip():
            raise ValueError("QUERY_TASK_ID is required in query mode.")
        return query_video_task(provider=PROVIDER, api_key=API_KEY, task_id=QUERY_TASK_ID)

    return _safe_call("query_video_task_from_config", _impl)


def submit_and_wait_from_config() -> Dict[str, Any]:
    """
    顶层辅助函数。
    用于 submit_and_wait 模式。
    """

    def _impl():
        submit_result = submit_video_task_from_config()
        if not submit_result["success"]:
            return submit_result

        task_id = str(submit_result["data"].get("task_id") or "").strip()
        if not task_id:
            raise RuntimeError("submit_video_task_from_config succeeded but task_id is missing.")

        return wait_video_task(
            provider=PROVIDER,
            api_key=API_KEY,
            task_id=task_id,
            poll_interval_seconds=POLL_INTERVAL_SECONDS,
            max_wait_seconds=MAX_WAIT_SECONDS,
        )

    return _safe_call("submit_and_wait_from_config", _impl)


def run_with_config() -> Dict[str, Any]:
    """
    一键运行入口。

    使用方法：
    1. 先修改本文件顶部配置区。
    2. 运行：python Veo3\\jimmy_veo_api.py
    3. 查看终端输出的 JSON 结果。
    """

    def _impl():
        if API_KEY.strip() == "replace_with_your_api_key":
            raise ValueError("Please update API_KEY in the top config section.")

        action = ACTION.strip().lower()
        if action == "submit":
            return submit_video_task_from_config()
        if action == "query":
            return query_video_task_from_config()
        if action == "submit_and_wait":
            return submit_and_wait_from_config()
        raise ValueError(f"Unsupported ACTION: {ACTION}")

    result = _safe_call("run_with_config", _impl)
    _print_json("run_result", result)
    return result


if __name__ == "__main__":
    run_with_config()
