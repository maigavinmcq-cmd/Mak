from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import requests

from app.api.base_provider import BaseImageProvider
from app.api.image_api import ImageGenerationResult
from app.api.response_parser import extract_error_message, extract_image_url, extract_status, extract_task_id
from app.file_utils import download_file, file_to_data_url


class XibapiNanoBananaProvider(BaseImageProvider):
    provider_key = "xibapi_nano_banana"
    provider_name = "xibapi Nano Banana"
    default_base_url = "https://xibapi.com"
    model_options = [
        {
            "logical_key": "nano_banana_2",
            "display_name": "Nano Banana 2 标准版",
            "provider_value": "nano_banana_2",
        },
        {
            "logical_key": "nano_banana_pro",
            "display_name": "Nano Banana Pro",
            "provider_value": "nano_banana_pro",
        },
        {
            "logical_key": "nano_banana_pro_1k",
            "display_name": "Nano Banana Pro 1K",
            "provider_value": "nano_banana_pro-1K",
        },
        {
            "logical_key": "nano_banana_pro_2k",
            "display_name": "Nano Banana Pro 2K",
            "provider_value": "nano_banana_pro-2K",
        },
        {
            "logical_key": "nano_banana_pro_4k",
            "display_name": "Nano Banana Pro 4K",
            "provider_value": "nano_banana_pro-4K",
        },
    ]

    def generate_image(
        self,
        product_image_path: str,
        prompt: str,
        model_logical_key: str,
        api_key: str,
        extra_params: dict[str, Any] | None = None,
    ) -> ImageGenerationResult:
        params = extra_params or {}
        if not api_key.strip():
            return ImageGenerationResult(False, error_message="IMAGE_API_KEY 为空")
        if not prompt.strip():
            return ImageGenerationResult(False, error_message="图片提示词为空")
        path = Path(product_image_path)
        if not path.exists():
            return ImageGenerationResult(False, error_message=f"产品图片不存在: {path}")

        model = self.get_model_option(model_logical_key)["provider_value"]
        output_path = str(params.get("output_path") or "")
        base_url = self._base_url(str(params.get("base_url") or ""))
        timeout = int(params.get("timeout") or 120)
        retry_count = max(1, int(params.get("retry_count") or 3))
        retry_interval = max(1, int(params.get("retry_interval_seconds") or 5))
        poll_interval = max(1, int(params.get("poll_interval_seconds") or 5))
        max_poll_count = max(1, int(params.get("max_poll_count") or 120))
        on_task_id = params.get("on_task_id")
        stop_event = params.get("stop_event")

        metadata = {
            "aspectRatio": str(params.get("aspect_ratio") or "auto"),
            "urls": [file_to_data_url(path)],
        }
        image_size = self._image_size(model, str(params.get("image_size") or ""))
        if image_size:
            metadata["imageSize"] = image_size

        payload = {
            "model": model,
            "prompt": prompt.strip(),
            "metadata": metadata,
        }
        submit_url = f"{base_url}/v1/videos"
        headers = {"Authorization": f"Bearer {api_key.strip()}", "Content-Type": "application/json"}
        last_raw: Any = None
        last_error = ""

        for attempt in range(1, retry_count + 1):
            try:
                response = requests.post(submit_url, headers=headers, json=payload, timeout=timeout)
                raw = self._json_or_text(response)
                last_raw = raw
                if response.status_code in {400, 401, 403, 405, 422}:
                    return ImageGenerationResult(False, raw_response=raw, error_message=f"Nano Banana 提交参数或鉴权错误: HTTP {response.status_code} {raw}")
                if not response.ok:
                    last_error = f"Nano Banana 提交失败: HTTP {response.status_code} {raw}"
                    if response.status_code in {429, 500, 502, 503, 504} and attempt < retry_count:
                        time.sleep(retry_interval * attempt)
                        continue
                    return ImageGenerationResult(False, raw_response=raw, error_message=last_error)
                if not isinstance(raw, dict):
                    return ImageGenerationResult(False, raw_response=raw, error_message="Nano Banana 提交返回非 JSON")

                task_id = extract_task_id(raw)
                status = (extract_status(raw) or "").lower()
                image_url = extract_image_url(raw)
                if task_id:
                    self._notify_task_id(on_task_id, task_id, raw)
                if image_url and status in {"", "completed", "succeeded", "success"}:
                    saved = download_file(image_url, output_path, timeout=timeout) if output_path else None
                    return ImageGenerationResult(True, image_path=saved, image_url=image_url, task_id=task_id, raw_response=raw)
                if not task_id:
                    return ImageGenerationResult(False, raw_response=raw, error_message="Nano Banana 未返回任务 ID")
                return self._poll_until_done(
                    base_url=base_url,
                    headers=headers,
                    task_id=task_id,
                    output_path=output_path,
                    timeout=timeout,
                    poll_interval=poll_interval,
                    max_poll_count=max_poll_count,
                    submit_raw=raw,
                    stop_event=stop_event,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = f"Nano Banana 网络异常: {exc}"
                if attempt < retry_count:
                    time.sleep(retry_interval * attempt)
                    continue
                return ImageGenerationResult(False, raw_response=last_raw, error_message=last_error)
            except Exception as exc:
                return ImageGenerationResult(False, raw_response=last_raw, error_message=f"Nano Banana 调用异常: {exc}")

        return ImageGenerationResult(False, raw_response=last_raw, error_message=last_error or "Nano Banana 提交失败")

    def poll_image_task(
        self,
        task_id: str,
        api_key: str,
        extra_params: dict[str, Any] | None = None,
    ) -> ImageGenerationResult:
        params = extra_params or {}
        task_id = str(task_id or "").strip()
        if not task_id:
            return ImageGenerationResult(False, error_message="图生图 task_id 为空，无法续跑轮询")
        if not api_key.strip():
            return ImageGenerationResult(False, task_id=task_id, error_message="IMAGE_API_KEY 为空")
        base_url = self._base_url(str(params.get("base_url") or ""))
        headers = {"Authorization": f"Bearer {api_key.strip()}", "Content-Type": "application/json"}
        return self._poll_until_done(
            base_url=base_url,
            headers=headers,
            task_id=task_id,
            output_path=str(params.get("output_path") or ""),
            timeout=int(params.get("timeout") or 120),
            poll_interval=max(1, int(params.get("poll_interval_seconds") or 5)),
            max_poll_count=max(1, int(params.get("max_poll_count") or 120)),
            submit_raw={"id": task_id, "resume": True},
            stop_event=params.get("stop_event"),
        )

    def _poll_until_done(
        self,
        *,
        base_url: str,
        headers: dict[str, str],
        task_id: str,
        output_path: str,
        timeout: int,
        poll_interval: int,
        max_poll_count: int,
        submit_raw: Any,
        stop_event: Any = None,
    ) -> ImageGenerationResult:
        poll_url = f"{base_url}/v1/videos/{task_id}"
        last_raw: Any = submit_raw
        for poll_count in range(1, max_poll_count + 1):
            if self._stop_requested(stop_event):
                return ImageGenerationResult(False, task_id=task_id, raw_response=last_raw, error_message="Nano Banana image polling stopped")
            if self._sleep_or_cancel(poll_interval, stop_event):
                return ImageGenerationResult(False, task_id=task_id, raw_response=last_raw, error_message="Nano Banana image polling stopped")
            try:
                response = requests.get(poll_url, headers=headers, timeout=timeout)
                raw = self._json_or_text(response)
                last_raw = raw
                if not response.ok:
                    if response.status_code in {429, 500, 502, 503, 504}:
                        continue
                    return ImageGenerationResult(False, task_id=task_id, raw_response=raw, error_message=f"Nano Banana 轮询失败: HTTP {response.status_code} {raw}")
                if not isinstance(raw, dict):
                    return ImageGenerationResult(False, task_id=task_id, raw_response=raw, error_message="Nano Banana 轮询返回非 JSON")

                status = (extract_status(raw) or "").lower()
                image_url = extract_image_url(raw)
                if image_url and status in {"", "completed", "succeeded", "success"}:
                    saved = download_file(image_url, output_path, timeout=timeout) if output_path else None
                    return ImageGenerationResult(True, image_path=saved, image_url=image_url, task_id=task_id, raw_response=raw)
                if status == "completed":
                    if not image_url:
                        return ImageGenerationResult(False, task_id=task_id, raw_response=raw, error_message="Nano Banana 已完成但未返回图片 URL")
                    saved = download_file(image_url, output_path, timeout=timeout) if output_path else None
                    return ImageGenerationResult(True, image_path=saved, image_url=image_url, task_id=task_id, raw_response=raw)
                if status == "failed":
                    return ImageGenerationResult(False, task_id=task_id, raw_response=raw, error_message=extract_error_message(raw) or "Nano Banana 图片任务失败")
            except (requests.Timeout, requests.ConnectionError):
                continue
            except Exception as exc:
                return ImageGenerationResult(False, task_id=task_id, raw_response=last_raw, error_message=f"Nano Banana 轮询异常: {exc}")
        return ImageGenerationResult(False, task_id=task_id, status="timeout", raw_response=last_raw, error_message=f"Nano Banana 轮询超过最大次数: {max_poll_count}")

    @staticmethod
    def _stop_requested(stop_event: Any) -> bool:
        return bool(stop_event is not None and getattr(stop_event, "is_set", lambda: False)())

    @classmethod
    def _sleep_or_cancel(cls, seconds: int, stop_event: Any) -> bool:
        deadline = time.monotonic() + max(0, int(seconds or 0))
        while time.monotonic() < deadline:
            if cls._stop_requested(stop_event):
                return True
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        return cls._stop_requested(stop_event)

    @staticmethod
    def _base_url(base_url: str) -> str:
        text = (base_url or "").strip().rstrip("/")
        if not text or "YOUR_API_HOST" in text:
            return XibapiNanoBananaProvider.default_base_url
        return text

    @staticmethod
    def _image_size(model: str, configured: str) -> str:
        configured = configured.strip()
        if configured in {"1K", "2K", "4K"}:
            return configured
        for value in ("1K", "2K", "4K"):
            if model.endswith(f"-{value}"):
                return value
        return ""

    @staticmethod
    def _json_or_text(response: requests.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return response.text

    @staticmethod
    def _notify_task_id(callback: Any, task_id: str, raw: Any) -> None:
        if not callable(callback):
            return
        try:
            callback(task_id, raw)
        except TypeError:
            callback(task_id)
