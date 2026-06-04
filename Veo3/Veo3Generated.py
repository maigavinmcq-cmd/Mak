import importlib.util
import json
import base64
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests


def _runtime_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        local_appdata = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        runtime_dir = local_appdata / "Veo3Portable"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        return runtime_dir
    return Path(__file__).resolve().parent


def _resource_base_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))

try:
    from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
except ModuleNotFoundError:
    def stop_after_attempt(max_attempts: int) -> int:
        return max_attempts

    def wait_exponential(multiplier: int = 1, min: int = 1, max: int = 10) -> Dict[str, int]:
        return {"multiplier": multiplier, "min": min, "max": max}

    def retry_if_exception_type(exc_types: Tuple[type, ...]) -> Tuple[type, ...]:
        return exc_types

    def retry(stop: int, wait: Dict[str, int], retry: Tuple[type, ...], reraise: bool = True):
        def decorator(func):
            def wrapper(*args, **kwargs):
                attempts = stop if isinstance(stop, int) else 1
                last_error = None
                for attempt in range(1, attempts + 1):
                    try:
                        return func(*args, **kwargs)
                    except retry as exc:  # type: ignore[misc]
                        last_error = exc
                        if attempt >= attempts:
                            if reraise:
                                raise
                            return None
                        sleep_seconds = min(max(wait["min"], wait["multiplier"] * (2 ** (attempt - 1))), wait["max"])
                        time.sleep(sleep_seconds)
                if last_error and reraise:
                    raise last_error
                return None

            return wrapper

        return decorator

BASE_URL = "https://www.jimmyai.cn"
CREATE_PATH = "/api/open-api/v1/veo/frames"
STATUS_PATH = "/api/open-api/v1/videos/{task_id}"
XIBAPI_BASE_URL = "https://xibapi.com"
XIBAPI_CREATE_PATH = "/v1/videos"
XIBAPI_STATUS_PATH = "/v1/videos/{task_id}"
SUCCESS_CODE = 20000
POLL_INTERVAL_SECONDS = 5
UI_REFRESH_INTERVAL_MS = 5000
APP_DIR = _runtime_base_dir()
RESOURCE_DIR = _resource_base_dir()
LOG_FILE = APP_DIR / "veo3_client.log"
TMP_UPLOAD_DIR = APP_DIR / "_tmp_oss_uploads"
STATE_FILE = APP_DIR / "veo3_ui_state.json"
OSS_UPLOADER_PATH = RESOURCE_DIR / "Sora2-mission" / "xv_gui" / "providers" / "oss_uploader.py"
MODEL_NAME = "veo_3_1_fast"
XIBAPI_TEXT_MODEL = "veo_3_1-fast"
XIBAPI_FRAMES_MODEL = "veo_3_1-fast-fl"
API_PROVIDER_OPTIONS = {
    "JimmyAI 首尾帧": "jimmy",
    "XIBAPI VEO": "xibapi",
}
XIBAPI_MODE_OPTIONS = {
    "文生视频": "text",
    "首尾帧模式": "frames",
    "参考图模式": "reference",
}
ORIENTATION_OPTIONS = {
    "横屏 720P": {"orientation": "landscape", "resolution": "720p", "size": "1280x720"},
    "横屏 1080P": {"orientation": "landscape", "resolution": "1080p", "size": "1920x1080"},
    "横屏 4K": {"orientation": "landscape", "resolution": "4k", "size": "3840x2160"},
    "竖屏 720P": {"orientation": "portrait", "resolution": "720p", "size": "720x1280"},
    "竖屏 1080P": {"orientation": "portrait", "resolution": "1080p", "size": "1080x1920"},
    "竖屏 4K": {"orientation": "portrait", "resolution": "4k", "size": "2160x3840"},
}
PROMPT_TEMPLATES = {
    "无模板": "",
    "商品展示": "让首尾帧之间自然过渡，镜头运动平滑，主体真实清晰，突出产品细节。",
    "电商转化": "让首尾帧之间自然过渡，强调卖点变化、使用场景和转化氛围，镜头节奏适合短视频。",
    "氛围叙事": "让首尾帧之间自然过渡，镜头运动克制流畅，环境光自然，整体具有电影感。",
}
LOCAL_STATUS_META = {
    "local_queue": ("待提交", "#6b7280"),
    "submitting": ("提交中", "#2563eb"),
    "submitted": ("已提交", "#2563eb"),
    "pending": ("初始化", "#64748b"),
    "queued": ("排队中", "#64748b"),
    "processing": ("生成中", "#7c3aed"),
    "completed": ("已完成", "#16a34a"),
    "failed": ("失败", "#dc2626"),
}
ACTIVE_REMOTE_STATUSES = {"submitted", "pending", "queued", "processing"}
logging.basicConfig(filename=str(LOG_FILE), level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", encoding="utf-8")


class JimmyAIVeoFramesClient:
    def __init__(self, api_key: str, base_url: str = BASE_URL):
        api_key = api_key.strip()
        if not api_key:
            raise ValueError("API 密钥不能为空。")
        self.base_url = base_url.rstrip("/")
        self.create_url = f"{self.base_url}{CREATE_PATH}"
        self.status_url = f"{self.base_url}{STATUS_PATH}"
        self.auth_headers = {"Authorization": f"Bearer {api_key}"}
        self.json_headers = {**self.auth_headers, "Content-Type": "application/json"}

    @staticmethod
    def _truncate_text(value: str, limit: int = 4000) -> str:
        return value if len(value) <= limit else f"{value[:limit]} ... [truncated {len(value) - limit} chars]"

    def _log_http_failure(self, stage: str, response: requests.Response, payload: Optional[Any] = None) -> None:
        payload_repr = ""
        if payload is not None:
            try:
                payload_repr = json.dumps(payload, ensure_ascii=False, default=str)
            except TypeError:
                payload_repr = str(payload)
        logging.error("HTTP failure at %s | status=%s | url=%s | payload=%s | response=%s", stage, response.status_code, response.url, payload_repr, self._truncate_text(response.text or ""))

    def _build_http_error_message(self, stage: str, response: requests.Response) -> str:
        status_code = response.status_code
        response_text = self._truncate_text((response.text or "").strip())
        if status_code in {401, 403}:
            prefix = f"{stage}失败：API 密钥或权限异常（{status_code}）。"
        elif status_code in {400, 422}:
            prefix = f"{stage}失败：请求参数可能有误（{status_code}）。"
        elif status_code == 404:
            prefix = f"{stage}失败：接口路径不存在（404）。"
        elif status_code == 405:
            prefix = f"{stage}失败：请求方法错误（405）。"
        elif 500 <= status_code < 600:
            prefix = f"{stage}失败：服务端异常（{status_code}）。"
        else:
            prefix = f"{stage}失败：HTTP {status_code}。"
        log_hint = f"完整返回体已写入日志：{LOG_FILE}"
        return f"{prefix}\n返回体：{response_text}\n{log_hint}" if response_text else f"{prefix}\n{log_hint}"

    @staticmethod
    def _extract_business_data(stage: str, response_json: Dict[str, Any]) -> Dict[str, Any]:
        code = response_json.get("code")
        data = response_json.get("data")
        msg = response_json.get("msg")
        if code != SUCCESS_CODE or not isinstance(data, dict):
            raise RuntimeError(f"{stage}失败：业务响应异常。code={code}, msg={msg}, body={response_json}")
        return data

    def _validate_create_params(self, prompt: str, orientation: str, first_frame_url: str, last_frame_url: str, resolution: str) -> None:
        if not prompt.strip():
            raise ValueError("提示词不能为空。")
        if orientation not in {"landscape", "portrait"}:
            raise ValueError("orientation 仅支持 landscape 或 portrait。")
        if resolution not in {"720p", "1080p", "4k"}:
            raise ValueError("resolution 仅支持 720p、1080p、4k。")
        if not is_http_url(first_frame_url):
            raise ValueError("首帧图片 URL 必须是 http/https 直链。")
        if last_frame_url.strip() and not is_http_url(last_frame_url):
            raise ValueError("尾帧图片 URL 必须是 http/https 直链。")

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type((requests.exceptions.Timeout, requests.exceptions.ConnectionError)), reraise=True)
    def create_video_task(self, prompt: str, orientation: str, first_frame_url: str, last_frame_url: str, resolution: str, model: str = MODEL_NAME) -> Dict[str, Any]:
        self._validate_create_params(prompt, orientation, first_frame_url, last_frame_url, resolution)
        payload = {
            "model": model,
            "prompt": prompt.strip(),
            "orientation": orientation,
            "first_frame_url": first_frame_url.strip(),
            "last_frame_url": last_frame_url.strip(),
            "resolution": resolution,
        }
        response = requests.post(self.create_url, headers=self.json_headers, json=payload, timeout=120)
        if response.status_code >= 400:
            self._log_http_failure("create_video_task", response, payload=payload)
            raise RuntimeError(self._build_http_error_message("创建任务", response))
        try:
            response.raise_for_status()
        except requests.HTTPError:
            self._log_http_failure("create_video_task", response, payload=payload)
            raise RuntimeError(self._build_http_error_message("创建任务", response))
        body = response.json()
        data = self._extract_business_data("创建任务", body)
        if not data.get("task_id"):
            logging.error("create_video_task missing task_id | response=%s", self._truncate_text(str(body)))
            raise RuntimeError(f"创建任务失败：返回体缺少 data.task_id。body={body}")
        return data

    def get_task_status(self, task_id: str) -> Dict[str, Any]:
        task_id = task_id.strip()
        if not task_id:
            raise ValueError("task_id 不能为空。")
        response = requests.get(self.status_url.format(task_id=task_id), headers=self.auth_headers, timeout=60)
        if response.status_code >= 400:
            self._log_http_failure("get_task_status", response, payload={"task_id": task_id})
            raise RuntimeError(self._build_http_error_message("查询任务", response))
        try:
            response.raise_for_status()
        except requests.HTTPError:
            self._log_http_failure("get_task_status", response, payload={"task_id": task_id})
            raise RuntimeError(self._build_http_error_message("查询任务", response))
        return self._extract_business_data("查询任务", response.json())


