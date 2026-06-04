# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
import re
import time
import threading
from pathlib import Path
import mimetypes

import requests

from ..utils import (
    build_xintian_videos_url,
    build_xintian_video_status_url,
    safe_json_loads,
    find_first_key,
    parse_progress_percent,
)


@dataclass
class XintianConfig:
    api_key: str
    base_url: str
    model: str
    prompt: str
    image_path: str
    connect_timeout: int = 30
    read_timeout: int = 900
    verify_ssl: bool = True
    poll_interval_sec: int = 3


def _extract_error_message(obj: dict, fallback: str = "") -> str:
    """从响应里提取更友好的错误描述。"""
    msg = find_first_key(obj, ["message", "msg"])
    if not msg:
        msg = find_first_key(obj, ["error", "reason", "detail", "error_message", "err_message"])
    if not msg:
        msg = fallback or "failed"

    s = str(msg).replace("\n", " ").replace("\r", " ").strip()
    if len(s) > 200:
        s = s[:200] + "..."
    return s


def _extract_video_id(obj: dict, raw_text: str = "") -> str:
    vid = (
        find_first_key(obj, ["video_id", "id", "task_id", "job_id", "uuid"])
        or find_first_key(obj, ["data.video_id", "data.id", "data.task_id", "data.job_id", "data.uuid"])
        or ""
    )
    vid = (vid or "").strip()
    if vid:
        return vid

    m = re.search(r"(video_[0-9a-fA-F\-]{8,})", raw_text or "")
    if m:
        return m.group(1)

    m2 = re.search(r"(task_[0-9a-zA-Z_\-]+)", raw_text or "")
    return m2.group(1) if m2 else ""


def _build_create_payload(cfg: XintianConfig) -> dict:
    return {
        "model": (cfg.model or "sora-2-portrait-15s").strip(),
        "prompt": (cfg.prompt or "").strip(),
    }

def _build_multipart_args(cfg: XintianConfig):
    payload = _build_create_payload(cfg)
    image_path = (cfg.image_path or "").strip()
    if not image_path:
        raise ValueError("xintian_missing_input_reference")
    if image_path.lower().startswith(("http://", "https://")):
        raise ValueError("xintian_input_reference_must_be_local_file")

    p = Path(image_path)
    if not p.exists() or not p.is_file():
        raise ValueError(f"xintian_input_reference_not_found:{image_path}")

    mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    fh = p.open("rb")
    files = {
        "input_reference": (p.name, fh, mime),
    }
    return payload, files, fh


def run_xintian_upload_and_poll(
    cfg: XintianConfig,
    on_text,
    on_remote_id,
    on_done,
    on_error,
    stop_flag: threading.Event,
):
    """创建任务并持续轮询直到成功/失败。"""
    try:
        post_url = build_xintian_videos_url(cfg.base_url)
        headers = {
            "Authorization": f"Bearer {cfg.api_key.strip()}",
            "Accept": "application/json",
        }
        timeout = (cfg.connect_timeout, cfg.read_timeout)

        on_text(f"请求地址: {post_url}\n")
        on_text("提供方: XINTIAN 上传+轮询\n")
        on_text(f"模型: {cfg.model}\n")

        payload, files, fh = _build_multipart_args(cfg)
        try:
            r = requests.post(
                post_url,
                headers=headers,
                data=payload,
                files=files,
                timeout=timeout,
                verify=cfg.verify_ssl,
            )
        finally:
            fh.close()

        r.encoding = "utf-8"
        if r.status_code not in (200, 201):
            obj = safe_json_loads(r.text) or {}
            msg = _extract_error_message(obj, fallback=f"HTTP {r.status_code}")
            on_error(f"create_fail:{msg}")
            return

        obj = safe_json_loads(r.text) or {}
        video_id = _extract_video_id(obj, r.text)
        if not video_id:
            on_error("create_fail:missing video_id")
            return

        try:
            on_remote_id(video_id)
        except Exception:
            pass

        run_xintian_poll_existing(cfg, video_id, on_text, on_done, on_error, stop_flag)

    except Exception as e:
        on_error(str(e))


def run_xintian_poll_existing(
    cfg: XintianConfig,
    video_id: str,
    on_text,
    on_done,
    on_error,
    stop_flag: threading.Event,
):
    """按 remote_id 轮询，直到成功/失败/停止。"""
    try:
        headers = {"Authorization": f"Bearer {cfg.api_key.strip()}"}
        timeout = (cfg.connect_timeout, cfg.read_timeout)
        status_url = build_xintian_video_status_url(cfg.base_url, video_id)

        done_keywords = {"succeeded", "success", "completed", "done", "finished"}
        fail_keywords = {"failed", "error", "rejected", "canceled", "cancelled"}

        final_video_url = None
        last_status = None
        last_progress = None

        on_text(f"状态地址: {status_url}\n")

        while True:
            if stop_flag.is_set():
                on_error("stopped")
                return

            try:
                gr = requests.get(status_url, headers=headers, timeout=timeout, verify=cfg.verify_ssl)
            except requests.exceptions.Timeout as e:
                on_error(f"poll_timeout:{e}")
                return
            except Exception as e:
                on_error(f"poll_net_err:{e}")
                return

            gr.encoding = "utf-8"
            if gr.status_code != 200:
                obj = safe_json_loads(gr.text) or {}
                msg = _extract_error_message(obj, fallback=f"HTTP {gr.status_code}")
                on_error(f"poll_http:{msg}")
                return

            obj = safe_json_loads(gr.text) or {}
            status = (find_first_key(obj, ["status", "state"]) or "").strip()
            progress = find_first_key(obj, ["progress", "percent", "percentage", "data.progress"])
            url = find_first_key(obj, ["video_url", "url", "result_url", "download_url", "mp4_url", "data.result.video_url"])

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
                pval = parse_progress_percent(gr.text)

            if status and status != last_status:
                on_text(f"> 状态: {status}\n")
                last_status = status

            if pval is not None and (last_progress is None or abs(pval - last_progress) >= 0.1):
                on_text(f"> 进度: {pval:.1f}%\n")
                last_progress = pval

            if isinstance(url, str) and url.strip():
                final_video_url = url.strip()

            status_l = status.lower() if status else ""
            if final_video_url or any(k in status_l for k in done_keywords):
                on_done(final_video_url)
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


