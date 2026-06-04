# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
import time
import threading
from pathlib import Path
import requests

from ..utils import safe_json_loads, find_first_key, parse_progress_percent, guess_mime


# -------------------------
# Preset 映射（你要的 sora-2-portrait-15s）
# t.model 存这个 preset 名称
# -------------------------
PRESETS = {
    "sora-2-portrait-15s": {
        "model": "sora-2",
        "duration": 15,
        "aspect_ratio": "9:16",
    },
    "sora-2-portrait-10s": {
        "model": "sora-2",
        "duration": 10,
        "aspect_ratio": "9:16",
    },
    "sora-2-landscape-15s": {
        "model": "sora-2",
        "duration": 15,
        "aspect_ratio": "16:9",
    },
    "sora-2-landscape-10s": {
        "model": "sora-2",
        "duration": 10,
        "aspect_ratio": "16:9",
    },
}


@dataclass
class ToapisConfig:
    api_key: str
    base_url: str
    preset: str               # 例如 sora-2-portrait-15s
    prompt: str
    image_path: str           # 本地图片路径

    # 可选参数
    connect_timeout: int = 30
    read_timeout: int = 900
    verify_ssl: bool = True
    poll_interval_sec: int = 2

    # metadata
    n: int = 1
    watermark: bool = False
    private: bool = True
    style: str | None = None
    thumbnail: bool = False


def _auth_headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key.strip()}"}


def _extract_error_message(obj: dict, fallback: str = "") -> str:
    """
    ✅ 日志精简：只保留“可读错误信息”，去掉 request id 等尾巴
    目标：显示成 “HTTP 401 无效的令牌”
    """
    msg = find_first_key(obj, ["message", "msg", "error", "detail", "reason"]) or fallback or "failed"
    s = str(msg).replace("\n", " ").replace("\r", " ").strip()

    # 去掉括号里的 request id 等噪音
    # 例如：无效的令牌 (request id: xxxx) -> 无效的令牌
    if "(" in s and ")" in s:
        # 只移除末尾括号段（常见是 request id）
        idx = s.rfind("(")
        if idx > 0:
            s = s[:idx].strip()

    # 长度控制
    if len(s) > 120:
        s = s[:120] + "..."
    return s


def _preset_payload(preset: str) -> dict:
    p = PRESETS.get((preset or "").strip())
    if not p:
        raise ValueError(f"未知 ToAPIs preset: {preset}（请先在 PRESETS 里配置）")
    return dict(p)