class XibapiVeoClient:
    def __init__(self, api_key: str, base_url: str = XIBAPI_BASE_URL):
        api_key = api_key.strip()
        if not api_key:
            raise ValueError("API 密钥不能为空。")
        self.base_url = base_url.rstrip("/")
        self.create_url = f"{self.base_url}{XIBAPI_CREATE_PATH}"
        self.status_url = f"{self.base_url}{XIBAPI_STATUS_PATH}"
        self.headers = {"Authorization": f"Bearer {api_key}"}

    @staticmethod
    def _truncate_text(value: str, limit: int = 4000) -> str:
        return value if len(value) <= limit else f"{value[:limit]} ... [truncated {len(value) - limit} chars]"

    def _build_http_error_message(self, stage: str, response: requests.Response) -> str:
        status_code = response.status_code
        response_text = self._truncate_text((response.text or "").strip())
        if status_code in {401, 403}:
            prefix = f"{stage}失败：API 密钥或权限异常（{status_code}）。"
        elif status_code in {400, 422}:
            prefix = f"{stage}失败：请求参数可能有误（{status_code}）。"
        elif status_code == 404:
            prefix = f"{stage}失败：接口路径不存在（404）。"
        elif status_code == 405:
            prefix = f"{stage}失败：请求方法错误（405）。"
        elif 500 <= status_code < 600:
            prefix = f"{stage}失败：服务端异常（{status_code}）。"
        else:
            prefix = f"{stage}失败：HTTP {status_code}。"
        return f"{prefix}\n返回体：{response_text}" if response_text else prefix

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), retry=retry_if_exception_type((requests.exceptions.Timeout, requests.exceptions.ConnectionError)), reraise=True)
    def create_video_task(self, prompt: str, size: str, model: str, input_uploads: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not prompt.strip():
            raise ValueError("提示词不能为空。")
        if not size.strip():
            raise ValueError("size 不能为空。")
        if not model.strip():
            raise ValueError("model 不能为空。")
        data: List[Tuple[str, str]] = [
            ("model", model.strip()),
            ("prompt", prompt.strip()),
            ("size", size.strip()),
        ]
        files: List[Tuple[str, Tuple[str, bytes, str]]] = []
        for uploaded in input_uploads:
            if not uploaded or not uploaded.get("data"):
                continue
            files.append((
                "input_reference[]",
                (
                    uploaded.get("name") or "upload.bin",
                    uploaded["data"],
                    uploaded.get("mime_type") or "application/octet-stream",
                ),
            ))
        response = requests.post(self.create_url, headers=self.headers, data=data, files=files, timeout=120)
        if response.status_code >= 400:
            raise RuntimeError(self._build_http_error_message("创建任务", response))
        body = response.json()
        task_id = str(body.get("id") or body.get("task_id") or "").strip()
        if not task_id:
            raise RuntimeError(f"创建任务失败：返回体缺少 id。body={body}")
        return body

    def get_task_status(self, task_id: str) -> Dict[str, Any]:
        task_id = task_id.strip()
        if not task_id:
            raise ValueError("task_id 不能为空。")
        response = requests.get(self.status_url.format(task_id=task_id), headers=self.headers, timeout=60)
        if response.status_code >= 400:
            raise RuntimeError(self._build_http_error_message("查询任务", response))
        return response.json()


def is_http_url(value: str) -> bool:
    return value.strip().lower().startswith(("http://", "https://"))


def serialize_upload(file_obj: Any) -> Dict[str, Any]:
    return {
        "name": getattr(file_obj, "name", "upload.bin"),
        "data": file_obj.getvalue(),
        "mime_type": getattr(file_obj, "type", "application/octet-stream") or "application/octet-stream",
    }


def load_oss_upload_func():
    module_dirs = [str(OSS_UPLOADER_PATH.parent), str(OSS_UPLOADER_PATH.parent.parent.parent)]
    for module_dir in module_dirs:
        if module_dir not in sys.path:
            sys.path.insert(0, module_dir)
    spec = importlib.util.spec_from_file_location("veo3_oss_uploader", OSS_UPLOADER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"oss_uploader_load_failed:{OSS_UPLOADER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.upload_file_and_sign_url


def upload_serialized_image_to_url(uploaded: Dict[str, Any], role: str) -> str:
    if not isinstance(uploaded, dict) or not uploaded.get("data"):
        raise RuntimeError(f"missing_uploaded_image:{role}")
    TMP_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(uploaded.get("name") or f"{role}.bin").suffix or ".bin"
    temp_path = TMP_UPLOAD_DIR / f"{role}_{int(time.time() * 1000)}{suffix}"
    temp_path.write_bytes(uploaded["data"])
    try:
        uploader = load_oss_upload_func()
        return uploader(str(temp_path))
    finally:
        temp_path.unlink(missing_ok=True)


def build_api_client(api_provider: str, api_key: str):
    if api_provider == "jimmy":
        return JimmyAIVeoFramesClient(api_key)
    if api_provider == "xibapi":
        return XibapiVeoClient(api_key)
    raise ValueError(f"不支持的 API 类型：{api_provider}")


def ensure_task_urls(task: Dict[str, Any]) -> None:
    if not is_http_url(task.get("first_frame_url", "")):
        task["first_frame_url"] = upload_serialized_image_to_url(task.get("first_frame_upload") or {}, "first_frame")
    if task.get("last_frame_upload"):
        if not is_http_url(task.get("last_frame_url", "")):
            task["last_frame_url"] = upload_serialized_image_to_url(task.get("last_frame_upload") or {}, "last_frame")
    else:
        task["last_frame_url"] = ""
        if not task.get("last_frame_name"):
            task["last_frame_name"] = "未上传尾帧"


def normalize_remote_status(status: str) -> str:
    value = (status or "").strip().lower()
    if value in {"submitted", "created"}:
        return "submitted"
    if value in {"pending", "queued", "processing", "completed", "failed"}:
        return value
    if value in {"running", "in_progress"}:
        return "processing"
    if value in {"success", "succeeded"}:
        return "completed"
    if value in {"error", "cancelled"}:
        return "failed"
    return value or "queued"


def extract_video_url(result: Dict[str, Any]) -> str:
    nested = result.get("result") if isinstance(result.get("result"), dict) else {}
    return (
        result.get("video_url")
        or nested.get("video_url")
        or result.get("url")
        or result.get("download_url")
        or result.get("result_url")
        or ""
    )


def get_task_remote_id(task: Dict[str, Any]) -> str:
    return str(task.get("remote_task_id") or task.get("local_id") or "")


def get_xibapi_uploads(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    mode = task.get("xibapi_mode", "frames")
    if mode == "text":
        return []
    if mode == "frames":
        uploads = [task.get("first_frame_upload")] if task.get("first_frame_upload") else []
        if task.get("last_frame_upload"):
            uploads.append(task["last_frame_upload"])
        return uploads
    return [item for item in task.get("reference_uploads", []) if item and item.get("data")]


def get_task_route_label(task: Dict[str, Any]) -> str:
    return "Jimmy 路由" if task.get("api_provider") == "jimmy" else "XIBAPI 路由"


def get_task_type_label(task: Dict[str, Any]) -> str:
    if task.get("api_provider") == "jimmy":
        return "首尾帧"
    mode = task.get("xibapi_mode", "")
    if mode == "text":
        return "文生视频"
    if mode == "reference":
        return "参考图"
    return "首尾帧"


def get_task_status_label(task: Dict[str, Any]) -> str:
    status = normalize_remote_status(task.get("status", ""))
    if status == "completed":
        return "已完成"
    if status == "processing":
        return "生成中"
    if status == "failed":
        return "失败"
    return "排队中"


def get_task_badge_html(task: Dict[str, Any]) -> str:
    status = normalize_remote_status(task.get("status", ""))
    label_map = {
        "completed": ("已完成", "#22c55e"),
        "processing": ("生成中", "#8b5cf6"),
        "failed": ("失败", "#ef4444"),
        "queued": ("排队中", "#64748b"),
        "pending": ("排队中", "#64748b"),
        "submitted": ("排队中", "#64748b"),
        "local_queue": ("排队中", "#64748b"),
    }
    label, color = label_map.get(status, ("排队中", "#64748b"))
    route = get_task_route_label(task)
    return (
        f"<span style='display:inline-block;padding:4px 10px;border-radius:999px;background:{color};color:#fff;font-size:11px;font-weight:700;'>{label}</span> "
        f"<span style='display:inline-block;padding:4px 10px;border-radius:999px;border:1px solid rgba(148,163,184,.45);background:rgba(15,23,42,.35);color:#cbd5e1;font-size:11px;font-weight:600;'>{route}</span>"
    )


def get_task_preview_upload(task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if task.get("first_frame_upload"):
        return task.get("first_frame_upload")
    refs = task.get("reference_uploads", [])
    return refs[0] if refs else None


def normalize_filter_value(filter_name: str, value: Any) -> Any:
    raw = str(value or "").strip()
    mapping = {
        "time_range": {"All": "全部"},
        "status": {"All": "全部", "Queued": "排队中", "Processing": "生成中", "Success": "已完成", "Failed": "失败"},
        "route": {"All": "全部", "Jimmy Route": "Jimmy 路由", "XIBAPI Route": "XIBAPI 路由"},
        "type": {"All": "全部", "Text to Video": "文生视频", "First-last Frame": "首尾帧", "Reference Image": "参考图"},
    }
    return mapping.get(filter_name, {}).get(raw, raw)


def task_matches_filters(task: Dict[str, Any]) -> bool:
    st = _load_streamlit()
    search_value = st.session_state.get("veo3_filter_search", "").strip().lower()
    if search_value:
        haystack = " ".join([
            str(task.get("remote_task_id", "")),
            str(task.get("local_id", "")),
            str(task.get("prompt", "")),
        ]).lower()
        if search_value not in haystack:
            return False

    status_filter = normalize_filter_value("status", st.session_state.get("veo3_filter_status", "全部"))
    status_label = get_task_status_label(task)
    if status_filter != "全部" and status_filter != status_label:
        return False

    route_filter = normalize_filter_value("route", st.session_state.get("veo3_filter_route", "全部"))
    route_label = get_task_route_label(task)
    if route_filter != "全部" and route_filter != route_label:
        return False

    type_filter = normalize_filter_value("type", st.session_state.get("veo3_filter_type", "全部"))
    type_label = get_task_type_label(task)
    if type_filter != "全部" and type_filter != type_label:
        return False

    time_range = normalize_filter_value("time_range", st.session_state.get("veo3_filter_time_range", "最近 7 天"))
    day_map = {"最近 24 小时": 1, "最近 3 天": 3, "最近 7 天": 7, "最近 30 天": 30, "全部": None}
    max_days = day_map.get(time_range, 7)
    if max_days is not None:
        if time.time() - float(task.get("created_at", 0)) > max_days * 86400:
            return False
    return True


def infer_progress(result: Dict[str, Any], status: str, previous: int = 0) -> int:
    raw_progress = result.get("progress")
    if isinstance(raw_progress, (int, float)):
        return max(0, min(int(raw_progress), 100))
    fallback = {"submitted": 5, "pending": 5, "queued": 15, "processing": 55, "completed": 100, "failed": previous}
    return max(previous, fallback.get(status, previous)) if status != "failed" else previous


def format_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def format_optional_timestamp(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    ts = float(value)
    if ts > 10_000_000_000:
        ts = ts / 1000
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def truncate_text(value: str, limit: int = 72) -> str:
    value = value.strip()
    return value if len(value) <= limit else f"{value[:limit]}..."


def build_status_badge(status: str) -> str:
    label, color = LOCAL_STATUS_META.get(status, (status or "未知", "#6b7280"))
    return f"<span style='display:inline-block;padding:4px 10px;border-radius:999px;background:{color}14;color:{color};font-size:12px;font-weight:600;'>{label}</span>"


def render_uploaded_image_preview(uploaded: Optional[Dict[str, Any]], label: str) -> None:
    st = _load_streamlit()
    if not uploaded or not uploaded.get("data"):
        st.caption(f"{label}：未上传")
        return
    st.image(uploaded["data"], caption=label, use_container_width=True)


def encode_upload_for_storage(uploaded: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not uploaded or not uploaded.get("data"):
        return None
    return {
        "name": uploaded.get("name", "upload.bin"),
        "mime_type": uploaded.get("mime_type", "application/octet-stream"),
        "data_b64": base64.b64encode(uploaded["data"]).decode("ascii"),
    }


def decode_upload_from_storage(payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not payload or not payload.get("data_b64"):
        return None
    return {
        "name": payload.get("name", "upload.bin"),
        "mime_type": payload.get("mime_type", "application/octet-stream"),
        "data": base64.b64decode(payload["data_b64"]),
    }


def serialize_task_for_storage(task: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(task)
    data["first_frame_upload"] = encode_upload_for_storage(task.get("first_frame_upload"))
    data["last_frame_upload"] = encode_upload_for_storage(task.get("last_frame_upload"))
    data["reference_uploads"] = [encode_upload_for_storage(item) for item in task.get("reference_uploads", []) if item]
    data["api_key"] = ""
    return data


def deserialize_task_from_storage(task: Dict[str, Any]) -> Dict[str, Any]:
    data = dict(task)
    data["first_frame_upload"] = decode_upload_from_storage(task.get("first_frame_upload"))
    data["last_frame_upload"] = decode_upload_from_storage(task.get("last_frame_upload"))
    data["reference_uploads"] = [decode_upload_from_storage(item) for item in task.get("reference_uploads", []) if item]
    data["api_key"] = ""
    return data


def load_persisted_state() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        logging.exception("load_persisted_state failed")
        return {}
    tasks = [deserialize_task_from_storage(item) for item in payload.get("tasks", []) if isinstance(item, dict)]
    return {
        "tasks": tasks,
        "selected_task": payload.get("selected_task"),
        "preferences": payload.get("preferences", {}),
    }


def persist_state() -> None:
    st = _load_streamlit()
    payload = {
        "tasks": [serialize_task_for_storage(task) for task in st.session_state.get("veo3_tasks", [])],
        "selected_task": st.session_state.get("veo3_selected_task"),
        "preferences": {
            "api_provider_label": st.session_state.get("veo3_api_provider_label", "JimmyAI 首尾帧"),
            "xibapi_mode_label": st.session_state.get("veo3_xibapi_mode_label", "首尾帧模式"),
            "preset_label": st.session_state.get("veo3_preset_label", "竖屏 1080P"),
            "template_name": st.session_state.get("veo3_template_name", "无模板"),
            "prompt": st.session_state.get("veo3_prompt", ""),
            "auto_refresh": st.session_state.get("veo3_auto_refresh", True),
            "filter_time_range": st.session_state.get("veo3_filter_time_range", "最近 7 天"),
            "filter_search": st.session_state.get("veo3_filter_search", ""),
            "filter_status": st.session_state.get("veo3_filter_status", "全部"),
            "filter_route": st.session_state.get("veo3_filter_route", "全部"),
            "filter_type": st.session_state.get("veo3_filter_type", "全部"),
            "hide_expiry_notice": st.session_state.get("veo3_hide_expiry_notice", False),
        },
    }
    try:
        STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logging.exception("persist_state failed")


def can_use_autorefresh() -> bool:
    try:
        import streamlit_autorefresh  # noqa: F401
        return True
    except Exception:
        return False


def ensure_state() -> None:
    st = _load_streamlit()
    if "_veo3_persisted" not in st.session_state:
        st.session_state._veo3_persisted = load_persisted_state()
    if "veo3_tasks" not in st.session_state:
        st.session_state.veo3_tasks = st.session_state._veo3_persisted.get("tasks", [])
    if "veo3_selected_task" not in st.session_state:
        st.session_state.veo3_selected_task = st.session_state._veo3_persisted.get("selected_task")
    if "veo3_notice" not in st.session_state:
        st.session_state.veo3_notice = None
    if "veo3_auto_refresh" not in st.session_state:
        st.session_state.veo3_auto_refresh = st.session_state._veo3_persisted.get("preferences", {}).get("auto_refresh", True)
    prefs = st.session_state._veo3_persisted.get("preferences", {})
    if "veo3_api_provider_label" not in st.session_state:
        st.session_state.veo3_api_provider_label = prefs.get("api_provider_label", "JimmyAI 首尾帧")
    if "veo3_xibapi_mode_label" not in st.session_state:
        st.session_state.veo3_xibapi_mode_label = prefs.get("xibapi_mode_label", "首尾帧模式")
    if "veo3_preset_label" not in st.session_state:
        st.session_state.veo3_preset_label = prefs.get("preset_label", "竖屏 1080P")
    if "veo3_template_name" not in st.session_state:
        st.session_state.veo3_template_name = prefs.get("template_name", "无模板")
    if "veo3_prompt" not in st.session_state:
        st.session_state.veo3_prompt = prefs.get("prompt", "")
    if "veo3_api_key" not in st.session_state:
        st.session_state.veo3_api_key = ""
    if "veo3_form_first_frame_upload" not in st.session_state:
        st.session_state.veo3_form_first_frame_upload = None
    if "veo3_form_last_frame_upload" not in st.session_state:
        st.session_state.veo3_form_last_frame_upload = None
    if "veo3_form_reference_uploads" not in st.session_state:
        st.session_state.veo3_form_reference_uploads = []
    if "veo3_selected_ids" not in st.session_state:
        st.session_state.veo3_selected_ids = []
    if "veo3_detail_dialog_task_id" not in st.session_state:
        st.session_state.veo3_detail_dialog_task_id = None
    if "veo3_delete_confirm_ids" not in st.session_state:
        st.session_state.veo3_delete_confirm_ids = []
    if "veo3_pending_backfill_task_id" not in st.session_state:
        st.session_state.veo3_pending_backfill_task_id = None
    if "veo3_filter_reset_requested" not in st.session_state:
        st.session_state.veo3_filter_reset_requested = False
    if "veo3_last_global_poll_at" not in st.session_state:
        st.session_state.veo3_last_global_poll_at = 0.0
    if "veo3_filter_time_range" not in st.session_state:
        st.session_state.veo3_filter_time_range = normalize_filter_value("time_range", prefs.get("filter_time_range", "最近 7 天"))
    if "veo3_filter_search" not in st.session_state:
        st.session_state.veo3_filter_search = prefs.get("filter_search", "")
    if "veo3_filter_status" not in st.session_state:
        st.session_state.veo3_filter_status = normalize_filter_value("status", prefs.get("filter_status", "全部"))
    if "veo3_filter_route" not in st.session_state:
        st.session_state.veo3_filter_route = normalize_filter_value("route", prefs.get("filter_route", "全部"))
    if "veo3_filter_type" not in st.session_state:
        st.session_state.veo3_filter_type = normalize_filter_value("type", prefs.get("filter_type", "全部"))
    if "veo3_hide_expiry_notice" not in st.session_state:
        st.session_state.veo3_hide_expiry_notice = prefs.get("hide_expiry_notice", False)
    if "veo3_filter_draft_time_range" not in st.session_state:
        st.session_state.veo3_filter_draft_time_range = normalize_filter_value("time_range", st.session_state.veo3_filter_time_range)
    if "veo3_filter_draft_search" not in st.session_state:
        st.session_state.veo3_filter_draft_search = st.session_state.veo3_filter_search
    if "veo3_filter_draft_status" not in st.session_state:
        st.session_state.veo3_filter_draft_status = normalize_filter_value("status", st.session_state.veo3_filter_status)
    if "veo3_filter_draft_route" not in st.session_state:
        st.session_state.veo3_filter_draft_route = normalize_filter_value("route", st.session_state.veo3_filter_route)
    if "veo3_filter_draft_type" not in st.session_state:
        st.session_state.veo3_filter_draft_type = normalize_filter_value("type", st.session_state.veo3_filter_type)
    if st.session_state.veo3_selected_task and not any(task["local_id"] == st.session_state.veo3_selected_task for task in st.session_state.veo3_tasks):
        st.session_state.veo3_selected_task = st.session_state.veo3_tasks[0]["local_id"] if st.session_state.veo3_tasks else None


def create_task_record(payload: Dict[str, Any], queued_locally: bool) -> Dict[str, Any]:
    now = time.time()
    return {
        "local_id": f"task-{int(now * 1000)}",
        "remote_task_id": "",
        "api_provider": payload["api_provider"],
        "api_key": payload["api_key"],
        "model": payload["model"],
        "orientation": payload["orientation"],
        "resolution": payload["resolution"],
        "size": payload["size"],
        "preset_label": payload["preset_label"],
        "template_name": payload.get("template_name", "无模板"),
        "prompt": payload["prompt"],
        "prompt_preview": truncate_text(payload["prompt"], 88),
        "first_frame_upload": payload["first_frame_upload"],
        "last_frame_upload": payload["last_frame_upload"],
        "reference_uploads": payload.get("reference_uploads", []),
        "first_frame_name": payload["first_frame_upload"]["name"] if payload.get("first_frame_upload") else "未上传首帧",
        "last_frame_name": payload["last_frame_upload"]["name"] if payload.get("last_frame_upload") else "未上传尾帧",
        "reference_names": payload.get("reference_names", []),
        "xibapi_mode": payload.get("xibapi_mode", ""),
        "first_frame_url": "",
        "last_frame_url": "",
        "status": "local_queue" if queued_locally else "submitting",
        "progress": 0 if queued_locally else 3,
        "created_at": now,
        "updated_at": now,
        "last_polled_at": 0.0,
        "error": "",
        "video_url": "",
        "result": {},
    }


def add_task(task: Dict[str, Any]) -> None:
    st = _load_streamlit()
    st.session_state.veo3_tasks.insert(0, task)
    st.session_state.veo3_selected_task = task["local_id"]
    persist_state()


def get_selected_task() -> Optional[Dict[str, Any]]:
    st = _load_streamlit()
    selected_id = st.session_state.veo3_selected_task
    tasks = st.session_state.veo3_tasks
    for task in tasks:
        if task["local_id"] == selected_id:
            return task
    if tasks:
        st.session_state.veo3_selected_task = tasks[0]["local_id"]
        return tasks[0]
    return None


def get_task_by_local_id(local_id: str) -> Optional[Dict[str, Any]]:
    st = _load_streamlit()
    for task in st.session_state.get("veo3_tasks", []):
        if task["local_id"] == local_id:
            return task
    return None


def remove_task(local_id: str) -> None:
    st = _load_streamlit()
    st.session_state.veo3_tasks = [task for task in st.session_state.veo3_tasks if task["local_id"] != local_id]
    st.session_state.veo3_selected_ids = [task_id for task_id in st.session_state.get("veo3_selected_ids", []) if task_id != local_id]
    if st.session_state.get("veo3_detail_dialog_task_id") == local_id:
        st.session_state.veo3_detail_dialog_task_id = None
    if st.session_state.veo3_selected_task == local_id:
        st.session_state.veo3_selected_task = st.session_state.veo3_tasks[0]["local_id"] if st.session_state.veo3_tasks else None
    persist_state()


def resolve_task_api_key(task: Dict[str, Any]) -> str:
    st = _load_streamlit()
    return str(task.get("api_key") or st.session_state.get("veo3_api_key") or "").strip()


def set_form_upload_state(first_frame: Optional[Dict[str, Any]] = None, last_frame: Optional[Dict[str, Any]] = None, references: Optional[List[Dict[str, Any]]] = None) -> None:
    st = _load_streamlit()
    st.session_state.veo3_form_first_frame_upload = first_frame
    st.session_state.veo3_form_last_frame_upload = last_frame
    st.session_state.veo3_form_reference_uploads = references or []


def get_form_upload_state() -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    st = _load_streamlit()
    return (
        st.session_state.get("veo3_form_first_frame_upload"),
        st.session_state.get("veo3_form_last_frame_upload"),
        st.session_state.get("veo3_form_reference_uploads", []),
    )


def populate_form_from_task(task: Dict[str, Any]) -> None:
    st = _load_streamlit()
    st.session_state.veo3_pending_backfill_task_id = task.get("local_id")


def apply_pending_form_backfill() -> None:
    st = _load_streamlit()
    task_id = st.session_state.get("veo3_pending_backfill_task_id")
    if not task_id:
        return
    task = get_task_by_local_id(task_id)
    st.session_state.veo3_pending_backfill_task_id = None
    if task is None:
        st.session_state.veo3_notice = ("error", f"未找到待回填任务：{task_id}")
        return
    provider_label = "JimmyAI 首尾帧" if task.get("api_provider") == "jimmy" else "XIBAPI VEO"
    mode_map = {"text": "文生视频", "frames": "首尾帧模式", "reference": "参考图模式"}
    st.session_state.veo3_api_provider_label = provider_label
    st.session_state.veo3_xibapi_mode_label = mode_map.get(task.get("xibapi_mode", "frames"), "首尾帧模式")
    st.session_state.veo3_preset_label = task.get("preset_label", "竖屏 1080P")
    st.session_state.veo3_template_name = task.get("template_name", "无模板")
    st.session_state.veo3_prompt = task.get("prompt", "")
    set_form_upload_state(
        task.get("first_frame_upload"),
        task.get("last_frame_upload"),
        task.get("reference_uploads", []),
    )
    st.session_state.veo3_notice = ("success", f"已回填任务参数：{task.get('local_id')}")


def clear_form_upload_state() -> None:
    set_form_upload_state(None, None, [])

def sync_task_from_result(task: Dict[str, Any], result: Dict[str, Any]) -> None:
    status = normalize_remote_status(result.get("status", ""))
    task["status"] = status
    task["progress"] = infer_progress(result, status, task.get("progress", 0))
    task["updated_at"] = time.time()
    task["last_polled_at"] = task["updated_at"]
    task["result"] = result
    task["video_url"] = extract_video_url(result) or task.get("video_url", "")
    task["error"] = result.get("error_message") or result.get("error") or result.get("message") or task.get("error", "")


def submit_task(task: Dict[str, Any]) -> None:
    task["status"] = "submitting"
    task["progress"] = max(task.get("progress", 0), 3)
    task["updated_at"] = time.time()
    task["error"] = ""
    api_key = resolve_task_api_key(task)
    client = build_api_client(task["api_provider"], api_key)
    task["api_key"] = api_key
    if task["api_provider"] == "jimmy":
        ensure_task_urls(task)
        created = client.create_video_task(
            prompt=task["prompt"],
            orientation=task["orientation"],
            first_frame_url=task["first_frame_url"],
            last_frame_url=task["last_frame_url"],
            resolution=task["resolution"],
            model=task["model"],
        )
        task["remote_task_id"] = created["task_id"]
    else:
        created = client.create_video_task(
            prompt=task["prompt"],
            size=task["size"],
            model=task["model"],
            input_uploads=get_xibapi_uploads(task),
        )
        task["remote_task_id"] = str(created.get("id") or created.get("task_id") or "")
    task["status"] = normalize_remote_status(created.get("status", "queued"))
    task["progress"] = infer_progress(created, task["status"], 5)
    task["updated_at"] = time.time()
    task["last_polled_at"] = 0.0
    task["result"] = created


def retry_task(task: Dict[str, Any]) -> None:
    task["remote_task_id"] = ""
    task["video_url"] = ""
    task["result"] = {}
    task["error"] = ""
    submit_task(task)


def poll_remote_tasks() -> Tuple[int, bool]:
    st = _load_streamlit()
    changed = 0
    active_exists = False
    now = time.time()
    for task in st.session_state.veo3_tasks:
        if not task.get("remote_task_id") or task["status"] not in ACTIVE_REMOTE_STATUSES:
            continue
        active_exists = True
        if now - task.get("last_polled_at", 0) < POLL_INTERVAL_SECONDS:
            continue
        try:
            api_key = resolve_task_api_key(task)
            client = build_api_client(task["api_provider"], api_key)
            task["api_key"] = api_key
            result = client.get_task_status(task["remote_task_id"])
        except Exception as exc:
            logging.exception("poll_remote_tasks failed for %s", task["remote_task_id"])
            task["error"] = str(exc)
            task["updated_at"] = now
            task["last_polled_at"] = now
            changed += 1
            continue
        sync_task_from_result(task, result)
        changed += 1
    active_exists = active_exists or any(task["status"] in ACTIVE_REMOTE_STATUSES for task in st.session_state.veo3_tasks)
    return changed, active_exists


def validate_payload(payload: Dict[str, Any], require_api_key: bool) -> List[str]:
    errors: List[str] = []
    if require_api_key and not payload["api_key"].strip():
        errors.append("请填写 API 密钥。")
    if not payload["prompt"].strip():
        errors.append("请输入提示词。")
    if len(payload["prompt"].strip()) < 12:
        errors.append("提示词过短，至少写到 12 个字，避免生成结果过于随机。")
    if payload["api_provider"] == "jimmy":
        if not payload.get("first_frame_upload"):
            errors.append("\u8bf7\u4e0a\u4f20\u9996\u5e27\u56fe\u7247\u3002")
        if payload.get("last_frame_upload") and not payload.get("first_frame_upload"):
            errors.append("不能只上传尾帧图片；首帧图片是必填项。")
    elif payload["xibapi_mode"] == "frames":
        if not payload.get("first_frame_upload"):
            errors.append("XIBAPI 首尾帧模式下请上传首帧图片。")
        if payload.get("last_frame_upload") and not payload.get("first_frame_upload"):
            errors.append("XIBAPI 首尾帧模式不能只上传尾帧图片。")
    elif payload["xibapi_mode"] == "reference":
        ref_uploads = payload.get("reference_uploads", [])
        if not ref_uploads:
            errors.append("XIBAPI 参考图模式下请至少上传 1 张参考图。")
        if len(ref_uploads) > 3:
            errors.append("XIBAPI 参考图模式最多上传 3 张参考图。")
    return errors


def build_payload_from_form(api_provider: str, api_key: str, preset_label: str, prompt: str, template_name: str, first_frame_upload: Optional[Dict[str, Any]], last_frame_upload: Optional[Dict[str, Any]], xibapi_mode: str, reference_uploads: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    preset = ORIENTATION_OPTIONS[preset_label]
    template_prefix = PROMPT_TEMPLATES.get(template_name, "").strip()
    final_prompt = f"{template_prefix}\n{prompt.strip()}".strip() if template_prefix else prompt.strip()
    reference_uploads = [item for item in (reference_uploads or []) if item is not None]
    if api_provider == "jimmy":
        model = MODEL_NAME
    elif xibapi_mode == "frames":
        model = XIBAPI_FRAMES_MODEL
    else:
        model = XIBAPI_TEXT_MODEL
    return {
        "api_provider": api_provider,
        "api_key": api_key.strip(),
        "model": model,
        "orientation": preset["orientation"],
        "resolution": preset["resolution"],
        "size": preset["size"],
        "preset_label": preset_label,
        "template_name": template_name,
        "prompt": final_prompt,
        "first_frame_upload": first_frame_upload,
        "last_frame_upload": last_frame_upload,
        "reference_uploads": reference_uploads,
        "reference_names": [item["name"] for item in reference_uploads],
        "xibapi_mode": xibapi_mode,
    }


def render_css() -> None:
    st = _load_streamlit()
    st.markdown("""
        <style>
        .stApp {background:linear-gradient(180deg,#07111f 0%,#0b1730 100%); color:#e5eefc;}
        .top-banner {border:1px solid rgba(148,163,184,.16);border-radius:24px;padding:22px 24px;background:linear-gradient(135deg,rgba(30,41,59,.94),rgba(37,99,235,.18));margin-bottom:1rem;box-shadow:0 18px 45px rgba(2,6,23,.35);}
        .notice-bar {border:1px solid rgba(251,191,36,.28);border-radius:18px;padding:14px 16px;background:linear-gradient(135deg,rgba(120,53,15,.95),rgba(146,64,14,.88));color:#fde68a;margin-bottom:1rem;}
        .panel-card {border:1px solid rgba(148,163,184,.14);border-radius:22px;padding:18px;background:rgba(15,23,42,.72);box-shadow:0 18px 45px rgba(2,6,23,.28);}
        .task-row {border:1px solid rgba(148,163,184,.14);border-radius:20px;padding:14px 14px 10px 14px;margin-bottom:14px;background:linear-gradient(180deg,rgba(15,23,42,.92),rgba(17,24,39,.86));box-shadow:0 12px 30px rgba(2,6,23,.24);}
        .empty-state {padding:32px 20px;border-radius:20px;border:1px dashed rgba(148,163,184,.28);background:rgba(15,23,42,.58);text-align:center;color:#cbd5e1;}
        .section-title {font-size:18px;font-weight:700;color:#f8fafc;margin-bottom:6px;}
        .section-subtitle {font-size:13px;color:#94a3b8;margin-bottom:4px;}
        .batch-bar {border:1px solid rgba(148,163,184,.14);border-radius:18px;padding:14px 16px;background:rgba(15,23,42,.75);margin-bottom:1rem;}
        .task-meta {font-size:12px;color:#94a3b8;}
        </style>
    """, unsafe_allow_html=True)


def maybe_autorefresh(enabled: bool, has_active_tasks: bool) -> bool:
    if not enabled or not has_active_tasks:
        return False
    try:
        from streamlit_autorefresh import st_autorefresh
        st_autorefresh(interval=UI_REFRESH_INTERVAL_MS, key="veo3-task-refresh")
        return True
    except Exception:
        return False


def render_top_section(api_key_ready: bool) -> None:
    st = _load_streamlit()
    st.markdown("<div class='top-banner'>", unsafe_allow_html=True)
    col1, col2, col3 = st.columns([1.5, 1.0, 0.9])
    with col1:
        icon_col, text_col = st.columns([0.16, 0.84])
        with icon_col:
            st.markdown(
                "<div style='width:56px;height:56px;border-radius:18px;background:linear-gradient(135deg,#7c3aed,#2563eb);display:flex;align-items:center;justify-content:center;font-size:26px;'>🎬</div>",
                unsafe_allow_html=True,
            )
        with text_col:
            st.markdown("## 视频任务中心")
            st.caption("集中管理、筛选和处理你的视频生成任务。")
    with col2:
        st.metric("接口状态", "已配置" if api_key_ready else "缺少密钥")
        st.caption("支持 JimmyAI 与 XIBAPI 路由")
    with col3:
        st.metric("任务记录", len(st.session_state.get("veo3_tasks", [])))
        st.caption("本地持久化保存")
    st.markdown("</div>", unsafe_allow_html=True)


def render_expiry_notice() -> None:
    st = _load_streamlit()
    if st.session_state.get("veo3_hide_expiry_notice"):
        return
    bar_col1, bar_col2 = st.columns([0.92, 0.08])
    with bar_col1:
        st.markdown(
            "<div class='notice-bar'><strong>视频链接仅保留 3 天，请及时下载保存。</strong></div>",
            unsafe_allow_html=True,
        )
    with bar_col2:
        if st.button("关闭", key="hide-expiry-notice", use_container_width=True):
            st.session_state.veo3_hide_expiry_notice = True
            persist_state()
            st.rerun()


def render_left_panel() -> Tuple[Optional[Dict[str, Any]], bool, bool, str, bool]:
    st = _load_streamlit()
    apply_pending_form_backfill()
    st.markdown("<div class='panel-card'>", unsafe_allow_html=True)
    st.subheader("创建任务")
    st.caption("根据所选 API 自动切换表单。JimmyAI 使用 JSON + OSS URL，XIBAPI 使用 multipart/form-data。")
    api_key = st.text_input("API 密钥", type="password", key="veo3_api_key", help="格式一般为 sk_xxx，程序会自动补 Bearer。")
    provider_label = st.selectbox("API 类型", list(API_PROVIDER_OPTIONS.keys()), key="veo3_api_provider_label")
    api_provider = API_PROVIDER_OPTIONS[provider_label]
    preset_label = st.selectbox("画面方向与分辨率", list(ORIENTATION_OPTIONS.keys()), key="veo3_preset_label")
    template_name = st.selectbox("提示词模板", list(PROMPT_TEMPLATES.keys()), key="veo3_template_name")
    prompt = st.text_area("提示词", height=180, key="veo3_prompt", placeholder="描述镜头运动、主体变化、光线、质感和过渡方式。")
    st.caption(f"当前字数：{len(prompt.strip())}。建议 >= 12 个字。")
    cached_first_frame, cached_last_frame, cached_reference_uploads = get_form_upload_state()
    first_frame_file = None
    last_frame_file = None
    reference_files: List[Any] = []
    first_frame_upload = cached_first_frame
    last_frame_upload = cached_last_frame
    reference_uploads = list(cached_reference_uploads)
    xibapi_mode_label = st.session_state.get("veo3_xibapi_mode_label", "首尾帧模式")
    xibapi_mode = XIBAPI_MODE_OPTIONS.get(xibapi_mode_label, "frames")
    if api_provider == "jimmy":
        st.text_input("模式", value="首尾帧视频生成", disabled=True)
        st.text_input("模型", value=MODEL_NAME, disabled=True)
        upload_col1, upload_col2 = st.columns(2)
        with upload_col1:
            first_frame_file = st.file_uploader("首帧图片", type=["png", "jpg", "jpeg", "webp", "bmp"], key="jimmy-first-frame")
        with upload_col2:
            last_frame_file = st.file_uploader("尾帧图片（可选）", type=["png", "jpg", "jpeg", "webp", "bmp"], key="jimmy-last-frame")
        if first_frame_file is not None:
            first_frame_upload = serialize_upload(first_frame_file)
        if last_frame_file is not None:
            last_frame_upload = serialize_upload(last_frame_file)
        set_form_upload_state(first_frame_upload, last_frame_upload, [])
        st.write("图片缩略图")
        preview_col1, preview_col2 = st.columns(2)
        with preview_col1:
            render_uploaded_image_preview(first_frame_upload, "首帧缩略图")
        with preview_col2:
            render_uploaded_image_preview(last_frame_upload, "尾帧缩略图")
        st.info("规则：首帧必传，尾帧可选；只上传首帧 + 提示词也可以提交 JimmyAI，尾帧参数会以空值发送。")
    else:
        xibapi_mode_label = st.selectbox("XIBAPI 使用方式", list(XIBAPI_MODE_OPTIONS.keys()), key="veo3_xibapi_mode_label")
        xibapi_mode = XIBAPI_MODE_OPTIONS[xibapi_mode_label]
        st.text_input("模型", value=XIBAPI_FRAMES_MODEL if xibapi_mode == "frames" else XIBAPI_TEXT_MODEL, disabled=True)
        st.text_input("提交方式", value="multipart/form-data", disabled=True)
        if xibapi_mode == "frames":
            upload_col1, upload_col2 = st.columns(2)
            with upload_col1:
                first_frame_file = st.file_uploader("首帧图片", type=["png", "jpg", "jpeg", "webp", "bmp"], key="xibapi-first-frame")
            with upload_col2:
                last_frame_file = st.file_uploader("尾帧图片（可选）", type=["png", "jpg", "jpeg", "webp", "bmp"], key="xibapi-last-frame")
            if first_frame_file is not None:
                first_frame_upload = serialize_upload(first_frame_file)
            if last_frame_file is not None:
                last_frame_upload = serialize_upload(last_frame_file)
            set_form_upload_state(first_frame_upload, last_frame_upload, [])
            st.write("图片缩略图")
            preview_col1, preview_col2 = st.columns(2)
            with preview_col1:
                render_uploaded_image_preview(first_frame_upload, "首帧缩略图")
            with preview_col2:
                render_uploaded_image_preview(last_frame_upload, "尾帧缩略图")
            st.info("XIBAPI 首尾帧模式：使用 `veo_3_1-fast-fl`，通过重复的 `input_reference[]` 提交 1~2 张图。")
        elif xibapi_mode == "reference":
            reference_files = st.file_uploader("参考图（最多 3 张）", type=["png", "jpg", "jpeg", "webp", "bmp"], accept_multiple_files=True, key="xibapi-reference-files")
            if reference_files:
                reference_uploads = [serialize_upload(file_obj) for file_obj in reference_files[:3]]
            set_form_upload_state(None, None, reference_uploads)
            if reference_uploads:
                st.write("参考图缩略图")
                preview_cols = st.columns(min(3, len(reference_uploads)))
                for idx, uploaded in enumerate(reference_uploads):
                    with preview_cols[idx]:
                        render_uploaded_image_preview(uploaded, f"参考图 {idx + 1}")
            else:
                st.caption("当前未上传参考图。")
            st.info("XIBAPI 参考图模式：使用 `veo_3_1-fast`，通过重复的 `input_reference[]` 提交 1~3 张参考图。")
        else:
            clear_form_upload_state()
            st.info("XIBAPI 文生视频：只提交 `model`、`prompt`、`size`，不上传图片。")
    action_col1, action_col2, action_col3 = st.columns(3)
    with action_col1:
        generate_now = st.button("生成视频", type="primary", use_container_width=True)
    with action_col2:
        queue_only = st.button("加入批量队列", use_container_width=True)
    with action_col3:
        clear_form = st.button("清空表单", use_container_width=True)
    st.markdown("</div>", unsafe_allow_html=True)
    if clear_form:
        st.session_state.veo3_prompt = ""
        st.session_state.veo3_template_name = "无模板"
        clear_form_upload_state()
        for uploader_key in ["jimmy-first-frame", "jimmy-last-frame", "xibapi-first-frame", "xibapi-last-frame", "xibapi-reference-files"]:
            if uploader_key in st.session_state:
                st.session_state[uploader_key] = None
        persist_state()
        st.rerun()
    if not generate_now and not queue_only:
        return None, False, False, api_key, False
    payload = build_payload_from_form(
        api_provider=api_provider,
        api_key=api_key,
        preset_label=preset_label,
        prompt=prompt,
        template_name=template_name,
        first_frame_upload=first_frame_upload,
        last_frame_upload=last_frame_upload,
        xibapi_mode=xibapi_mode,
        reference_uploads=reference_uploads,
    )
    errors = validate_payload(payload, require_api_key=(generate_now or queue_only))
    if errors:
        for error in errors:
            st.error(error)
        return None, False, False, api_key, False
    return payload, generate_now, queue_only, api_key, True

def render_status_panel(selected_task: Optional[Dict[str, Any]], api_key_ready: bool, auto_refresh_ready: bool) -> None:
    st = _load_streamlit()
    st.markdown("<div class='panel-card'>", unsafe_allow_html=True)
    st.subheader("任务状态")
    if selected_task is None:
        st.markdown("<div class='empty-state'><strong>等待任务</strong><br/>左侧上传首帧图片（尾帧可选）并填写提示词后，可直接提交任务或加入批量队列。</div>", unsafe_allow_html=True)
        st.markdown("</div>", unsafe_allow_html=True)
        return
    status = selected_task["status"]
    result = selected_task.get("result") or {}
    st.markdown(build_status_badge(status), unsafe_allow_html=True)
    st.write(f"任务 ID：`{get_task_remote_id(selected_task)}`")
    provider_label = "JimmyAI" if selected_task.get("api_provider") == "jimmy" else "XIBAPI"
    mode_text = ""
    if selected_task.get("api_provider") == "xibapi":
        mode_map = {"text": "文生视频", "frames": "首尾帧模式", "reference": "参考图模式"}
        mode_text = f" | {mode_map.get(selected_task.get('xibapi_mode', ''), 'XIBAPI')}"
    st.caption(f"{provider_label}{mode_text} | {selected_task['preset_label']} | 模型 {selected_task['model']} | 创建于 {format_time(selected_task['created_at'])}")
    st.progress(max(0, min(selected_task.get("progress", 0), 100)))
    if status == "local_queue":
        st.info("任务已进入本地队列，等待批量提交。")
    elif status == "submitting":
        st.info("正在向当前 API 提交任务。")
    elif status in {"submitted", "pending", "queued"}:
        st.info("任务已提交，当前处于初始化或排队阶段。")
    elif status == "processing":
        st.info("正在生成视频，系统会继续轮询。")
    elif status == "completed":
        st.success("任务已完成，可以预览并下载。")
    elif status == "failed":
        st.error(selected_task.get("error") or "任务失败，请检查图片、参数、API 密钥和接口配置。")
    st.write("提示词")
    st.code(selected_task["prompt"], language=None)
    if selected_task.get("api_provider") == "xibapi" and selected_task.get("xibapi_mode") == "reference":
        st.caption(f"参考图文件：{', '.join(selected_task.get('reference_names', [])) or '未上传'}")
    else:
        st.caption(f"\u9996\u5e27\u6587\u4ef6\uff1a{selected_task.get('first_frame_name', '-')}")
        if selected_task.get("first_frame_url"):
            st.caption(f"\u9996\u5e27 OSS URL\uff1a{selected_task['first_frame_url']}")
        st.caption(f"\u5c3e\u5e27\u6587\u4ef6\uff1a{selected_task.get('last_frame_name', '-')}")
        if selected_task.get("last_frame_url"):
            st.caption(f"\u5c3e\u5e27 OSS URL\uff1a{selected_task['last_frame_url']}")
        elif selected_task.get("api_provider") == "jimmy":
            st.caption("尾帧 OSS URL：未填写，已按空值提交")
    st.write("图片预览")
    if selected_task.get("api_provider") == "xibapi" and selected_task.get("xibapi_mode") == "reference":
        reference_uploads = selected_task.get("reference_uploads", [])
        if reference_uploads:
            ref_cols = st.columns(min(3, len(reference_uploads)))
            for idx, uploaded in enumerate(reference_uploads[:3]):
                with ref_cols[idx]:
                    render_uploaded_image_preview(uploaded, f"参考图 {idx + 1}")
        else:
            st.caption("当前模式未上传参考图。")
    else:
        preview_col1, preview_col2 = st.columns(2)
        with preview_col1:
            render_uploaded_image_preview(selected_task.get("first_frame_upload"), "首帧预览")
        with preview_col2:
            render_uploaded_image_preview(selected_task.get("last_frame_upload"), "尾帧预览")
    if selected_task.get("video_url"):
        st.write("视频预览")
        st.video(selected_task["video_url"])
    action_col1, action_col2, action_col3 = st.columns(3)
    with action_col1:
        if st.button("回填到表单", key=f"detail-backfill-{selected_task['local_id']}", use_container_width=True):
            populate_form_from_task(selected_task)
            persist_state()
            st.rerun()
    with action_col2:
        if selected_task.get("video_url"):
            st.link_button("下载视频", selected_task["video_url"], use_container_width=True)
        else:
            st.button("下载视频", key=f"detail-download-disabled-{selected_task['local_id']}", use_container_width=True, disabled=True)
    with action_col3:
        if st.button("删除任务", key=f"detail-delete-{selected_task['local_id']}", use_container_width=True):
            st.session_state.veo3_detail_dialog_task_id = None
            st.session_state.veo3_delete_confirm_ids = [selected_task["local_id"]]
            st.rerun()
    with st.expander("任务返回详情", expanded=False):
        st.json({
            "status": result.get("status"),
            "progress": result.get("progress"),
            "created_at": format_optional_timestamp(result.get("created_at")),
            "updated_at": format_optional_timestamp(result.get("updated_at")),
            "completed_at": format_optional_timestamp(result.get("completed_at")),
            "video_url": extract_video_url(result),
        })
    if status in ACTIVE_REMOTE_STATUSES and not auto_refresh_ready:
        st.caption("当前环境未启用自动刷新扩展，点击“刷新任务状态”即可轮询。")
    if status == "failed" and not api_key_ready:
        st.caption("若要重新提交失败任务，请先重新填写 API 密钥。")
    st.markdown("</div>", unsafe_allow_html=True)


def render_task_detail_dialog(task_id: Optional[str], api_key_ready: bool, auto_refresh_ready: bool) -> None:
    st = _load_streamlit()
    if not task_id:
        return

    @st.dialog("任务详情", width="large")
    def _show_dialog() -> None:
        task = get_task_by_local_id(task_id)
        if task is None:
            st.warning("该任务已不存在。")
            if st.button("关闭", key="detail-dialog-close-missing", use_container_width=True):
                st.session_state.veo3_detail_dialog_task_id = None
                st.rerun()
            return
        render_status_panel(task, api_key_ready=api_key_ready, auto_refresh_ready=auto_refresh_ready)
        if st.button("关闭详情", key=f"detail-dialog-close-{task_id}", use_container_width=True):
            st.session_state.veo3_detail_dialog_task_id = None
            st.rerun()

    _show_dialog()


def render_delete_confirm_dialog() -> None:
    st = _load_streamlit()
    delete_ids = list(st.session_state.get("veo3_delete_confirm_ids", []))
    if not delete_ids:
        return

    @st.dialog("确认删除", width="small")
    def _show_dialog() -> None:
        existing_tasks = [task for task_id in delete_ids if (task := get_task_by_local_id(task_id))]
        count = len(existing_tasks)
        if count == 0:
            st.warning("没有可删除的任务。")
        elif count == 1:
            task = existing_tasks[0]
            st.warning("删除后无法恢复。")
            st.write(f"确认删除任务：`{task['local_id']}`")
            st.caption(truncate_text(task.get("prompt", ""), 80))
        else:
            st.warning(f"将删除 {count} 条任务记录，删除后无法恢复。")
            for task in existing_tasks[:5]:
                st.caption(f"• {task['local_id']} | {truncate_text(task.get('prompt', ''), 36)}")
            if count > 5:
                st.caption(f"其余 {count - 5} 条任务将一并删除。")

        action_col1, action_col2 = st.columns(2)
        with action_col1:
            if st.button("取消", key="delete-dialog-cancel", use_container_width=True):
                st.session_state.veo3_delete_confirm_ids = []
                st.rerun()
        with action_col2:
            if st.button("确认删除", key="delete-dialog-confirm", type="primary", use_container_width=True, disabled=count == 0):
                for task in existing_tasks:
                    remove_task(task["local_id"])
                st.session_state.veo3_delete_confirm_ids = []
                st.session_state.veo3_notice = ("success", f"已删除 {count} 条任务。")
                persist_state()
                st.rerun()

    _show_dialog()


def build_export_excel_bytes(tasks: List[Dict[str, Any]]) -> bytes:
    import io
    import pandas as pd

    rows = []
    for task in tasks:
        rows.append({
            "任务ID": get_task_remote_id(task),
            "状态": get_task_status_label(task),
            "路由": get_task_route_label(task),
            "类型": get_task_type_label(task),
            "模型": task.get("model", ""),
            "提示词": task.get("prompt", ""),
            "创建时间": format_time(task.get("created_at", 0)),
            "视频链接": task.get("video_url", ""),
        })
    buffer = io.BytesIO()
    pd.DataFrame(rows).to_excel(buffer, index=False)
    buffer.seek(0)
    return buffer.getvalue()


def render_filters_bar(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    st = _load_streamlit()
    if st.session_state.get("veo3_filter_reset_requested"):
        st.session_state.veo3_filter_time_range = "最近 7 天"
        st.session_state.veo3_filter_search = ""
        st.session_state.veo3_filter_status = "全部"
        st.session_state.veo3_filter_route = "全部"
        st.session_state.veo3_filter_type = "全部"
        st.session_state.veo3_filter_draft_time_range = "最近 7 天"
        st.session_state.veo3_filter_draft_search = ""
        st.session_state.veo3_filter_draft_status = "全部"
        st.session_state.veo3_filter_draft_route = "全部"
        st.session_state.veo3_filter_draft_type = "全部"
        st.session_state.veo3_filter_reset_requested = False
        persist_state()

    st.markdown("<div class='panel-card'>", unsafe_allow_html=True)
    st.markdown("<div class='section-title'>筛选区</div>", unsafe_allow_html=True)
    st.markdown("<div class='section-subtitle'>按时间、状态、路由和关键词筛选当前任务结果。</div>", unsafe_allow_html=True)

    row1 = st.columns([1.0, 1.4, 1.0, 1.0, 1.0, 0.7])
    with row1[0]:
        st.selectbox("时间范围", ["最近 24 小时", "最近 3 天", "最近 7 天", "最近 30 天", "全部"], key="veo3_filter_draft_time_range")
    with row1[1]:
        st.text_input("搜索", key="veo3_filter_draft_search", placeholder="按任务 ID 或提示词搜索")
    with row1[2]:
        st.selectbox("状态", ["全部", "排队中", "生成中", "已完成", "失败"], key="veo3_filter_draft_status")
    with row1[3]:
        st.selectbox("路由", ["全部", "Jimmy 路由", "XIBAPI 路由"], key="veo3_filter_draft_route")
    with row1[4]:
        st.selectbox("类型", ["全部", "文生视频", "首尾帧", "参考图"], key="veo3_filter_draft_type")
    with row1[5]:
        refresh_now = st.button("刷新", key="filter-refresh", use_container_width=True)

    row2 = st.columns([0.9, 0.9, 1.1, 2.1])
    with row2[0]:
        reset_now = st.button("重置", key="filter-reset", use_container_width=True)
    with row2[1]:
        search_now = st.button("搜索", key="filter-search", type="primary", use_container_width=True)
    with row2[2]:
        st.download_button(
            "导出 Excel",
            data=build_export_excel_bytes([task for task in tasks if task_matches_filters(task)]),
            file_name="video_task_center_export.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

    if reset_now:
        st.session_state.veo3_filter_reset_requested = True
        st.rerun()

    if search_now:
        st.session_state.veo3_filter_time_range = st.session_state.veo3_filter_draft_time_range
        st.session_state.veo3_filter_search = st.session_state.veo3_filter_draft_search
        st.session_state.veo3_filter_status = st.session_state.veo3_filter_draft_status
        st.session_state.veo3_filter_route = st.session_state.veo3_filter_draft_route
        st.session_state.veo3_filter_type = st.session_state.veo3_filter_draft_type
        persist_state()
        st.rerun()

    if refresh_now:
        changed, _ = poll_remote_tasks()
        st.session_state.veo3_last_global_poll_at = time.time()
        if changed:
            persist_state()
        st.rerun()

    st.markdown("</div>", unsafe_allow_html=True)
    return [task for task in tasks if task_matches_filters(task)]


def render_batch_bar(filtered_tasks: List[Dict[str, Any]]) -> None:
    st = _load_streamlit()
    selected_ids = list(st.session_state.get("veo3_selected_ids", []))
    selected_tasks = [task for task in filtered_tasks if task["local_id"] in selected_ids]
    downloadable = [task for task in selected_tasks if task.get("video_url")]

    st.markdown("<div class='batch-bar'>", unsafe_allow_html=True)
    bar_col1, bar_col2, bar_col3, bar_col4 = st.columns([2.0, 1.2, 1.0, 1.0])
    with bar_col1:
        st.markdown(f"**批量操作：已选择 {len(selected_tasks)} 项**")
    with bar_col2:
        if st.button("下载已选视频", key="batch-download", use_container_width=True, disabled=not downloadable):
            st.session_state.veo3_notice = ("info", f"已准备 {len(downloadable)} 个可下载视频链接。")
    with bar_col3:
        if st.button("删除已选任务", key="batch-delete", use_container_width=True, disabled=not selected_tasks):
            st.session_state.veo3_delete_confirm_ids = [task["local_id"] for task in selected_tasks]
            st.rerun()
    with bar_col4:
        if st.button("清空选择", key="batch-clear", use_container_width=True, disabled=not selected_tasks):
            st.session_state.veo3_selected_ids = []
            persist_state()
            st.rerun()
    if downloadable:
        with st.expander("已选任务下载链接", expanded=False):
            for idx, task in enumerate(downloadable, start=1):
                st.link_button(f"下载视频 {idx}", task["video_url"], use_container_width=True)
    st.markdown("</div>", unsafe_allow_html=True)


def render_task_list(api_key: str) -> bool:
    st = _load_streamlit()
    tasks = st.session_state.veo3_tasks
    st.markdown("<div class='section-title'>任务卡片</div>", unsafe_allow_html=True)
    task_stats = {
        "排队中": len([task for task in tasks if get_task_status_label(task) == "排队中"]),
        "生成中": len([task for task in tasks if get_task_status_label(task) == "生成中"]),
        "已完成": len([task for task in tasks if get_task_status_label(task) == "已完成"]),
        "失败": len([task for task in tasks if get_task_status_label(task) == "失败"]),
    }
    stats_cols = st.columns(4)
    for idx, (label, value) in enumerate(task_stats.items()):
        stats_cols[idx].metric(label, value)
    control_cols = st.columns([1.0, 1.0, 1.0])
    with control_cols[0]:
        submit_pending = st.button("提交待发任务", use_container_width=True)
    with control_cols[1]:
        auto_refresh = st.toggle("自动刷新", key="veo3_auto_refresh")
    with control_cols[2]:
        if st.button("立即刷新", use_container_width=True):
            poll_remote_tasks()
            st.rerun()
    if submit_pending:
        pending_count = 0
        for task in tasks:
            if task["status"] != "local_queue":
                continue
            pending_count += 1
            try:
                submit_task(task)
            except Exception as exc:
                logging.exception("submit_pending failed")
                task["status"] = "failed"
                task["error"] = str(exc)
                task["updated_at"] = time.time()
        st.session_state.veo3_notice = ("success", f"已提交 {pending_count} 个待提交任务。") if pending_count else ("info", "当前没有待提交任务。")
        st.rerun()

    filtered_tasks = render_filters_bar(tasks)
    render_batch_bar(filtered_tasks)
    if not filtered_tasks:
        st.markdown("<div class='empty-state'><strong>暂无匹配任务</strong><br/>可以调整筛选条件，或在侧栏创建新任务。</div>", unsafe_allow_html=True)
        return auto_refresh

    selected_ids = set(st.session_state.get("veo3_selected_ids", []))
    columns_per_row = 4
    for start in range(0, len(filtered_tasks), columns_per_row):
        row_tasks = filtered_tasks[start:start + columns_per_row]
        row_cols = st.columns(columns_per_row)
        for idx, task in enumerate(row_tasks):
            with row_cols[idx]:
                st.markdown("<div class='task-row'>", unsafe_allow_html=True)
                top_cols = st.columns([0.78, 0.22])
                with top_cols[0]:
                    st.markdown(get_task_badge_html(task), unsafe_allow_html=True)
                with top_cols[1]:
                    checked = st.checkbox("选择", key=f"card-select-{task['local_id']}", value=task["local_id"] in selected_ids, label_visibility="collapsed")
                    if checked:
                        selected_ids.add(task["local_id"])
                    else:
                        selected_ids.discard(task["local_id"])
                preview = get_task_preview_upload(task)
                if preview and preview.get("data"):
                    st.image(preview["data"], use_container_width=True)
                else:
                    st.markdown("<div class='empty-state' style='padding:18px 10px;'>暂无预览</div>", unsafe_allow_html=True)
                top_action_cols = st.columns([1.0, 1.0])
                with top_action_cols[0]:
                    if st.button("查看详情", key=f"open-{task['local_id']}", use_container_width=True):
                        st.session_state.veo3_selected_task = task["local_id"]
                        st.session_state.veo3_detail_dialog_task_id = task["local_id"]
                        st.rerun()
                with top_action_cols[1]:
                    if st.button("回填参数", key=f"redo-{task['local_id']}", use_container_width=True):
                        populate_form_from_task(task)
                        st.rerun()
                st.markdown(f"**{truncate_text(task.get('prompt',''), 64)}**")
                st.caption(f"任务ID：{truncate_text(get_task_remote_id(task), 22)}")
                st.caption(f"`{task.get('model','')}`")
                st.caption(f"{get_task_type_label(task)} | {format_time(task['created_at'])}")
                action_cols = st.columns([1.0, 1.0])
                with action_cols[0]:
                    if task["status"] == "completed" and task.get("video_url"):
                        st.link_button("下载视频", task["video_url"], use_container_width=True)
                    else:
                        st.button("下载视频", key=f"download-disabled-{task['local_id']}", use_container_width=True, disabled=True)
                with action_cols[1]:
                    if st.button("删除任务", key=f"delete-{task['local_id']}", use_container_width=True):
                        st.session_state.veo3_delete_confirm_ids = [task["local_id"]]
                        st.rerun()
                st.markdown("</div>", unsafe_allow_html=True)
    st.session_state.veo3_selected_ids = list(selected_ids)
    return auto_refresh


def show_notice() -> None:
    st = _load_streamlit()
    notice = st.session_state.veo3_notice
    if not notice:
        return
    level, message = notice
    if level == "success":
        st.success(message)
    elif level == "error":
        st.error(message)
    else:
        st.info(message)
    st.session_state.veo3_notice = None


def _load_streamlit():
    import streamlit as st
    return st


def render_app() -> None:
    st = _load_streamlit()
    st.set_page_config(page_title="VEO 任务中心", layout="wide")
    ensure_state()
    render_css()
    top_placeholder = st.empty()
    with st.sidebar:
        payload, generate_now, queue_only, api_key, has_valid_submission = render_left_panel()
    api_key_ready = bool(api_key.strip())
    if has_valid_submission and payload is not None:
        task = create_task_record(payload, queued_locally=queue_only)
        add_task(task)
        if queue_only:
            st.session_state.veo3_notice = ("success", f"任务已加入批量队列：{task['local_id']}")
            st.rerun()
        if generate_now:
            try:
                submit_task(task)
                st.session_state.veo3_notice = ("success", f"任务已提交：{task['remote_task_id'] or task['local_id']}")
            except Exception as exc:
                logging.exception("initial submit_task failed")
                task["status"] = "failed"
                task["error"] = str(exc)
                task["updated_at"] = time.time()
                st.session_state.veo3_notice = ("error", str(exc))
            st.rerun()
    has_active_tasks = any(task["status"] in ACTIVE_REMOTE_STATUSES and task.get("remote_task_id") for task in st.session_state.veo3_tasks)
    if has_active_tasks and st.session_state.get("veo3_auto_refresh"):
        now = time.time()
        if now - st.session_state.get("veo3_last_global_poll_at", 0.0) >= POLL_INTERVAL_SECONDS:
            changed, has_active_tasks = poll_remote_tasks()
            st.session_state.veo3_last_global_poll_at = now
            if changed:
                persist_state()
    auto_refresh_ready = can_use_autorefresh()
    with top_placeholder.container():
        render_top_section(api_key_ready)
        render_expiry_notice()
        show_notice()
    auto_refresh_enabled = render_task_list(api_key)
    render_task_detail_dialog(
        st.session_state.get("veo3_detail_dialog_task_id"),
        api_key_ready=api_key_ready,
        auto_refresh_ready=auto_refresh_ready,
    )
    render_delete_confirm_dialog()
    maybe_autorefresh(auto_refresh_enabled, has_active_tasks)


if __name__ == "__main__":
    render_app()