def run_xintian_create_only(
    cfg: XintianConfig,
    on_text,
    on_remote_id,
    on_error,
    stop_flag: threading.Event,
):
    """只创建任务，拿到 remote_id 后立即返回，不做轮询。"""
    try:
        if stop_flag.is_set():
            on_error("stopped")
            return

        url = build_xintian_videos_url(cfg.base_url)
        headers = {
            "Authorization": f"Bearer {cfg.api_key.strip()}",
            "Accept": "application/json",
        }
        timeout = (cfg.connect_timeout, cfg.read_timeout)

        on_text(f"请求地址: {url}\n")
        on_text("提供方: XINTIAN 仅创建\n")
        on_text(f"模型: {cfg.model}\n")

        payload, files, fh = _build_multipart_args(cfg)
        try:
            r = requests.post(url, headers=headers, data=payload, files=files, timeout=timeout, verify=cfg.verify_ssl)
        finally:
            fh.close()

        r.encoding = "utf-8"
        if r.status_code not in (200, 201):
            obj = safe_json_loads(r.text) or {}
            msg = _extract_error_message(obj, fallback=f"HTTP {r.status_code}")
            on_error(f"create_http:{msg}")
            return

        obj = safe_json_loads(r.text) or {}
        vid = _extract_video_id(obj, r.text)
        create_status = (find_first_key(obj, ["status", "state", "data.status", "data.state"]) or "").strip().lower()

        if not vid:
            msg = _extract_error_message(obj, fallback="missing video_id")
            on_error(f"create_no_id:{msg}")
            return

        # 你的规则：create 阶段要求状态至少是 queue/queued 才放行下一个任务。
        if create_status and create_status not in ("queue", "queued", "pending", "processing", "running", "submitted"):
            on_error(f"create_bad_status:{create_status}")
            return

        if create_status:
            on_text(f"创建状态={create_status}\n")
        on_text(f"创建成功 | remote_id={vid}\n")
        on_remote_id(vid)

    except Exception as e:
        on_error(str(e))


def run_xintian_poll_once(
    cfg: XintianConfig,
    video_id: str,
    on_text,
    on_done,
    on_error,
    stop_flag: threading.Event | None = None,
):
    """只轮询一次（单次 GET）。"""
    try:
        if stop_flag is not None and stop_flag.is_set():
            on_error("stopped")
            return

        headers = {"Authorization": f"Bearer {cfg.api_key.strip()}"}
        timeout = (cfg.connect_timeout, cfg.read_timeout)
        status_url = build_xintian_video_status_url(cfg.base_url, video_id)

        gr = requests.get(status_url, headers=headers, timeout=timeout, verify=cfg.verify_ssl)
        gr.encoding = "utf-8"

        if gr.status_code != 200:
            obj = safe_json_loads(gr.text) or {}
            msg = _extract_error_message(obj, fallback=f"HTTP {gr.status_code}")
            on_error(f"poll_http:{msg}")
            return

        obj = safe_json_loads(gr.text) or {}
        status = (find_first_key(obj, ["status", "state", "data.status", "data.state"]) or "").strip()
        progress = find_first_key(obj, ["progress", "percent", "percentage", "data.progress"])
        url = find_first_key(obj, ["video_url", "url", "result_url", "download_url", "mp4_url", "data.result.video_url"])

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
            pval = parse_progress_percent(gr.text)

        if status:
            on_text(f"> 状态: {status}\n")
        if pval is not None:
            on_text(f"> 进度: {pval:.1f}%\n")

        final_video_url = (url or "").strip() if isinstance(url, str) else ""
        done_keywords = {"succeeded", "success", "completed", "done", "finished"}
        fail_keywords = {"failed", "error", "rejected", "canceled", "cancelled"}
        status_l = status.lower() if status else ""

        if final_video_url or any(k in status_l for k in done_keywords):
            on_done(final_video_url or None)
            return

        if any(k in status_l for k in fail_keywords):
            msg = _extract_error_message(obj, fallback=status or "failed")
            on_error(f"task_failed:{msg}")
            return

        # still running: 不触发 done/error
        return

    except requests.exceptions.Timeout as e:
        on_error(f"poll_timeout:{e}")
    except Exception as e:
        on_error(f"poll_net_err:{e}")
