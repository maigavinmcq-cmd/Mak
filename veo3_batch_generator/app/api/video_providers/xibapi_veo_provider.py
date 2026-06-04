from __future__ import annotations

from typing import Any

from app.api.base_provider import BaseVideoProvider
from app.api.video_api import VideoPollResult, VideoSubmitResult, poll_video_task, submit_video_task


class XibapiVeoProvider(BaseVideoProvider):
    provider_key = "xibapi_veo"
    provider_name = "xibapi Veo"
    model_options = [
        {
            "logical_key": "veo_3",
            "display_name": "Veo 3",
            "provider_value": "veo_3",
        },
        {
            "logical_key": "veo_3_fast",
            "display_name": "Veo 3 Fast",
            "provider_value": "veo_3-fast",
        },
        {
            "logical_key": "veo_3_1",
            "display_name": "Veo 3.1",
            "provider_value": "veo_3_1",
        },
        {
            "logical_key": "veo_3_1_fast",
            "display_name": "Veo 3.1 Fast",
            "provider_value": "veo_3_1-fast-fl",
        },
        {
            "logical_key": "veo_3_1_hd_fl",
            "display_name": "Veo 3.1 Fast-HD",
            "provider_value": "veo_3_1-hd-fl",
        },
        {
            "logical_key": "veo_3_1_hd",
            "display_name": "Veo 3.1 HD",
            "provider_value": "veo_3_1-hd",
        },
    ]

    def submit_video_task(
        self,
        image_source: str,
        prompt: str,
        model_logical_key: str,
        api_key: str,
        extra_params: dict[str, Any] | None = None,
    ) -> VideoSubmitResult:
        params = extra_params or {}
        model = self.get_model_option(model_logical_key)["provider_value"]
        return submit_video_task(
            image_source=image_source,
            video_prompt=prompt,
            api_key=api_key,
            base_url=str(params.get("base_url") or "https://xibapi.com"),
            model=model,
            orientation=str(params.get("orientation") or "portrait"),
            resolution=str(params.get("resolution") or "1080x1920"),
            retry_count=int(params.get("retry_count") or 3),
            retry_interval_seconds=int(params.get("retry_interval_seconds") or 5),
            timeout=int(params.get("timeout") or 120),
        )

    def poll_video_task(
        self,
        task_id: str,
        api_key: str,
        extra_params: dict[str, Any] | None = None,
    ) -> VideoPollResult:
        params = extra_params or {}
        return poll_video_task(
            task_id=task_id,
            api_key=api_key,
            base_url=str(params.get("base_url") or "https://xibapi.com"),
        )
