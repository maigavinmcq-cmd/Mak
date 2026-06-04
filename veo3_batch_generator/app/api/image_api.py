from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import requests

from app.api.response_parser import extract_image_b64, extract_image_url, extract_task_id
from app.file_utils import download_file, file_to_data_url, save_base64_image


@dataclass
class ImageGenerationResult:
    success: bool
    image_path: Optional[str] = None
    image_url: Optional[str] = None
    task_id: Optional[str] = None
    status: Optional[str] = None
    raw_response: Any = None
    error_message: Optional[str] = None


def _is_retryable_status(status_code: int) -> bool:
    return status_code in {429, 500, 502, 503, 504}


def _parse_json_or_text(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _short_error(raw: Any, limit: int = 800) -> str:
    if isinstance(raw, dict):
        return str(raw)
    text = str(raw or "")
    if "<title>" in text and "</title>" in text:
        title = text.split("<title>", 1)[1].split("</title>", 1)[0].strip()
        return title
    return text[:limit]


def generate_image_from_product_image(
    product_image_path: str,
    image_prompt: str,
    api_key: str,
    provider: str = "xibapi_gpt_image2",
    model_logical_key: str = "gpt_image_2",
    output_path: str = "",
    base_url: str = "",
    model: str | int = "gpt-image-2",
    size: str = "1024x1024",
    retry_count: int = 3,
    retry_interval_seconds: int = 5,
    timeout: int = 180,
) -> ImageGenerationResult:
    if provider and model_logical_key and str(model_logical_key).startswith(("http://", "https://")):
        # Backward compatibility for the older positional signature:
        # (..., api_key, output_path, base_url, model, size, retry_count)
        legacy_output_path = provider
        legacy_base_url = model_logical_key
        legacy_model = output_path or "gpt-image-2"
        legacy_size = base_url or "1024x1024"
        legacy_retry_count = model
        provider = "xibapi_gpt_image2"
        model_logical_key = "gpt_image_2"
        output_path = legacy_output_path
        base_url = legacy_base_url
        model = str(legacy_model)
        size = str(legacy_size)
        try:
            retry_count = int(legacy_retry_count)
        except (TypeError, ValueError):
            pass
    del provider, model_logical_key
    if not api_key:
        return ImageGenerationResult(False, error_message="IMAGE_API_KEY 为空")
    if not base_url or "YOUR_API_HOST" in base_url:
        return ImageGenerationResult(False, error_message="IMAGE_API_BASE_URL 未配置真实域名")
    if not image_prompt.strip():
        return ImageGenerationResult(False, error_message="图片提示词为空")
    path = Path(product_image_path)
    if not path.exists():
        return ImageGenerationResult(False, error_message=f"图片文件不存在：{path}")

    retry_count = max(1, retry_count)
    url = f"{base_url.rstrip('/')}/v1/images/generations"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "prompt": image_prompt,
        "size": size,
        "image": [file_to_data_url(path)],
    }
    last_error = ""
    last_raw: Any = None

    for attempt in range(1, retry_count + 1):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=timeout)
            raw = _parse_json_or_text(response)
            last_raw = raw
            if response.status_code in {400, 401, 403, 422}:
                return ImageGenerationResult(False, raw_response=raw, error_message=f"图生图 API 参数或鉴权错误：HTTP {response.status_code} {_short_error(raw)}")
            if not response.ok:
                last_error = f"图生图 API HTTP {response.status_code}：{_short_error(raw)}"
                if _is_retryable_status(response.status_code) and attempt < retry_count:
                    import time

                    time.sleep(retry_interval_seconds * attempt)
                    continue
                return ImageGenerationResult(False, raw_response=raw, error_message=last_error)

            if not isinstance(raw, dict):
                return ImageGenerationResult(False, raw_response=raw, error_message=f"图生图 API 返回非 JSON：{_short_error(raw)}")

            task_id = extract_task_id(raw)
            image_url = extract_image_url(raw)
            image_b64 = extract_image_b64(raw)
            if image_b64:
                saved = save_base64_image(image_b64, output_path)
                return ImageGenerationResult(True, image_path=saved, image_url=image_url, task_id=task_id, raw_response=raw)
            if image_url:
                try:
                    saved = download_file(image_url, output_path, timeout=timeout)
                except Exception as exc:
                    saved = None
                    last_error = f"图片 URL 下载失败，已保留 URL：{exc}"
                return ImageGenerationResult(True, image_path=saved, image_url=image_url, task_id=task_id, raw_response=raw, error_message=last_error or None)
            return ImageGenerationResult(False, raw_response=raw, error_message="图生图 API 返回字段缺失：未找到 b64 或 url")
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = f"图生图 API 网络异常：{exc}"
            if attempt < retry_count:
                import time

                time.sleep(retry_interval_seconds * attempt)
                continue
            return ImageGenerationResult(False, raw_response=last_raw, error_message=last_error)
        except Exception as exc:
            return ImageGenerationResult(False, raw_response=last_raw, error_message=f"图生图 API 未知异常：{exc}")

    return ImageGenerationResult(False, raw_response=last_raw, error_message=last_error or "图生图 API 调用失败")