def upload_image_to_toapis(cfg: ToapisConfig, on_text=None) -> str:
    """
    POST /v1/uploads/images
    返回 data.url
    """
    url = f"{cfg.base_url.rstrip('/')}/v1/uploads/images"
    headers = _auth_headers(cfg.api_key)
    timeout = (cfg.connect_timeout, cfg.read_timeout)

    if on_text:
        on_text(f"📡 Upload URL: {url}\n")
        on_text(f"🧠 Provider: TOAPIS upload\n")

    fp = None
    try:
        fp = open(cfg.image_path, "rb")
        files = {"file": (Path(cfg.image_path).name, fp, guess_mime(cfg.image_path))}
        r = requests.post(url, headers=headers, files=files, timeout=timeout, verify=cfg.verify_ssl)
    finally:
        try:
            if fp:
                fp.close()
        except Exception:
            pass

    r.encoding = "utf-8"
    obj = safe_json_loads(r.text) or {}

    if r.status_code not in (200, 201):
        msg = _extract_error_message(obj, fallback=f"HTTP {r.status_code}")
        raise RuntimeError(f"HTTP {r.status_code} {msg}")

    # 成功结构：{ success:true, data:{ url: ... } }
    data = obj.get("data") if isinstance(obj, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("upload_fail: response missing data")

    image_url = data.get("url")
    if not image_url or not isinstance(image_url, str):
        raise RuntimeError("upload_fail: response missing data.url")

    if on_text:
        on_text(f"✅ Uploaded image_url: {image_url}\n\n")
    return image_url


def create_video_generation(cfg: ToapisConfig, image_url: str, on_text=None) -> str:
    """
    POST /v1/videos/generations
    返回 id
    """
    url = f"{cfg.base_url.rstrip('/')}/v1/videos/generations"
    headers = {
        **_auth_headers(cfg.api_key),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    timeout = (cfg.connect_timeout, cfg.read_timeout)

    preset = _preset_payload(cfg.preset)

    body = {
        "model": preset["model"],
        "prompt": (cfg.prompt or "").strip(),
        "duration": int(preset["duration"]),
        "aspect_ratio": preset["aspect_ratio"],
        "image_urls": [{"url": image_url}],
        "metadata": {
            "n": int(cfg.n),
            "watermark": bool(cfg.watermark),
            "private": bool(cfg.private),
        },
    }
    if cfg.style:
        body["metadata"]["style"] = cfg.style
    if cfg.thumbnail:
        body["thumbnail"] = True

    if on_text:
        on_text(f"📡 Create URL: {url}\n")
        on_text("🧠 Provider: TOAPIS video generations\n")
        on_text(f"🧠 Preset: {cfg.preset} -> model={preset['model']} duration={preset['duration']} ar={preset['aspect_ratio']}\n")
        on_text("🚀 开始创建视频任务...\n")

    r = requests.post(url, headers=headers, json=body, timeout=timeout, verify=cfg.verify_ssl)
    r.encoding = "utf-8"
    obj = safe_json_loads(r.text) or {}

    if r.status_code not in (200, 201):
        msg = _extract_error_message(obj, fallback=f"HTTP {r.status_code}")
        raise RuntimeError(f"HTTP {r.status_code} {msg}")

    task_id = obj.get("id") if isinstance(obj, dict) else None
    if not task_id or not isinstance(task_id, str):
        raise RuntimeError("create_fail: response missing id")

    if on_text:
        on_text(f"🆔 task_id: {task_id}\n\n")
    return task_id


def poll_video_generation(cfg: ToapisConfig, task_id: str, on_text, on_done, on_error, stop_flag: threading.Event):
    """
    GET /v1/videos/generations/{id}
    读取 status/progress/video_url
    """
    url = f"{cfg.base_url.rstrip('/')}/v1/videos/generations/{task_id}"
    headers = {"Accept": "application/json", **_auth_headers(cfg.api_key)}
    timeout = (cfg.connect_timeout, cfg.read_timeout)

    done_status = {"completed"}
    fail_status = {"failed"}

    last_status = None
    last_progress = None

    on_text(f"📡 Status URL: {url}\n")
    on_text("🔄 开始轮询进度...\n")

    while True:
        if stop_flag.is_set():
            on_error("stopped")
            return

        try:
            r = requests.get(url, headers=headers, timeout=timeout, verify=cfg.verify_ssl)
        except requests.exceptions.Timeout as e:
            on_error(f"poll_timeout:{e}")
            return
        except Exception as e:
            on_error(f"poll_net_err:{e}")
            return

        r.encoding = "utf-8"
        obj = safe_json_loads(r.text) or {}

        if r.status_code != 200:
            msg = _extract_error_message(obj, fallback=f"HTTP {r.status_code}")
            # ✅ 精简为 “HTTP XXX 具体原因”
            on_error(f"HTTP {r.status_code} {msg}")
            return

        status = (obj.get("status") or "").strip()
        progress = obj.get("progress")
        video_url = obj.get("video_url") or find_first_key(obj, ["url", "result_url", "download_url"])

        # 输出变化
        if status and status != last_status:
            on_text(f"> 📌 状态：{status}\n")
            last_status = status

        pval = None
        if isinstance(progress, (int, float)):
            pval = float(progress)
        else:
            pval = parse_progress_percent(r.text)

        if pval is not None:
            if last_progress is None or abs(pval - last_progress) >= 1.0:
                on_text(f"> 🏃 进度：{pval:.0f}%\n")
                last_progress = pval

        st = status.lower()

        if st in done_status:
            on_done(video_url if isinstance(video_url, str) else None)
            return

        if st in fail_status:
            msg = _extract_error_message(obj, fallback="failed")
            on_error(f"failed:{msg}")
            return

        # sleep
        interval = max(1, int(cfg.poll_interval_sec or 2))
        for _ in range(interval):
            if stop_flag.is_set():
                on_error("stopped")
                return
            time.sleep(1)


def run_toapis_create_and_poll(
    cfg: ToapisConfig,
    on_text,
    on_remote_id,
    on_done,
    on_error,
    stop_flag: threading.Event,
):
    """
    ✅ 完整流程：本地图片 -> 上传 -> 创建视频 -> 轮询
    remote_id 就用 task_id
    """
    try:
        # 1) upload local image
        image_url = upload_image_to_toapis(cfg, on_text=on_text)

        # 2) create generation
        task_id = create_video_generation(cfg, image_url, on_text=on_text)

        try:
            on_remote_id(task_id)
        except Exception:
            pass

        # 3) poll
        poll_video_generation(cfg, task_id, on_text, on_done, on_error, stop_flag)

    except Exception as e:
        # ✅ 这里也精简：只显示 “HTTP xxx xxx”
        msg = str(e).replace("\n", " ").strip()
        on_error(msg)
