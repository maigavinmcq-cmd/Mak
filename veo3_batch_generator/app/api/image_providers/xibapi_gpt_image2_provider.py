from __future__ import annotations

from pathlib import Path
import threading
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter

from app.api.base_provider import BaseImageProvider
from app.api.image_api import ImageGenerationResult
from app.api.response_parser import extract_error_message, extract_image_url, extract_status, extract_task_id, extract_video_url
from app.api.upload_api import upload_image_for_public_url
from app.file_utils import download_file, file_to_data_url


_ACTIVE_STATUSES = {"", "queued", "pending", "in_progress", "processing", "running"}
_COMPLETED_STATUSES = {"completed", "succeeded", "success"}
_FAILED_STATUSES = {"failed", "error", "cancelled", "canceled"}
_RETRYABLE_HTTP_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
_SUPPORTED_ASPECT_RATIOS = {"1:1", "5:4", "9:16", "21:9", "16:9", "3:2", "4:3", "4:5", "3:4", "2:3"}


class XibapiGptImage2Provider(BaseImageProvider):
    """xibapi GPT-image provider using the async `/v1/videos` image task API."""

    provider_key = "xibapi_gpt_image2"
    provider_name = "xibapi GPT Image"
    supports_async_image_tasks = True
    model_options = [
        {
            "logical_key": "image_2",
            "display_name": "GPT Image 2",
            "provider_value": "gpt-image-2",
        },
        {
            "logical_key": "gpt_image_2",
            "display_name": "GPT Image 2",
            "provider_value": "gpt-image-2",
        },
        {
            "logical_key": "gpt_image_2_2k",
            "display_name": "GPT Image 2 2K",
            "provider_value": "gpt-image-2-2K",
        },
        {
            "logical_key": "gpt_image_2_4k",
            "display_name": "GPT Image 2 4K",
            "provider_value": "gpt-image-2-4K",
        },
    ]

    def __init__(self) -> None:
        self._thread_local = threading.local()

    def _session(self) -> requests.Session:
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            adapter = HTTPAdapter(pool_connections=100, pool_maxsize=100)
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            self._thread_local.session = session
        return session

    def submit_image_task(
        self,
        product_image_path: str,
        prompt: str,
        model_logical_key: str,
        api_key: str,
        extra_params: dict[str, Any] | None = None,
    ) -> ImageGenerationResult:
        params = extra_params or {}
        validation_error = self._validate_common(prompt, api_key, params)
        if validation_error:
            return ImageGenerationResult(False, error_message=validation_error)

        base_url = self._base_url(str(params.get("base_url") or ""))
        endpoint = f"{base_url}/v1/videos"
        model = self.get_model_option(model_logical_key)["provider_value"]
        reference_urls, reference_error = self._reference_urls(product_image_path, params)
        if reference_error:
            return ImageGenerationResult(False, error_message=reference_error)
        payload = {
            "model": model,
            "prompt": str(prompt).strip(),
            "metadata": {
                "aspect_ratio": self._aspect_ratio(params),
                "urls": reference_urls,
            },
        }
        headers = self._headers(api_key)
        timeout = int(params.get("timeout") or 180)
        retry_count = max(1, int(params.get("retry_count") or 3))
        retry_interval_seconds = max(0, int(params.get("retry_interval_seconds") or 5))
        last_raw: Any = None
        last_error = ""

        for attempt in range(1, retry_count + 1):
            try:
                response = self._session().post(endpoint, headers=headers, json=payload, timeout=timeout)
                raw = self._json_or_text(response)
                last_raw = raw
                if not response.ok:
                    last_error = f"xibapi GPT-image2 async submit failed: HTTP {response.status_code} {self._short_payload(raw)}"
                    if response.status_code in _RETRYABLE_HTTP_STATUSES and attempt < retry_count:
                        self._sleep_or_cancel(retry_interval_seconds * attempt, params.get("stop_event"))
                        continue
                    return ImageGenerationResult(False, raw_response=raw, error_message=last_error)
                return self._result_from_submit_response(raw, params)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = f"xibapi GPT-image2 async submit network error: {exc}"
                if attempt < retry_count:
                    self._sleep_or_cancel(retry_interval_seconds * attempt, params.get("stop_event"))
                    continue
                return ImageGenerationResult(False, raw_response=last_raw, error_message=last_error)
            except Exception as exc:
                return ImageGenerationResult(
                    False,
                    raw_response=last_raw,
                    error_message=f"xibapi GPT-image2 async submit error: {exc}",
                )

        return ImageGenerationResult(False, raw_response=last_raw, error_message=last_error or "xibapi GPT-image2 async submit failed")

    def poll_image_task_once(
        self,
        task_id: str,
        api_key: str,
        extra_params: dict[str, Any] | None = None,
    ) -> ImageGenerationResult:
        params = extra_params or {}
        task_id = str(task_id or "").strip()
        if not task_id:
            return ImageGenerationResult(False, error_message="xibapi GPT-image2 image task_id is empty")
        if not str(api_key or "").strip():
            return ImageGenerationResult(False, task_id=task_id, error_message="xibapi GPT-image2 API Key is empty")
        base_url = self._base_url(str(params.get("base_url") or ""))
        timeout = int(params.get("timeout") or 180)
        endpoint = f"{base_url}/v1/videos/{task_id}"

        try:
            response = self._session().get(endpoint, headers=self._headers(api_key), timeout=timeout)
            raw = self._json_or_text(response)
            if not response.ok:
                error = f"xibapi GPT-image2 async poll failed: HTTP {response.status_code} {self._short_payload(raw)}"
                if response.status_code in _RETRYABLE_HTTP_STATUSES:
                    return ImageGenerationResult(False, task_id=task_id, status="in_progress", raw_response=raw, error_message=error)
                return ImageGenerationResult(False, task_id=task_id, raw_response=raw, error_message=error)
            return self._result_from_poll_response(task_id, raw, params)
        except (requests.Timeout, requests.ConnectionError) as exc:
            return ImageGenerationResult(
                False,
                task_id=task_id,
                status="in_progress",
                error_message=f"xibapi GPT-image2 async poll network error: {exc}",
            )
        except Exception as exc:
            return ImageGenerationResult(False, task_id=task_id, error_message=f"xibapi GPT-image2 async poll error: {exc}")

    def generate_image(
        self,
        product_image_path: str,
        prompt: str,
        model_logical_key: str,
        api_key: str,
        extra_params: dict[str, Any] | None = None,
    ) -> ImageGenerationResult:
        params = dict(extra_params or {})
        result = self.submit_image_task(product_image_path, prompt, model_logical_key, api_key, params)
        if result.success or not result.task_id:
            return result
        if str(result.status or "").lower() not in _ACTIVE_STATUSES:
            return result

        poll_interval = max(1, int(params.get("poll_interval_seconds") or 5))
        max_poll_count = max(1, int(params.get("max_poll_count") or 120))
        for _ in range(max_poll_count):
            if self._sleep_or_cancel(poll_interval, params.get("stop_event")):
                return ImageGenerationResult(False, task_id=result.task_id, status="in_progress", raw_response=result.raw_response, error_message="xibapi GPT-image2 image polling stopped")
            result = self.poll_image_task_once(result.task_id, api_key, params)
            if result.success:
                return result
            if str(result.status or "").lower() not in _ACTIVE_STATUSES:
                return result
        return ImageGenerationResult(
            False,
            task_id=result.task_id,
            status="in_progress",
            raw_response=result.raw_response,
            error_message=f"xibapi GPT-image2 image polling exceeded max count {max_poll_count}",
        )

    def _result_from_submit_response(self, raw: Any, params: dict[str, Any]) -> ImageGenerationResult:
        if not isinstance(raw, dict):
            return ImageGenerationResult(False, raw_response=raw, error_message=f"xibapi GPT-image2 async submit returned non-JSON: {self._short_payload(raw)}")
        task_id = extract_task_id(raw) or ""
        status = (extract_status(raw) or "").lower()
        if status in _FAILED_STATUSES:
            return ImageGenerationResult(False, task_id=task_id, status=status, raw_response=raw, error_message=self._error_message(raw))
        image_url = self._image_url(raw)
        if image_url and status in _COMPLETED_STATUSES:
            return self._save_image_result(task_id, image_url, raw, params)
        if task_id and status in _ACTIVE_STATUSES:
            return ImageGenerationResult(False, task_id=task_id, status=status or "queued", raw_response=raw)
        if task_id:
            return ImageGenerationResult(False, task_id=task_id, status=status or "queued", raw_response=raw)
        return ImageGenerationResult(False, raw_response=raw, error_message="xibapi GPT-image2 async submit response did not include task id")

    def _result_from_poll_response(self, task_id: str, raw: Any, params: dict[str, Any]) -> ImageGenerationResult:
        if not isinstance(raw, dict):
            return ImageGenerationResult(False, task_id=task_id, raw_response=raw, error_message=f"xibapi GPT-image2 async poll returned non-JSON: {self._short_payload(raw)}")
        status = (extract_status(raw) or "").lower()
        image_url = self._image_url(raw)
        if status in _COMPLETED_STATUSES:
            if not image_url:
                return ImageGenerationResult(False, task_id=task_id, status=status, raw_response=raw, error_message="xibapi GPT-image2 image task completed but did not return video_url")
            return self._save_image_result(task_id, image_url, raw, params)
        if status in _FAILED_STATUSES:
            return ImageGenerationResult(False, task_id=task_id, status=status, raw_response=raw, error_message=self._error_message(raw))
        if status in _ACTIVE_STATUSES:
            return ImageGenerationResult(False, task_id=task_id, status=status or "queued", raw_response=raw)
        if image_url:
            return self._save_image_result(task_id, image_url, raw, params)
        return ImageGenerationResult(False, task_id=task_id, status=status, raw_response=raw, error_message=self._error_message(raw) or f"xibapi GPT-image2 image task returned unknown status: {status or '-'}")

    def _save_image_result(self, task_id: str, image_url: str, raw: Any, params: dict[str, Any]) -> ImageGenerationResult:
        output_path = str(params.get("output_path") or "")
        timeout = int(params.get("timeout") or 180)
        retry_count = max(1, int(params.get("retry_count") or 3))
        retry_interval_seconds = max(0, int(params.get("retry_interval_seconds") or 5))
        saved_path = ""
        if output_path:
            try:
                saved_path = download_file(
                    image_url,
                    output_path,
                    timeout=timeout,
                    retry_count=retry_count,
                    retry_interval_seconds=retry_interval_seconds,
                )
            except Exception as exc:
                return ImageGenerationResult(
                    False,
                    image_url=image_url,
                    task_id=task_id,
                    status="completed",
                    raw_response=raw,
                    error_message=f"xibapi GPT-image2 image result returned but download failed: {exc}",
                )
        return ImageGenerationResult(True, image_path=saved_path, image_url=image_url, task_id=task_id, status="completed", raw_response=raw)

    @staticmethod
    def _validate_common(prompt: str, api_key: str, params: dict[str, Any]) -> str:
        if not str(api_key or "").strip():
            return "xibapi GPT-image2 API Key is empty"
        if not str(prompt or "").strip():
            return "xibapi GPT-image2 prompt is empty"
        base_url = str(params.get("base_url") or "").strip()
        if not base_url or "YOUR_API_HOST" in base_url:
            return "xibapi GPT-image2 API Base URL is empty or still uses YOUR_API_HOST"
        return ""

    @staticmethod
    def _base_url(base_url: str) -> str:
        return str(base_url or "").strip().rstrip("/")

    @staticmethod
    def _headers(api_key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {str(api_key).strip()}", "Content-Type": "application/json"}

    @classmethod
    def _aspect_ratio(cls, params: dict[str, Any]) -> str:
        configured = str(params.get("aspect_ratio") or "").strip()
        if configured in _SUPPORTED_ASPECT_RATIOS:
            return configured
        size = str(params.get("size") or params.get("image_size") or "").strip().lower()
        ratio = cls._aspect_ratio_from_size(size)
        if ratio:
            return ratio
        return "9:16"

    @staticmethod
    def _aspect_ratio_from_size(size: str) -> str:
        if "x" not in size:
            return ""
        left, right = size.split("x", 1)
        try:
            width = int(left.strip())
            height = int(right.strip())
        except ValueError:
            return ""
        if width <= 0 or height <= 0:
            return ""
        pairs = {
            (1, 1): "1:1",
            (5, 4): "5:4",
            (9, 16): "9:16",
            (21, 9): "21:9",
            (16, 9): "16:9",
            (3, 2): "3:2",
            (4, 3): "4:3",
            (4, 5): "4:5",
            (3, 4): "3:4",
            (2, 3): "2:3",
        }
        from math import gcd

        divisor = gcd(width, height)
        return pairs.get((width // divisor, height // divisor), "")

    @classmethod
    def _reference_urls(cls, product_image_path: str, params: dict[str, Any]) -> tuple[list[str], str]:
        local_values: list[str] = []
        url_values: list[str] = []

        text = str(product_image_path or "").strip()
        if text:
            local_values.append(text)
        for value in list(params.get("input_images") or []):
            text = str(value or "").strip()
            if text:
                local_values.append(text)
        for value in list(params.get("input_image_urls") or []):
            text = str(value or "").strip()
            if text:
                url_values.append(text)

        upload_api_url = str(params.get("image_upload_api_url") or "").strip()
        if upload_api_url:
            uploaded_refs: list[str] = []
            upload_errors: list[str] = []
            uploaded_local_paths: set[str] = set()
            for value in local_values:
                path = cls._local_path(value)
                if path is None:
                    continue
                try:
                    path_key = str(path.resolve()).lower()
                except OSError:
                    path_key = str(path).lower()
                if path_key in uploaded_local_paths:
                    continue
                uploaded_local_paths.add(path_key)
                url, raw, error = upload_image_for_public_url(
                    str(path),
                    upload_api_url,
                    api_key=str(params.get("image_upload_api_key") or ""),
                    file_field=str(params.get("image_upload_file_field") or "file"),
                    timeout=int(params.get("timeout") or 180),
                )
                if url:
                    cls._append_ref(uploaded_refs, url)
                elif error:
                    upload_errors.append(error)
            if uploaded_refs:
                return uploaded_refs[:5], ""
            if local_values and upload_errors:
                return [], f"xibapi GPT-image2 reference image OSS upload failed: {upload_errors[0]}"

        refs: list[str] = []
        fallback_values = url_values if url_values else local_values
        for value in fallback_values:
            ref = cls._normalise_reference(value)
            if not ref:
                continue
            cls._append_ref(refs, ref)
            if len(refs) >= 5:
                break
        return refs, ""

    @staticmethod
    def _append_ref(refs: list[str], ref: str) -> None:
        key = ref.lower() if ref.startswith(("http://", "https://", "data:image")) else ref
        existing = {item.lower() if item.startswith(("http://", "https://", "data:image")) else item for item in refs}
        if key not in existing:
            refs.append(ref)

    @staticmethod
    def _normalise_reference(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if text.startswith(("http://", "https://", "data:image")):
            return text
        path = Path(text)
        if not path.exists():
            return ""
        return file_to_data_url(path)

    @staticmethod
    def _local_path(value: str) -> Path | None:
        text = str(value or "").strip()
        if not text or text.startswith(("http://", "https://", "data:image")):
            return None
        path = Path(text)
        if path.exists() and path.is_file():
            return path
        return None

    @staticmethod
    def _image_url(raw: Any) -> str:
        return extract_video_url(raw) or extract_image_url(raw) or ""

    @staticmethod
    def _error_message(raw: Any) -> str:
        return extract_error_message(raw) or "xibapi GPT-image2 image task failed"

    @staticmethod
    def _json_or_text(response: requests.Response) -> dict[str, Any] | str:
        try:
            return response.json()
        except ValueError:
            return response.text

    @staticmethod
    def _short_payload(payload: Any, limit: int = 800) -> str:
        text = str(payload)
        if len(text) > limit:
            return text[:limit] + "..."
        return text

    @staticmethod
    def _sleep_or_cancel(seconds: int, stop_event: Any) -> bool:
        deadline = time.monotonic() + max(0, int(seconds or 0))
        while time.monotonic() < deadline:
            if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                return True
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        return bool(stop_event is not None and getattr(stop_event, "is_set", lambda: False)())
