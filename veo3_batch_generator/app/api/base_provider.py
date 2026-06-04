from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.api.image_api import ImageGenerationResult
from app.api.video_api import VideoPollResult, VideoSubmitResult


ModelOption = dict[str, str]


class BaseImageProvider(ABC):
    provider_key: str
    provider_name: str
    model_options: list[ModelOption]
    supports_async_image_tasks: bool = False

    def get_model_options(self) -> list[ModelOption]:
        return list(self.model_options)

    def get_model_option(self, logical_key: str) -> ModelOption:
        for option in self.model_options:
            if option["logical_key"] == logical_key:
                return option
        if not self.model_options:
            raise ValueError(f"图生图平台未配置模型: {self.provider_key}")
        return self.model_options[0]

    @abstractmethod
    def generate_image(
        self,
        product_image_path: str,
        prompt: str,
        model_logical_key: str,
        api_key: str,
        extra_params: dict[str, Any] | None = None,
    ) -> ImageGenerationResult:
        raise NotImplementedError

    def submit_image_task(
        self,
        product_image_path: str,
        prompt: str,
        model_logical_key: str,
        api_key: str,
        extra_params: dict[str, Any] | None = None,
    ) -> ImageGenerationResult:
        raise NotImplementedError

    def poll_image_task_once(
        self,
        task_id: str,
        api_key: str,
        extra_params: dict[str, Any] | None = None,
    ) -> ImageGenerationResult:
        raise NotImplementedError


class BaseVideoProvider(ABC):
    provider_key: str
    provider_name: str
    model_options: list[ModelOption]
    # When True, the provider only accepts a publicly reachable HTTP(S) image URL
    # as the first frame source and cannot consume a local file path / data URL.
    requires_remote_image_url: bool = False

    def get_model_options(self) -> list[ModelOption]:
        return list(self.model_options)

    def get_model_option(self, logical_key: str) -> ModelOption:
        for option in self.model_options:
            if option["logical_key"] == logical_key:
                return option
        if not self.model_options:
            raise ValueError(f"图生视频平台未配置模型: {self.provider_key}")
        return self.model_options[0]

    @abstractmethod
    def submit_video_task(
        self,
        image_source: str,
        prompt: str,
        model_logical_key: str,
        api_key: str,
        extra_params: dict[str, Any] | None = None,
    ) -> VideoSubmitResult:
        raise NotImplementedError

    @abstractmethod
    def poll_video_task(
        self,
        task_id: str,
        api_key: str,
        extra_params: dict[str, Any] | None = None,
    ) -> VideoPollResult:
        raise NotImplementedError
