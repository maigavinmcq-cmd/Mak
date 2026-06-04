# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
import re
import time
from pathlib import Path

import requests

from common import normalize_base_url, safe_json, find_first_key, parse_progress_percent, extract_error_message
from oss_uploader import upload_file_and_sign_url


@dataclass
class JimmyConfig:
    api_key: str
    base_url: str = "https://www.jimmyai.cn"
    model: str = "sora2Openai"
    prompt: str = ""
    image_url: str = ""
    duration: int = 15
    orientation: str = "portrait"
    connect_timeout: int = 30
    read_timeout: int = 900
    verify_ssl: bool = True
    poll_interval_sec: int = 3


@dataclass
class JimmyOssConfig:
    api_key: str
    base_url: str = "https://www.jimmyai.cn"
    model: str = "sora2Openai"
    prompt: str = ""
    image_path: str = ""
    duration: int = 15
    orientation: str = "portrait"
    connect_timeout: int = 30
    read_timeout: int = 900
    verify_ssl: bool = True
    poll_interval_sec: int = 3


def videos_url(base_url: str) -> str:
    b = normalize_base_url(base_url)
    low = b.lower()
    if low.endswith("/api/open-api/v1/videos"):
        return b
    if low.endswith("/api/open-api/v1"):
        return f"{b}/videos"
    if low.endswith("/api/open-api"):
        return f"{b}/v1/videos"
    return f"{b}/api/open-api/v1/videos"


def status_url(base_url: str, task_id: str) -> str:
    return f"{videos_url(base_url)}/{task_id.strip().strip('/')}"


def _read_text_file(path: Path) -> str:
    for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030"):
        try:
            return path.read_text(encoding=enc)
        except Exception:
            continue
    return path.read_text(encoding="utf-8", errors="ignore")


def _extract_first_http_url(text: str) -> str:
    m = re.search(r"https?://[^\s\"'<>]+", text or "", flags=re.IGNORECASE)
    if not m:
        return ""
    return m.group(0).strip().rstrip(".,;)]}")


def _resolve_local_image_file(p: Path) -> Path:
    if p.exists() and p.is_file():
        return p

    base_dir = p if p.is_dir() else p.parent
    if not base_dir.exists() or not base_dir.is_dir():
        raise ValueError(f"jimmy_image_dir_not_found:{base_dir}")

    patterns = ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.bmp")
    for pat in patterns:
        candidates = sorted(base_dir.glob(pat))
        if candidates:
            return candidates[0]
        candidates = sorted(base_dir.glob(pat.upper()))
        if candidates:
            return candidates[0]

    raise ValueError(f"jimmy_no_local_image_for_oss:{base_dir}")


def resolve_image_url(image_path: str) -> str:
    raw = (image_path or "").strip()
    if not raw:
        raise ValueError("jimmy_missing_image_path")
    if raw.lower().startswith(("http://", "https://")):
        return raw

    p = Path(raw)
    base_dir = p if p.is_dir() else p.parent
    txt = base_dir / "url.txt"
    if txt.exists() and txt.is_file():
        content = _read_text_file(txt)
        url = _extract_first_http_url(content)
        if url and url.lower().startswith(("http://", "https://")):
            return url
        raise ValueError(f"jimmy_invalid_url_txt:{txt}")

    local_img = _resolve_local_image_file(p)
    return upload_file_and_sign_url(str(local_img))


def create_video(cfg: JimmyConfig) -> dict:
    url = videos_url(cfg.base_url)
    headers = {
        "Authorization": f"Bearer {cfg.api_key.strip()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "model": cfg.model.strip(),
        "prompt": cfg.prompt.strip(),
        "duration": int(cfg.duration),
        "orientation": cfg.orientation.strip(),
        "images": [cfg.image_url.strip()],
    }
    resp = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=(cfg.connect_timeout, cfg.read_timeout),
        verify=cfg.verify_ssl,
    )
    obj = safe_json(resp)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"jimmy create failed: {extract_error_message(obj, f'HTTP {resp.status_code}')}")
    task_id = find_first_key(obj, ["task_id", "id", "job_id", "uuid", "data.task_id", "data.id"]) or ""
    task_id = str(task_id).strip()
    if not task_id:
        raise RuntimeError(f"jimmy create missing task_id: {extract_error_message(obj, 'missing task_id')}")
    return {"task_id": task_id, "raw": obj}


def create_video_via_oss(cfg: JimmyOssConfig) -> dict:
    image_url = resolve_image_url(cfg.image_path)
    direct_cfg = JimmyConfig(
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        model=cfg.model,
        prompt=cfg.prompt,
        image_url=image_url,
        duration=cfg.duration,
        orientation=cfg.orientation,
        connect_timeout=cfg.connect_timeout,
        read_timeout=cfg.read_timeout,
        verify_ssl=cfg.verify_ssl,
        poll_interval_sec=cfg.poll_interval_sec,
    )
    return create_video(direct_cfg)


def poll_video_once(cfg: JimmyConfig, task_id: str) -> dict:
    url = status_url(cfg.base_url, task_id)
    headers = {
        "Authorization": f"Bearer {cfg.api_key.strip()}",
        "Accept": "application/json",
    }
    resp = requests.get(
        url,
        headers=headers,
        timeout=(cfg.connect_timeout, cfg.read_timeout),
        verify=cfg.verify_ssl,
    )
    obj = safe_json(resp)
    if resp.status_code != 200:
        raise RuntimeError(f"jimmy poll failed: {extract_error_message(obj, f'HTTP {resp.status_code}')}")

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


def create_and_wait(cfg: JimmyConfig, max_wait_sec: int = 1800) -> dict:
    created = create_video(cfg)
    task_id = created["task_id"]
    start = time.time()
    while True:
        result = poll_video_once(cfg, task_id)
        status_l = (result["status"] or "").lower()
        if result["video_url"] or status_l in {"completed", "succeeded", "success", "done", "finished"}:
            return {"task_id": task_id, **result}
        if status_l in {"failed", "error", "rejected", "canceled", "cancelled"}:
            raise RuntimeError(f"jimmy task failed: {result['raw']}")
        if time.time() - start > max_wait_sec:
            raise TimeoutError("jimmy poll timeout")
        time.sleep(max(1, int(cfg.poll_interval_sec)))


def create_and_wait_via_oss(cfg: JimmyOssConfig, max_wait_sec: int = 1800) -> dict:
    created = create_video_via_oss(cfg)
    task_id = created["task_id"]
    direct_cfg = JimmyConfig(
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        model=cfg.model,
        prompt=cfg.prompt,
        image_url="",
        duration=cfg.duration,
        orientation=cfg.orientation,
        connect_timeout=cfg.connect_timeout,
        read_timeout=cfg.read_timeout,
        verify_ssl=cfg.verify_ssl,
        poll_interval_sec=cfg.poll_interval_sec,
    )
    start = time.time()
    while True:
        result = poll_video_once(direct_cfg, task_id)
        status_l = (result["status"] or "").lower()
        if result["video_url"] or status_l in {"completed", "succeeded", "success", "done", "finished"}:
            return {"task_id": task_id, **result}
        if status_l in {"failed", "error", "rejected", "canceled", "cancelled"}:
            raise RuntimeError(f"jimmy task failed: {result['raw']}")
        if time.time() - start > max_wait_sec:
            raise TimeoutError("jimmy poll timeout")
        time.sleep(max(1, int(cfg.poll_interval_sec)))
