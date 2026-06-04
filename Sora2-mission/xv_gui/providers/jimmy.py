# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
import re
import time
import threading
from pathlib import Path

import requests

from .oss_uploader import upload_file_and_sign_url
from ..utils import (
    safe_json_loads,
    find_first_key,
    parse_progress_percent,
)


@dataclass
class JimmyConfig:
    api_key: str
    base_url: str
    model: str
    prompt: str
    image_path: str
    duration: int = 15
    orientation: str = "portrait"
    connect_timeout: int = 30
    read_timeout: int = 900
    verify_ssl: bool = True
    poll_interval_sec: int = 3


def _videos_url(base_url: str) -> str:
    b = (base_url or "").strip().rstrip("/")
    low = b.lower()
    if low.endswith("/api/open-api/v1/videos"):
        return b
    if low.endswith("/api/open-api/v1"):
        return f"{b}/videos"
    if low.endswith("/api/open-api"):
        return f"{b}/v1/videos"
    return f"{b}/api/open-api/v1/videos"


def _status_url(base_url: str, task_id: str) -> str:
    rid = (task_id or "").strip().strip("/")
    return f"{_videos_url(base_url)}/{rid}"


def _extract_error_message(obj: dict, fallback: str = "") -> str:
    msg = find_first_key(obj, ["message", "msg", "error", "reason", "detail", "error_message"]) or fallback or "failed"
    s = str(msg).replace("\n", " ").replace("\r", " ").strip()
    return s[:200] + "..." if len(s) > 200 else s


def _extract_task_id(obj: dict, raw_text: str = "") -> str:
    tid = (
        find_first_key(obj, ["task_id", "id", "job_id", "uuid"])
        or find_first_key(obj, ["data.task_id", "data.id", "data.job_id", "data.uuid"])
        or ""
    )
    tid = (tid or "").strip()
    if tid:
        return tid
    m = re.search(r"(task_[0-9a-zA-Z_\-]+)", raw_text or "")
    return m.group(1) if m else ""


def _extract_video_url(obj: dict) -> str:
    v = find_first_key(obj, ["video_url", "url", "result_url", "download_url", "mp4_url", "data.result.video_url"]) or ""
    return (v or "").strip() if isinstance(v, str) else ""


def _build_payload(cfg: JimmyConfig) -> dict:
    image = _resolve_image_url(cfg.image_path)
    return {
        "model": (cfg.model or "sora2Stable").strip(),
        "prompt": (cfg.prompt or "").strip(),
        "duration": int(cfg.duration or 15),
        "orientation": (cfg.orientation or "portrait").strip(),
        "images": [image],
    }


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


def _resolve_image_url(image_path: str) -> str:
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


