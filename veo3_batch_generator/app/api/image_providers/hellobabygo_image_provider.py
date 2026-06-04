from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import requests

from app.api.base_provider import BaseImageProvider
from app.api.image_api import ImageGenerationResult
from app.api.response_parser import extract_error_message, extract_image_url, extract_status, extract_task_id
from app.file_utils import download_file, validate_downloaded_file


class HelloBabyGoImageProvider(BaseImageProvider):
    """HelloBabyGo image provider from the Spot Frog API document.

    API summary:
    - POST /v1/images/generations?async=true
    - GET  /v1/images/{task_id}
    - GET  /v1/images/{task_id}/content
    """

    provider_key = "hellobabygo_image"
    provider_name = "HelloBabyGo GPT Image"
    default_base_url = "https://api.hellobabygo.com"
    model_options = [
        {
            "logical_key": "gpt_image_2",
            "display_name": "GPT Image 2",
            "provider_value": "gpt-image-2",
        },
        {
            "logical_key": "auto_image",
            "display_name": "Auto Image",
            "provider_value": "auto-image",
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
        if not str(api_key or "").strip():
            return ImageGenerationResult(False, error_message="IMAGE_API_KEY is empty")
        if not str(prompt or "").strip():
            return ImageGenerationResult(False, error_message="Image prompt is empty")

        base_url = self._base_url(str(params.get("base_url") or ""))
        model = self.get_model_option(model_logical_key)["provider_value"]
        output_path = str(params.get("output_path") or "")
        timeout = int(params.get("timeout") or 120)
        retry_count = max(1, int(params.get("retry_count") or 3))
        retry_interval = max(1, int(params.get("retry_interval_seconds") or 5))
        poll_interval = max(1, int(params.get("poll_interval_seconds") or 5))
        max_poll_count = max(1, int(params.get("max_poll_count") or 120))
        on_task_id = params.get("on_task_id")
        stop_event = params.get("stop_event")

        payload: dict[str, Any] = {
            "model": model,
            "prompt": str(prompt).strip(),
            "response_format": "url",
            "size": str(params.get("size") or params.get("image_size") or "1024x1024"),
        }
        urls = self._collect_image_refs(product_image_path, params)
        if bool(params.get("requires_reference_image")) and not urls:
            return ImageGenerationResult(
                False,
                status="failed",
                error_message=(
                    "HelloBabyGo image generation requires a public reference image URL. "
                    "No product white-background image URL was resolved; check netdisk HTTP mapping or product_image_url."
                ),
            )
        if urls:
            payload["urls"] = urls

        headers = {
            "Authorization": f"Bearer {str(api_key).strip()}",
            "Content-Type": "application/json",
        }
        submit_url = f"{base_url}/v1/images/generations"
        last_raw: Any = None
        last_error = ""

        for attempt in range(1, retry_count + 1):
            try:
                response = requests.post(
                    submit_url,
                    params={"async": "true"},
                    headers=headers,
                    json=payload,
                    timeout=timeout,
                )
                raw = self._json_or_text(response)
                last_raw = raw
                if response.status_code in {400, 401, 403, 405, 422}:
                    return ImageGenerationResult(
                        False,
                        status="failed",
                        raw_response=raw,
                        error_message=f"HelloBabyGo image submit rejected: HTTP {response.status_code} {raw}",
                    )
                if not response.ok:
                    last_error = f"HelloBabyGo image submit failed: HTTP {response.status_code} {raw}"
                    if response.status_code in {429, 500, 502, 503, 504} and attempt < retry_count:
                        time.sleep(retry_interval * attempt)
                        continue
                    return ImageGenerationResult(False, status="failed", raw_response=raw, error_message=last_error)
                if not isinstance(raw, dict):
                    return ImageGenerationResult(False, status="failed", raw_response=raw, error_message="HelloBabyGo image submit returned non-JSON")

                task_id = extract_task_id(raw)
                status = (extract_status(raw) or "").lower()
                image_url = extract_image_url(raw)
                if task_id:
                    self._notify_task_id(on_task_id, task_id, raw)
                if image_url and status in {"", "completed", "succeeded", "success"}:
                    return self._finish_completed_image(base_url, headers, task_id or "", image_url, output_path, timeout, raw)
                if status == "completed" and (image_url or task_id):
                    return self._finish_completed_image(base_url, headers, task_id or "", image_url, output_path, timeout, raw)
                if not task_id:
                    return ImageGenerationResult(False, status="failed", raw_response=raw, error_message="HelloBabyGo image submit did not return task_id")

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
                last_error = f"HelloBabyGo image network error: {exc}"
                if attempt < retry_count:
                    time.sleep(retry_interval * attempt)
                    continue
                return ImageGenerationResult(False, status="failed", raw_response=last_raw, error_message=last_error)
            except Exception as exc:
                return ImageGenerationResult(False, status="failed", raw_response=last_raw, error_message=f"HelloBabyGo image error: {exc}")

        return ImageGenerationResult(False, status="failed", raw_response=last_raw, error_message=last_error or "HelloBabyGo image submit failed")

    def poll_image_task(
        self,
        task_id: str,
        api_key: str,
        extra_params: dict[str, Any] | None = None,
    ) -> ImageGenerationResult:
        params = extra_params or {}
        task_id = str(task_id or "").strip()
        if not task_id:
            return ImageGenerationResult(False, status="failed", error_message="image task_id is empty")
        if not str(api_key or "").strip():
            return ImageGenerationResult(False, task_id=task_id, status="failed", error_message="IMAGE_API_KEY is empty")
        base_url = self._base_url(str(params.get("base_url") or ""))
        headers = {"Authorization": f"Bearer {str(api_key).strip()}"}
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

    def download_image_content(
        self,
        task_id: str,
        api_key: str,
        output_path: str | Path,
        extra_params: dict[str, Any] | None = None,
    ) -> str:
        params = extra_params or {}
        base_url = self._base_url(str(params.get("base_url") or ""))
        timeout = int(params.get("timeout") or 120)
        url = f"{base_url}/v1/images/{str(task_id).strip()}/content"
        return self._download_authenticated_content(url, api_key, output_path, timeout)

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
        poll_url = f"{base_url}/v1/images/{task_id}"
        last_raw: Any = submit_raw
        for poll_count in range(1, max_poll_count + 1):
            if self._stop_requested(stop_event):
                return ImageGenerationResult(False, task_id=task_id, status="stopped", raw_response=last_raw, error_message="HelloBabyGo image polling stopped")
            if poll_count > 1 and self._sleep_or_cancel(poll_interval, stop_event):
                return ImageGenerationResult(False, task_id=task_id, status="stopped", raw_response=last_raw, error_message="HelloBabyGo image polling stopped")
            try:
                response = requests.get(poll_url, headers=headers, timeout=timeout)
                raw = self._json_or_text(response)
                last_raw = raw
                if not response.ok:
                    if response.status_code in {429, 500, 502, 503, 504}:
                        continue
                    return ImageGenerationResult(
                        False,
                        task_id=task_id,
                        status="failed",
                        raw_response=raw,
                        error_message=f"HelloBabyGo image poll failed: HTTP {response.status_code} {raw}",
                    )
                if not isinstance(raw, dict):
                    return ImageGenerationResult(False, task_id=task_id, status="failed", raw_response=raw, error_message="HelloBabyGo image poll returned non-JSON")

                status = (extract_status(raw) or "").lower()
                image_url = extract_image_url(raw)
                if image_url and status in {"", "completed", "succeeded", "success"}:
                    return self._finish_completed_image(base_url, headers, task_id, image_url, output_path, timeout, raw)
                if status == "completed":
                    return self._finish_completed_image(base_url, headers, task_id, image_url, output_path, timeout, raw)
                if status in {"failed", "failure", "error", "cancelled", "canceled"}:
                    return ImageGenerationResult(
                        False,
                        task_id=task_id,
                        status="failed",
                        raw_response=raw,
                        error_message=extract_error_message(raw) or "HelloBabyGo image task failed",
                    )
            except (requests.Timeout, requests.ConnectionError):
                continue
            except Exception as exc:
                return ImageGenerationResult(False, task_id=task_id, status="failed", raw_response=last_raw, error_message=f"HelloBabyGo image poll error: {exc}")
        return ImageGenerationResult(False, task_id=task_id, status="timeout", raw_response=last_raw, error_message=f"HelloBabyGo image poll exceeded max count: {max_poll_count}")

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

    def _finish_completed_image(
        self,
        base_url: str,
        headers: dict[str, str],
        task_id: str,
        image_url: str | None,
        output_path: str,
        timeout: int,
        raw: Any,
    ) -> ImageGenerationResult:
        saved = None
        last_error = ""
        if image_url and output_path:
            try:
                saved = download_file(image_url, output_path, timeout=timeout)
            except Exception as exc:
                last_error = f"image URL download failed, trying authenticated content: {exc}"
        if not saved and task_id and output_path:
            try:
                content_url = f"{base_url}/v1/images/{task_id}/content"
                saved = self._download_authenticated_content(content_url, headers.get("Authorization", "").replace("Bearer ", ""), output_path, timeout)
            except Exception as exc:
                last_error = f"{last_error}; content download failed: {exc}" if last_error else f"content download failed: {exc}"
        if not image_url and task_id:
            image_url = f"{base_url}/v1/images/{task_id}/content"
        return ImageGenerationResult(
            True,
            image_path=saved,
            image_url=image_url,
            task_id=task_id or None,
            raw_response=raw,
            error_message=last_error or None,
        )

    @classmethod
    def _base_url(cls, base_url: str) -> str:
        text = (base_url or "").strip().rstrip("/")
        if not text or "YOUR_API_HOST" in text or text == "https://xibapi.com":
            return cls.default_base_url
        return text

    @staticmethod
    def _collect_image_refs(product_image_path: str, params: dict[str, Any]) -> list[str]:
        refs: list[str] = []
        for value in list(params.get("input_image_urls") or []) + list(params.get("input_images") or []) + [product_image_path]:
            text = str(value or "").strip()
            if not text or text in refs:
                continue
            lower = text.lower()
            # HelloBabyGo's reference-image endpoint only accepts public image
            # URLs in the JSON "urls" array. Local files/data URLs are ignored
            # here so image-to-image nodes fail visibly instead of silently
            # degrading into text-only image generation.
            if lower.startswith(("http://", "https://")):
                refs.append(text)
                continue
        return refs

    @staticmethod
    def _download_authenticated_content(url: str, api_key: str, output_path: str | Path, timeout: int) -> str:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".part")
        headers = {"Authorization": f"Bearer {str(api_key).strip()}"}
        if tmp_path.exists():
            tmp_path.unlink()
        with requests.get(url, headers=headers, timeout=timeout, stream=True) as response:
            response.raise_for_status()
            expected_size = int(response.headers.get("Content-Length") or 0)
            content_encoding = str(response.headers.get("Content-Encoding") or "").strip()
            written = 0
            with tmp_path.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        handle.write(chunk)
                        written += len(chunk)
            if expected_size and not content_encoding and written != expected_size:
                raise IOError(f"incomplete image download: expected {expected_size} bytes, got {written} bytes")
        if not tmp_path.exists() or tmp_path.stat().st_size <= 0:
            raise IOError("downloaded image content is empty")
        tmp_path.replace(path)
        ok, message = validate_downloaded_file(path, expected_kind="image")
        if not ok:
            try:
                path.unlink()
            except OSError:
                pass
            raise IOError(f"downloaded image validation failed: {message}")
        return str(path)

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
