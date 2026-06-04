from __future__ import annotations

from app.api.base_provider import BaseImageProvider, BaseVideoProvider
from app.api.image_providers.hellobabygo_image_provider import HelloBabyGoImageProvider
from app.api.image_providers.xibapi_gpt_image2_provider import XibapiGptImage2Provider
from app.api.image_providers.xibapi_nano_banana_provider import XibapiNanoBananaProvider
from app.api.video_providers.hellobabygo_veo_provider import HelloBabyGoVeoProvider
from app.api.video_providers.jimmy_veo_provider import JimmyVeoProvider
from app.api.video_providers.xibapi_veo_provider import XibapiVeoProvider


IMAGE_PROVIDERS: dict[str, BaseImageProvider] = {
    XibapiGptImage2Provider.provider_key: XibapiGptImage2Provider(),
    XibapiNanoBananaProvider.provider_key: XibapiNanoBananaProvider(),
    HelloBabyGoImageProvider.provider_key: HelloBabyGoImageProvider(),
}

VIDEO_PROVIDERS: dict[str, BaseVideoProvider] = {
    XibapiVeoProvider.provider_key: XibapiVeoProvider(),
    JimmyVeoProvider.provider_key: JimmyVeoProvider(),
    HelloBabyGoVeoProvider.provider_key: HelloBabyGoVeoProvider(),
}


def get_image_provider(provider_key: str) -> BaseImageProvider:
    return IMAGE_PROVIDERS.get(provider_key) or next(iter(IMAGE_PROVIDERS.values()))


def get_video_provider(provider_key: str) -> BaseVideoProvider:
    return VIDEO_PROVIDERS.get(provider_key) or next(iter(VIDEO_PROVIDERS.values()))


def provider_display_items(providers: dict[str, BaseImageProvider] | dict[str, BaseVideoProvider]) -> list[tuple[str, str]]:
    return [(key, provider.provider_name) for key, provider in providers.items()]


def model_display_items(provider: BaseImageProvider | BaseVideoProvider) -> list[tuple[str, str]]:
    return [(option["logical_key"], option["display_name"]) for option in provider.get_model_options()]


def model_display_name(provider: BaseImageProvider | BaseVideoProvider, logical_key: str) -> str:
    return provider.get_model_option(logical_key)["display_name"]
