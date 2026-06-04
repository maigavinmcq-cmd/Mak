# -*- coding: utf-8 -*-
from __future__ import annotations

from jimmy_provider import (
    JimmyConfig,
    JimmyOssConfig,
    create_and_wait as jimmy_create_and_wait,
    create_and_wait_via_oss as jimmy_create_and_wait_via_oss,
)
from dyuapi_provider import DyuapiConfig, create_and_wait as dyuapi_create_and_wait
from xintian_provider import XintianConfig, create_and_wait as xintian_create_and_wait


SUPPORTED_PROVIDERS = ("jimmy", "jimmy_oss", "dyuapi", "xintian")


def run_provider(provider: str, config: dict, max_wait_sec: int = 1800) -> dict:
    provider = (provider or "").strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f"unsupported provider: {provider}")

    if provider == "jimmy":
        cfg = JimmyConfig(**config)
        return jimmy_create_and_wait(cfg, max_wait_sec=max_wait_sec)

    if provider == "jimmy_oss":
        cfg = JimmyOssConfig(**config)
        return jimmy_create_and_wait_via_oss(cfg, max_wait_sec=max_wait_sec)

    if provider == "dyuapi":
        cfg = DyuapiConfig(**config)
        return dyuapi_create_and_wait(cfg, max_wait_sec=max_wait_sec)

    if provider == "xintian":
        cfg = XintianConfig(**config)
        return xintian_create_and_wait(cfg, max_wait_sec=max_wait_sec)

    raise ValueError(f"unsupported provider: {provider}")


if __name__ == "__main__":
    #示例:
    # result = run_provider(
    #     "dyuapi",
    #     {
    #         "api_key": "YOUR_DYUAPI_KEY",
    #         "base_url": "https://api.dyuapi.com",
    #         "model": "sora2-portrait-15s",
    #         "prompt": "请基于参考图生成15秒竖版真实风格视频",
    #         "image_path": r"C:\\Users\\22892\\Downloads\\screenshot-20260321-165606 (1).png",
    #         "size": "720x1280",
    #         "seconds": "15",
    #     },
    # )
    # print(result)
    pass
