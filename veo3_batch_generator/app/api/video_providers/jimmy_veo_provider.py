from __future__ import annotations

from typing import Any

import requests

from app.api.base_provider import BaseVideoProvider
from app.api.response_parser import extract_video_url
from app.api.video_api import VideoPollResult, VideoSubmitResult


class JimmyVeoProvider(BaseVideoProvider):
    provider_key = "jimmy_veo"
    provider_name = "JimmyAI Veo"
    base_url = "https://www.jimmyai.cn"
    success_code = 20000
    # JimmyAI Veo only accepts a publicly reachable HTTP(S) image URL for the
    # first frame — it cannot consume a local file path or data URL.
    requires_remote_image_url = True
    model_options = [
        {
            "logical_key": "veo_3",
            "display_name": "Veo 3",
            "provider_value": "veo_3",
        },
        {
            "logical_key": "veo_3_fast",
            "display_name": "Veo 3 Fast",
            "provider_value": "veo_3_fast",
        },
        {
            "logical_key": "veo_3_1",
            "display_name": "Veo 3.1",
            "provider_value": "veo_3_1",
        },
        {
            "logical_key": "veo_3_1_fast",
            "display_name": "Veo 3.1 Fast",
            "provider_value": "veo_3_1_fast",
        },
    ]

    _SUCCESS_STATUSES = {"completed", "complete", "success", "succeeded", "finished", "done", "\u6210\u529f"}
    _FAILED_STATUSES = {"failed", "failure", "error", "cancelled", "canceled", "\u5931\u8d25"}
    _RATE_LIMIT_PATTERNS = (
        "request too frequent",
        "too frequent",
        "rate limit",
        "too many",
        "\u8bf7\u6c42\u8fc7\u4e8e\u9891\u7e41",
        "\u9891\u7e41",
    )
    _RETRYABLE_POLL_PATTERNS = _RATE_LIMIT_PATTERNS + (
        "try later",
        "temporary",
        "timeout",
        "timed out",
        "bad gateway",
        "http 408",
        "http 429",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
        "http 520",
        "http 521",
        "http 522",
        "http 523",
        "http 524",
        "\u8bf7\u7a0d\u540e",
        "\u7a0d\u540e\u518d\u8bd5",
    )

    def _headers(self, api_key: str, json_body: bool = False) -> dict[str, str]:
        if not api_key.strip():
            raise ValueError("VIDEO_API_KEY 为空")
        headers = {"Authorization": f"Bearer {api_key.strip()}"}
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    @staticmethod
    def _json_or_text(response: requests.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return response.text

    @staticmethod
    def _http_url(value: str) -> bool:
        return value.strip().lower().startswith(("http://", "https://"))

    @classmethod
    def _text_contains(cls, value: Any, patterns: tuple[str, ...]) -> bool:
        text = str(value or "").lower()
        return any(pattern.lower() in text for pattern in patterns)

    @classmethod
    def _retryable_poll_message(cls, value: Any) -> bool:
        return cls._text_contains(value, cls._RETRYABLE_POLL_PATTERNS)

    @classmethod
    def _rate_limited_message(cls, value: Any) -> bool:
        return cls._text_contains(value, cls._RATE_LIMIT_PATTERNS)

    def submit_video_task(
        self,
        image_source: str,
        prompt: str,
        model_logical_key: str,
        api_key: str,
        extra_params: dict[str, Any] | None = None,
    ) -> VideoSubmitResult:
        params = extra_params or {}
        first_frame_url = (image_source or "").strip()
        if not self._http_url(first_frame_url):
            return VideoSubmitResult(
                False,
                error_message="JimmyAI Veo 需要远程图片 URL；当前任务没有可用的 generated_image_url。",
            )
        if not prompt.strip():
            return VideoSubmitResult(False, error_message="视频提示词为空")

        model = self.get_model_option(model_logical_key)["provider_value"]
        orientation = str(params.get("orientation") or "landscape").lower()
        if orientation not in {"landscape", "portrait"}:
            orientation = "landscape"
        resolution = str(params.get("resolution") or "1080p").lower()
        if "4k" in resolution:
            resolution = "4k"
        elif "720" in resolution:
            resolution = "720p"
        elif "1080" in resolution:
            resolution = "1080p"
        payload = {
            "model": model,
            "prompt": prompt.strip(),
            "orientation": orientation,
            "first_frame_url": first_frame_url,
            "last_frame_url": str(params.get("last_frame_url") or ""),
            "resolution": resolution,
        }
        url = f"{str(params.get('base_url') or self.base_url).rstrip('/')}/api/open-api/v1/veo/frames"
        try:
            response = requests.post(
                url,
                headers=self._headers(api_key, json_body=True),
                json=payload,
                timeout=int(params.get("timeout") or 120),
            )
            raw = self._json_or_text(response)
            if not response.ok:
                return VideoSubmitResult(False, raw_response=raw, error_message=f"JimmyAI Veo 提交失败: HTTP {response.status_code} {raw}")
            if not isinstance(raw, dict):
                return VideoSubmitResult(False, raw_response=raw, error_message="JimmyAI Veo 返回非 JSON")
            if raw.get("code") != self.success_code or not isinstance(raw.get("data"), dict):
                return VideoSubmitResult(False, raw_response=raw, error_message=f"JimmyAI Veo 提交失败: {raw.get('msg') or raw}")
            task_id = str(raw["data"].get("task_id") or "").strip()
            if not task_id:
                return VideoSubmitResult(False, raw_response=raw, error_message="JimmyAI Veo 未返回 task_id")
            return VideoSubmitResult(True, task_id=task_id, raw_response=raw)
        except Exception as exc:
            return VideoSubmitResult(False, error_message=f"JimmyAI Veo 提交异常: {exc}")

    def poll_video_task(
        self,
        task_id: str,
        api_key: str,
        extra_params: dict[str, Any] | None = None,
    ) -> VideoPollResult:
        params = extra_params or {}
        if not task_id:
            return VideoPollResult(False, failed=True, error_message="video_task_id 为空，无法轮询")
        url = f"{str(params.get('base_url') or self.base_url).rstrip('/')}/api/open-api/v1/videos/{task_id}"
        try:
            response = requests.get(
                url,
                headers=self._headers(api_key),
                timeout=int(params.get("timeout") or 60),
            )
            raw = self._json_or_text(response)
            if not response.ok:
                retryable = response.status_code in {429, 500, 502, 503, 504}
                return VideoPollResult(
                    retryable,
                    failed=not retryable,
                    retryable_failure=retryable,
                    status=f"HTTP_{response.status_code}",
                    raw_response=raw,
                    error_message=f"JimmyAI Veo 轮询失败: HTTP {response.status_code} {raw}",
                )
            if not isinstance(raw, dict):
                return VideoPollResult(False, failed=True, raw_response=raw, error_message="JimmyAI Veo 轮询返回非 JSON")
            if raw.get("code") != self.success_code or not isinstance(raw.get("data"), dict):
                message = raw.get("msg") or raw.get("message") or raw
                if self._retryable_poll_message(message):
                    status = "rate_limited" if self._rate_limited_message(message) else "api_retry"
                    return VideoPollResult(
                        True,
                        finished=False,
                        failed=False,
                        retryable_failure=True,
                        status=status,
                        raw_response=raw,
                        error_message=f"JimmyAI Veo 轮询临时失败，继续使用原 task_id 轮询: {message}",
                    )
                return VideoPollResult(False, failed=True, raw_response=raw, error_message=f"JimmyAI Veo 轮询失败: {message}")

            data = raw["data"]
            status = str(data.get("status") or data.get("state") or data.get("task_status") or "").strip().lower()
            video_url = extract_video_url(raw) or str(
                data.get("video_url")
                or data.get("url")
                or data.get("result_url")
                or data.get("output_url")
                or ""
            ).strip()
            if status in self._SUCCESS_STATUSES or (video_url and str(data.get("progress") or "") == "100"):
                if not video_url:
                    return VideoPollResult(False, failed=True, status=status, raw_response=raw, error_message="视频已完成但没有视频链接")
                return VideoPollResult(True, finished=True, status=status, video_url=video_url, raw_response=raw)
            if status in self._FAILED_STATUSES:
                return VideoPollResult(False, failed=True, status=status, raw_response=raw, error_message=f"JimmyAI Veo 任务失败: {data}")
            return VideoPollResult(True, finished=False, failed=False, status=status or "unknown", raw_response=raw)
        except (requests.Timeout, requests.ConnectionError) as exc:
            return VideoPollResult(True, finished=False, failed=False, status="network_retry", error_message=f"网络异常，稍后继续轮询: {exc}")
        except Exception as exc:
            return VideoPollResult(False, failed=True, error_message=f"JimmyAI Veo 轮询异常: {exc}")