def run_jimmy_create_only(
    cfg: JimmyConfig,
    on_text,
    on_remote_id,
    on_error,
    stop_flag: threading.Event,
):
    try:
        if stop_flag.is_set():
            on_error("stopped")
            return

        url = _videos_url(cfg.base_url)
        headers = {
            "Authorization": f"Bearer {cfg.api_key.strip()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        timeout = (cfg.connect_timeout, cfg.read_timeout)

        on_text(f"请求地址: {url}\n")
        on_text("提供方: JIMMY 仅创建\n")
        on_text(f"模型: {cfg.model}\n")

        payload = _build_payload(cfg)
        r = requests.post(url, headers=headers, json=payload, timeout=timeout, verify=cfg.verify_ssl)
        r.encoding = "utf-8"
        if r.status_code not in (200, 201):
            obj = safe_json_loads(r.text) or {}
            msg = _extract_error_message(obj, fallback=f"HTTP {r.status_code}")
            on_error(f"create_http:{msg}")
            return

        obj = safe_json_loads(r.text) or {}
        tid = _extract_task_id(obj, r.text)
        if not tid:
            msg = _extract_error_message(obj, fallback="missing task_id")
            on_error(f"create_no_id:{msg}")
            return

        on_text(f"创建成功 | remote_id={tid}\n")
        on_remote_id(tid)
    except Exception as e:
        on_error(str(e))


def run_jimmy_poll_existing(
    cfg: JimmyConfig,
    task_id: str,
    on_text,
    on_done,
    on_error,
    stop_flag: threading.Event,
):
    try:
        headers = {"Authorization": f"Bearer {cfg.api_key.strip()}", "Accept": "application/json"}
        timeout = (cfg.connect_timeout, cfg.read_timeout)
        url = _status_url(cfg.base_url, task_id)

        done_keywords = {"completed", "succeeded", "success", "done", "finished"}
        fail_keywords = {"failed", "error", "rejected", "canceled", "cancelled"}
        last_status = None
        last_progress = None

        on_text(f"状态地址: {url}\n")
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
            if r.status_code != 200:
                obj = safe_json_loads(r.text) or {}
                msg = _extract_error_message(obj, fallback=f"HTTP {r.status_code}")
                on_error(f"poll_http:{msg}")
                return

            obj = safe_json_loads(r.text) or {}
            status = (find_first_key(obj, ["status", "state", "data.status", "data.state"]) or "").strip()
            progress = find_first_key(obj, ["progress", "percent", "percentage", "data.progress"])
            video_url = _extract_video_url(obj)

            pval = None
            if isinstance(progress, (int, float)):
                pval = float(progress)
                if 0 <= pval <= 1.0:
                    pval *= 100.0
            elif isinstance(progress, str):
                m = re.search(r"([0-9]+(?:\.[0-9]+)?)", progress)
                if m:
                    try:
                        pval = float(m.group(1))
                    except Exception:
                        pval = None
            if pval is None:
                pval = parse_progress_percent(r.text)

            if status and status != last_status:
                on_text(f"> 状态: {status}\n")
                last_status = status
            if pval is not None and (last_progress is None or abs(pval - last_progress) >= 0.1):
                on_text(f"> 进度: {pval:.1f}%\n")
                last_progress = pval

            status_l = status.lower() if status else ""
            if video_url or any(k in status_l for k in done_keywords):
                on_done(video_url or None)
                return
            if any(k in status_l for k in fail_keywords):
                msg = _extract_error_message(obj, fallback=status or "failed")
                on_error(f"task_failed:{msg}")
                return

            interval = max(1, int(getattr(cfg, "poll_interval_sec", 3) or 3))
            for _ in range(interval):
                if stop_flag.is_set():
                    on_error("stopped")
                    return
                time.sleep(1)
    except Exception as e:
        on_error(str(e))


def run_jimmy_create_and_poll(
    cfg: JimmyConfig,
    on_text,
    on_remote_id,
    on_done,
    on_error,
    stop_flag: threading.Event,
):
    def _rid_cb(rid: str):
        try:
            on_remote_id(rid)
        except Exception:
            pass
        run_jimmy_poll_existing(cfg, rid, on_text, on_done, on_error, stop_flag)

    run_jimmy_create_only(cfg, on_text, _rid_cb, on_error, stop_flag)


def run_jimmy_poll_once(
    cfg: JimmyConfig,
    task_id: str,
    on_text,
    on_done,
    on_error,
    stop_flag: threading.Event | None = None,
):
    try:
        if stop_flag is not None and stop_flag.is_set():
            on_error("stopped")
            return

        headers = {"Authorization": f"Bearer {cfg.api_key.strip()}", "Accept": "application/json"}
        timeout = (cfg.connect_timeout, cfg.read_timeout)
        url = _status_url(cfg.base_url, task_id)
        r = requests.get(url, headers=headers, timeout=timeout, verify=cfg.verify_ssl)
        r.encoding = "utf-8"

        if r.status_code != 200:
            obj = safe_json_loads(r.text) or {}
            msg = _extract_error_message(obj, fallback=f"HTTP {r.status_code}")
            on_error(f"poll_http:{msg}")
            return

        obj = safe_json_loads(r.text) or {}
        status = (find_first_key(obj, ["status", "state", "data.status", "data.state"]) or "").strip()
        progress = find_first_key(obj, ["progress", "percent", "percentage", "data.progress"])
        video_url = _extract_video_url(obj)

        pval = None
        if isinstance(progress, (int, float)):
            pval = float(progress)
            if 0 <= pval <= 1.0:
                pval *= 100.0
        elif isinstance(progress, str):
            m = re.search(r"([0-9]+(?:\.[0-9]+)?)", progress)
            if m:
                try:
                    pval = float(m.group(1))
                except Exception:
                    pval = None
        if pval is None:
            pval = parse_progress_percent(r.text)

        if status:
            on_text(f"> 状态: {status}\n")
        if pval is not None:
            on_text(f"> 进度: {pval:.1f}%\n")

        status_l = status.lower() if status else ""
        if video_url or any(k in status_l for k in ("completed", "succeeded", "success", "done", "finished")):
            on_done(video_url or None)
            return
        if any(k in status_l for k in ("failed", "error", "rejected", "canceled", "cancelled")):
            msg = _extract_error_message(obj, fallback=status or "failed")
            on_error(f"task_failed:{msg}")
            return
        return
    except requests.exceptions.Timeout as e:
        on_error(f"poll_timeout:{e}")
    except Exception as e:
        on_error(f"poll_net_err:{e}")
