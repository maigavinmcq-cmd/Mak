# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time

import requests

from common import normalize_base_url, safe_json, find_first_key, parse_progress_percent, extract_error_message, guess_mime


@dataclass
class XintianConfig:
    api_key: str
    base_url: str = "https://api.xintianwengai.com"
    model: str = "sora-2-portrait-15s"
    prompt: str = ""
    image_path: str = ""
    connect_timeout: int = 30
    read_timeout: int = 900
    verify_ssl: bool = True
    poll_interval_sec: int = 3


def videos_url(base_url: str) -> str:
    b = normalize_base_url(base_url)
    low = b.lower()
    if low.endswith("/v1/videos"):
        return b
    if low.endswith("/v1"):
        return f"{b}/videos"
    return f"{b}/v1/videos"


def status_url(base_url: str, video_id: str) -> str:
    base = videos_url(base_url)
    rid = video_id.strip().strip("/")
    if "/v1/videos/" in rid:
        rid = rid.split("/v1/videos/", 1)[-1].strip("/")
    return f"{base}/{rid}"


def create_video(cfg: XintianConfig) -> dict:
    image = Path(cfg.image_path)
    if not image.exists() or not image.is_file():
        raise FileNotFoundError(f"xintian image not found: {image}")

    url = videos_url(cfg.base_url)
    headers = {
        "Authorization": f"Bearer {cfg.api_key.strip()}",
        "Accept": "application/json",
    }
    data = {
        "model": cfg.model.strip(),
        "prompt": cfg.prompt.strip(),
    }

    with open(image, "rb") as f:
        files = {
            "input_reference": (image.name, f, guess_mime(str(image))),
        }
        resp = requests.post(
            url,
            headers=headers,
            data=data,
            files=files,
            timeout=(cfg.connect_timeout, cfg.read_timeout),
            verify=cfg.verify_ssl,
        )

    obj = safe_json(resp)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"xintian create failed: {extract_error_message(obj, f'HTTP {resp.status_code}')}")
    task_id = find_first_key(obj, ["video_id", "id", "task_id", "job_id", "uuid", "data.video_id", "data.id"]) or ""
    task_id = str(task_id).strip()
    if not task_id:
        raise RuntimeError("xintian create missing video_id")
    return {"task_id": task_id, "raw": obj}


def poll_video_once(cfg: XintianConfig, task_id: str) -> dict:
    url = status_url(cfg.base_url, task_id)
    headers = {
        "Authorization": f"Bearer {cfg.api_key.strip()}",
    }
    resp = requests.get(
        url,
        headers=headers,
        timeout=(cfg.connect_timeout, cfg.read_timeout),
        verify=cfg.verify_ssl,
    )
    obj = safe_json(resp)
    if resp.status_code != 200:
        raise RuntimeError(f"xintian poll failed: {extract_error_message(obj, f'HTTP {resp.status_code}')}")

    status = str(find_first_key(obj, ["status", "state", "data.status", "data.state"]) or "").strip()
    progress = find_first_key(obj, ["progress", "percent", "percentage", "data.progress"])
    video_url = find_first_key(obj, ["video_url", "url", "result_url", "download_url", "mp4_url", "data.result.video_url"])

    pval = None
    if isinstance(progress, (int, float)):
        pval = float(progress)
        if 0 <= pval <= 1:
            pval *= 100
    elif isinstance(progress, str):
        pval = parse_progress_percent(progress)

    return {
        "status": status,
        "progress": pval,
        "video_url": (str(video_url).strip() if isinstance(video_url, str) else ""),
        "raw": obj,
    }


def create_and_wait(cfg: XintianConfig, max_wait_sec: int = 1800) -> dict:
    created = create_video(cfg)
    task_id = created["task_id"]
    start = time.time()
    while True:
        result = poll_video_once(cfg, task_id)
        status_l = (result["status"] or "").lower()
        if result["video_url"] or status_l in {"completed", "succeeded", "success", "done", "finished"}:
            return {"task_id": task_id, **result}
        if status_l in {"failed", "error", "rejected", "canceled", "cancelled"}:
            raise RuntimeError(f"xintian task failed: {result['raw']}")
        if time.time() - start > max_wait_sec:
            raise TimeoutError("xintian poll timeout")
        time.sleep(max(1, int(cfg.poll_interval_sec)))
