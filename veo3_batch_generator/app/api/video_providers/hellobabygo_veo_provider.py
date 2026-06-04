from __future__ import annotations

import base64
import io
import json
import mimetypes
import threading
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import requests
from requests.adapters import HTTPAdapter

from app.api.base_provider import BaseVideoProvider
from app.api.response_parser import extract_error_message, extract_status, extract_task_id, extract_video_url
from app.api.video_api import VideoPollResult, VideoSubmitResult, is_retryable_video_failure
from app.file_utils import validate_downloaded_file


class HelloBabyGoVeoProvider(BaseVideoProvider):
    """HelloBabyGo VEO provider from the Spot Frog API document.

    API summary:
    - POST /v1/videos
    - GET  /v1/videos/{task_id}
    - GET  /v1/videos/{task_id}/content
    """

    provider_key = "hellobabygo_veo"
    provider_name = "HelloBabyGo VEO"
    default_base_url = "https://api.hellobabygo.com"
    model_options = [
        {
            "logical_key": "veo_3_1_fast_portrait_fl_hd",
            "display_name": "Veo 3.1 Fast Portrait FL HD",
            "provider_value": "veo_3_1-fast-portrait-fl-hd",
        },
        {
            "logical_key": "veo_3_1_fast_landscape_fl_hd",
            "display_name": "Veo 3.1 Fast Landscape FL HD",
            "provider_value": "veo_3_1-fast-landscape-fl-hd",
        },
        {
            "logical_key": "veo_3_1_fast_portrait_hd",
            "display_name": "Veo 3.1 Fast Portrait HD",
            "provider_value": "veo_3_1-fast-portrait-hd",
        },
        {
            "logical_key": "veo_3_1_fast_landscape_hd",
            "display_name": "Veo 3.1 Fast Landscape HD",
            "provider_value": "veo_3_1-fast-landscape-hd",
        },
        {
            "logical_key": "veo_3_1_fast_portrait_fl",
            "display_name": "Veo 3.1 Fast Portrait FL",
            "provider_value": "veo_3_1-fast-portrait-fl",
        },
        {
            "logical_key": "veo_3_1_fast_landscape_fl",
            "display_name": "Veo 3.1 Fast Landscape FL",
            "provider_value": "veo_3_1-fast-landscape-fl",
        },
        {
            "logical_key": "veo_3_1_fast_portrait",
            "display_name": "Veo 3.1 Fast Portrait",
            "provider_value": "veo_3_1-fast-portrait",
        },
        {
            "logical_key": "veo_3_1_fast_landscape",
            "display_name": "Veo 3.1 Fast Landscape",
            "provider_value": "veo_3_1-fast-landscape",
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

    def submit_video_task(
        self,
        image_source: str,
        prompt: str,
        model_logical_key: str,
        api_key: str,
        extra_params: dict[str, Any] | None = None,
    ) -> VideoSubmitResult:
        params = extra_params or {}
        if not str(api_key or "").strip():
            return VideoSubmitResult(False, error_message="VIDEO_API_KEY is empty")
        if not str(prompt or "").strip():
            return VideoSubmitResult(False, error_message="Video prompt is empty")

        base_url = self._base_url(str(params.get("base_url") or ""))
        model = self.get_model_option(model_logical_key)["provider_value"]
        timeout = int(params.get("timeout") or 120)
        retry_count = max(1, int(params.get("retry_count") or 3))
        retry_interval = max(1, int(params.get("retry_interval_seconds") or 5))
        resolution = str(params.get("resolution") or params.get("size") or "1080x1920")
        duration = self._int_value(params.get("duration"), default=8)
        submit_url = f"{base_url}/v1/videos"
        headers = {"Authorization": f"Bearer {str(api_key).strip()}"}
        image_refs = self._collect_video_refs(image_source, params)
        last_raw: Any = None
        last_error = ""

        for attempt in range(1, retry_count + 1):
            try:
                if image_refs:
                    raw = self._submit_image_video(
                        submit_url=submit_url,
                        headers=headers,
                        refs=image_refs,
                        model=model,
                        prompt=str(prompt).strip(),
                        resolution=resolution,
                        duration=duration,
                        timeout=timeout,
                    )
                else:
                    raw = self._submit_text_video(
                        submit_url=submit_url,
                        headers={**headers, "Content-Type": "application/json"},
                        model=model,
                        prompt=str(prompt).strip(),
                        resolution=resolution,
                        duration=duration,
                        timeout=timeout,
                    )
                last_raw = raw
                if not isinstance(raw, dict):
                    return VideoSubmitResult(False, raw_response=raw, error_message="HelloBabyGo video submit returned non-JSON")
                task_id = extract_task_id(raw)
                video_url = extract_video_url(raw)
                if task_id or video_url:
                    return VideoSubmitResult(True, task_id=task_id, video_url=video_url, raw_response=raw)
                return VideoSubmitResult(False, raw_response=raw, error_message="HelloBabyGo video submit did not return task_id")
            except _HttpSubmitError as exc:
                last_raw = exc.raw_response
                last_error = f"HelloBabyGo video submit failed: HTTP {exc.status_code} {exc.raw_response}"
                if exc.status_code in {429, 500, 502, 503, 504} and attempt < retry_count:
                    time.sleep(retry_interval * attempt)
                    continue
                return VideoSubmitResult(False, raw_response=exc.raw_response, error_message=last_error)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = f"HelloBabyGo video network error: {exc}"
                if attempt < retry_count:
                    time.sleep(retry_interval * attempt)
                    continue
                return VideoSubmitResult(False, raw_response=last_raw, error_message=last_error)
            except Exception as exc:
                return VideoSubmitResult(False, raw_response=last_raw, error_message=f"HelloBabyGo video error: {exc}")

        return VideoSubmitResult(False, raw_response=last_raw, error_message=last_error or "HelloBabyGo video submit failed")

    def poll_video_task(
        self,
        task_id: str,
        api_key: str,
        extra_params: dict[str, Any] | None = None,
    ) -> VideoPollResult:
        params = extra_params or {}
        task_id = str(task_id or "").strip()
        if not task_id:
            return VideoPollResult(False, failed=True, error_message="video task_id is empty")
        if not str(api_key or "").strip():
            return VideoPollResult(False, failed=True, error_message="VIDEO_API_KEY is empty")

        base_url = self._base_url(str(params.get("base_url") or ""))
        timeout = int(params.get("timeout") or 120)
        url = f"{base_url}/v1/videos/{task_id}"
        headers = {"Authorization": f"Bearer {str(api_key).strip()}"}
        try:
            response = self._session().get(url, headers=headers, timeout=timeout)
            raw = self._json_or_text(response)
            if not response.ok:
                if response.status_code in {429, 500, 502, 503, 504}:
                    return VideoPollResult(True, finished=False, failed=False, status=f"HTTP_{response.status_code}", raw_response=raw)
                return VideoPollResult(
                    False,
                    failed=True,
                    retryable_failure=response.status_code not in {401, 403} or is_retryable_video_failure(raw),
                    status=f"HTTP_{response.status_code}",
                    raw_response=raw,
                    error_message=f"HelloBabyGo video poll failed: HTTP {response.status_code} {raw}",
                )
            if not isinstance(raw, dict):
                return VideoPollResult(False, failed=True, raw_response=raw, error_message="HelloBabyGo video poll returned non-JSON")

            status = (extract_status(raw) or "").lower()
            if status in {"completed", "succeeded", "success"}:
                video_url = extract_video_url(raw) or f"{base_url}/v1/videos/{task_id}/content"
                return VideoPollResult(True, finished=True, status=status or "completed", video_url=video_url, raw_response=raw)
            if status in {"failed", "error", "cancelled", "canceled"}:
                message = extract_error_message(raw) or raw
                return VideoPollResult(
                    False,
                    failed=True,
                    retryable_failure=is_retryable_video_failure(message),
                    status=status or "failed",
                    raw_response=raw,
                    error_message=f"HelloBabyGo video task failed: {message}",
                )
            return VideoPollResult(True, finished=False, failed=False, status=status or "unknown", raw_response=raw)
        except (requests.Timeout, requests.ConnectionError) as exc:
            return VideoPollResult(True, finished=False, failed=False, status="network_retry", error_message=f"Network error, will keep polling: {exc}")
        except Exception as exc:
            return VideoPollResult(False, failed=True, error_message=f"HelloBabyGo video poll error: {exc}")

    def download_video_content(
        self,
        task_id: str,
        api_key: str,
        output_path: str | Path,
        extra_params: dict[str, Any] | None = None,
    ) -> str:
        params = extra_params or {}
        base_url = self._base_url(str(params.get("base_url") or ""))
        timeout = int(params.get("timeout") or 180)
        url = f"{base_url}/v1/videos/{str(task_id).strip()}/content"
        return self._download_authenticated_content(url, api_key, output_path, timeout)

    def _submit_image_video(
        self,
        *,
        submit_url: str,
        headers: dict[str, str],
        refs: list[str],
        model: str,
        prompt: str,
        resolution: str,
        duration: int,
        timeout: int,
    ) -> Any:
        # The API document describes first/last-frame video via multipart
        # input_reference[]. Current workflow video nodes often provide one
        # image; send it as both first and last frame so the provider still
        # receives a valid image-video request.
        selected_refs = refs[:2] if len(refs) >= 2 else [refs[0], refs[0]]
        data = {
            "model": model,
            "prompt": prompt,
            "size": resolution,
            # The live API unmarshals multipart scalar fields as strings, then
            # validates Alias.duration as an int. Keep duration typed by putting
            # it inside the provider alias JSON object.
            "alias": json.dumps({"duration": duration}, ensure_ascii=False),
        }
        with ExitStack() as stack:
            files = [
                *[("input_reference[]", self._file_tuple(ref, stack, idx)) for idx, ref in enumerate(selected_refs)],
            ]
            response = self._session().post(submit_url, headers=headers, data=data, files=files, timeout=timeout)
            raw = self._json_or_text(response)
            if response.status_code in {400, 401, 403, 405, 422} or not response.ok:
                raise _HttpSubmitError(response.status_code, raw)
            return raw

    def _submit_text_video(
        self,
        *,
        submit_url: str,
        headers: dict[str, str],
        model: str,
        prompt: str,
        resolution: str,
        duration: int,
        timeout: int,
    ) -> Any:
        response = self._session().post(
            submit_url,
            headers=headers,
            json={"model": model, "prompt": prompt, "size": resolution, "duration": duration},
            timeout=timeout,
        )
        raw = self._json_or_text(response)
        if response.status_code in {400, 401, 403, 405, 422} or not response.ok:
            raise _HttpSubmitError(response.status_code, raw)
        return raw

    def _file_tuple(self, value: str, stack: ExitStack, index: int) -> tuple[str, Any, str]:
        text = str(value or "").strip()
        lower = text.lower()
        if lower.startswith(("http://", "https://")):
            response = self._session().get(text, timeout=120)
            response.raise_for_status()
            parsed = urlparse(text)
            name = Path(unquote(parsed.path)).name or f"reference_{index}.png"
            mime = response.headers.get("Content-Type") or mimetypes.guess_type(name)[0] or "image/png"
            return (name, io.BytesIO(response.content), mime.split(";")[0])
        if lower.startswith("data:image"):
            header, b64_data = text.split(",", 1)
            mime = header.split(";", 1)[0].replace("data:", "") or "image/png"
            ext = mimetypes.guess_extension(mime) or ".png"
            return (f"reference_{index}{ext}", io.BytesIO(base64.b64decode(b64_data)), mime)
        path = Path(text)
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"video input image not found: {text}")
        handle = stack.enter_context(path.open("rb"))
        mime = mimetypes.guess_type(str(path))[0] or "image/png"
        return (path.name, handle, mime)

    @staticmethod
    def _collect_video_refs(image_source: str, params: dict[str, Any]) -> list[str]:
        refs: list[str] = []

        def add(value: Any) -> None:
            text = str(value or "").strip()
            if text and text not in refs:
                refs.append(text)

        # The workflow passes public URL "companions" for local files in
        # input_image_urls. For multipart-capable providers, use the actual
        # image source/input file first and only fall back to companion URLs
        # when there is no concrete source. Otherwise a dead intranet HTTP
        # mapping can make submission fail even though the local image exists.
        for value in [image_source] + list(params.get("input_images") or []):
            add(value)
        if refs:
            return refs
        for value in list(params.get("input_image_urls") or []):
            add(value)
        return refs

    @classmethod
    def _base_url(cls, base_url: str) -> str:
        text = (base_url or "").strip().rstrip("/")
        if not text or "YOUR_API_HOST" in text or text == "https://xibapi.com":
            return cls.default_base_url
        return text

    @staticmethod
    def _int_value(value: Any, default: int) -> int:
        text = str(value).strip() if value is not None else ""
        if not text:
            return default
        try:
            return int(float(text))
        except (TypeError, ValueError):
            return default

    def _download_authenticated_content(self, url: str, api_key: str, output_path: str | Path, timeout: int) -> str:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".part")
        headers = {"Authorization": f"Bearer {str(api_key).strip()}"}
        if tmp_path.exists():
            tmp_path.unlink()
        with self._session().get(url, headers=headers, timeout=timeout, stream=True) as response:
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
                raise IOError(f"incomplete video download: expected {expected_size} bytes, got {written} bytes")
        if not tmp_path.exists() or tmp_path.stat().st_size <= 0:
            raise IOError("downloaded video content is empty")
        tmp_path.replace(path)
        ok, message = validate_downloaded_file(path, expected_kind="video")
        if not ok:
            try:
                path.unlink()
            except OSError:
                pass
            raise IOError(f"downloaded video validation failed: {message}")
        return str(path)

    @staticmethod
    def _json_or_text(response: requests.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return response.text


class _HttpSubmitError(Exception):
    def __init__(self, status_code: int, raw_response: Any) -> None:
        super().__init__(f"HTTP {status_code}: {raw_response}")
        self.status_code = status_code
        self.raw_response = raw_response
