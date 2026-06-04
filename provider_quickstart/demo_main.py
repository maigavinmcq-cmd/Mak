# -*- coding: utf-8 -*-
from jimmy_provider import (
    JimmyConfig,
    JimmyOssConfig,
    create_and_wait as jimmy_create_and_wait,
    create_and_wait_via_oss as jimmy_create_and_wait_via_oss,
)
from dyuapi_provider import DyuapiConfig, create_and_wait as dyuapi_create_and_wait
from xintian_provider import XintianConfig, create_and_wait as xintian_create_and_wait


def demo_jimmy():
    cfg = JimmyConfig(
        api_key="YOUR_JIMMY_API_KEY",
        base_url="https://www.jimmyai.cn",
        model="sora2Openai",
        prompt="请基于参考图生成15秒竖版真实风格视频",
        image_url="https://your-image-url.com/demo.png",
        duration=15,
        orientation="portrait",
    )
    result = jimmy_create_and_wait(cfg)
    print("Jimmy task_id:", result["task_id"])
    print("Jimmy video_url:", result["video_url"])


def demo_jimmy_oss():
    cfg = JimmyOssConfig(
        api_key="YOUR_JIMMY_API_KEY",
        base_url="https://www.jimmyai.cn",
        model="sora2Openai",
        prompt="请基于参考图生成15秒竖版真实风格视频",
        image_path=r"C:\input\demo.png",
        duration=15,
        orientation="portrait",
    )
    result = jimmy_create_and_wait_via_oss(cfg)
    print("Jimmy OSS task_id:", result["task_id"])
    print("Jimmy OSS video_url:", result["video_url"])


def demo_dyuapi():
    cfg = DyuapiConfig(
        api_key="YOUR_DYUAPI_KEY",
        base_url="https://api.dyuapi.com",
        model="sora2-portrait-15s",
        prompt="请基于参考图生成15秒竖版真实风格视频",
        image_path=r"C:\input\demo.png",
        size="720x1280",
        seconds="15",
    )
    result = dyuapi_create_and_wait(cfg)
    print("DYUAPI task_id:", result["task_id"])
    print("DYUAPI video_url:", result["video_url"])


def demo_xintian():
    cfg = XintianConfig(
        api_key="YOUR_XINTIAN_API_KEY",
        base_url="https://api.xintianwengai.com",
        model="sora-2-portrait-15s",
        prompt="请基于参考图生成15秒竖版真实风格视频",
        image_path=r"C:\input\demo.png",
    )
    result = xintian_create_and_wait(cfg)
    print("XINTIAN task_id:", result["task_id"])
    print("XINTIAN video_url:", result["video_url"])


if __name__ == "__main__":
    # 按需打开其中一个示例
    # demo_jimmy()
    # demo_jimmy_oss()
    # demo_dyuapi()
    # demo_xintian()
    pass
