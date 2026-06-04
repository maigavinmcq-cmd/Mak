# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
import json
import mimetypes
from typing import Callable, Any

import requests


TextCB = Callable[[str], None]
DoneCB = Callable[[str | None], None]
ErrCB = Callable[[str], None]
RemoteCB = Callable[[str], None]


@dataclass
class LingkeConfig:
    """
    Lingke Provider Config
    - preset: 你UI里选择的“预设名”，例如 sora-2-portrait-15s
    - 本地图片：image_path -> provider 内部会尝试上传成 URL，再按 Lingke 文档发 create
    """
    api_key: str
    base_url: str
    preset: str

    prompt: str
    image_path: str = ""  # 本地图片（可空）
    verify_ssl: bool = True
    timeout_sec: int = 60
    poll_interval_sec: int = 3
    pending_timeout_sec: int = 60 * 30  # 30min 超时保护


# ---------------------------
# Preset -> Lingke params
# ---------------------------
def _preset_to_params(preset: str) -> dict:
    """
    ✅ 你要求：封装一个模型 sora-2-portrait-15s
    参数固定：
    - model: sora-2
    - orientation: portrait
    - duration: 15
    """
    p = (preset or "").strip()

    if p == "sora-2-portrait-15s":
        return {
            "model": "sora-2",
            "orientation": "portrait",
            "duration": 15,
            "size": "large",
            "watermark": False,
            "private": True,
        }

    # 兜底：给个合理默认（避免崩）
    return {
        "model": "sora-2",
        "orientation": "portrait",
        "duration": 15,
        "size": "large",
        "watermark": False,
        "private": True,
    }


def _headers(token: str) -> dict:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }


def _join_url(base: str, path: str) -> str:
    b = (base or "").rstrip("/")
    p = (path or "").lstrip("/")
    return f"{b}/{p}"


def _safe_json(resp: requests.Response) -> dict | None:
    try:
        return resp.json()
    except Exception:
        try:
            return json.loads(resp.text or "")
        except Exception:
            return None


def _pick_first_url(obj: Any) -> str | None:
    """
    从各种可能字段中抽取 URL
    """
    if obj is None:
        return None
    if isinstance(obj, str):
        s = obj.strip()
        if s.startswith("http"):
            return s
        return None
    if isinstance(obj, dict):
        # 常见字段
        for k in ("url", "video_url", "downloadable_url", "file_url", "cdn_url"):
            v = obj.get(k)
            u = _pick_first_url(v)
            if u:
                return u

        # 你的 completed 样例：detail.url
        if isinstance(obj.get("detail"), dict):
            u = _pick_first_url(obj["detail"].get("url"))
            if u:
                return u

        # 你的 completed 样例：detail.draft_info.downloadable_url / encodings.source.path 等
        detail = obj.get("detail")
        if isinstance(detail, dict):
            u = _pick_first_url(detail.get("downloadable_url"))
            if u:
                return u
            draft = detail.get("draft_info")
            if isinstance(draft, dict):
                u = _pick_first_url(draft.get("downloadable_url"))
                if u:
                    return u
                enc = draft.get("encodings")
                if isinstance(enc, dict):
                    # 依次尝试 source / source_wm / md
                    for kk in ("source", "source_wm", "md"):
                        if isinstance(enc.get(kk), dict):
                            u = _pick_first_url(enc[kk].get("path"))
                            if u:
                                return u

        # 遍历 dict values
        for v in obj.values():
            u = _pick_first_url(v)
            if u:
                return u

    if isinstance(obj, list):
        for it in obj:
            u = _pick_first_url(it)
            if u:
                return u
    return None


def _extract_status(j: dict) -> str:
    # 优先顶层 status
    st = (j.get("status") or "").strip()
    if st:
        return st
    # 有些接口状态藏在 detail.status / detail.pending_info.status
    detail = j.get("detail")
    if isinstance(detail, dict):
        st2 = (detail.get("status") or "").strip()
        if st2:
            return st2
        pend = detail.get("pending_info")
        if isinstance(pend, dict):
            st3 = (pend.get("status") or "").strip()
            if st3:
                return st3
    return ""


def _extract_progress_pct(j: dict) -> float | None:
    """
    你的 pending 样例：detail.pending_info.progress_pct
    """
    detail = j.get("detail")
    if isinstance(detail, dict):
        pend = detail.get("pending_info")
        if isinstance(pend, dict):
            v = pend.get("progress_pct")
            try:
                if v is None:
                    return None
                # progress_pct 可能是 0~1 或 0~100
                fv = float(v)
                if fv <= 1.0:
                    return fv * 100.0
                return fv
            except Exception:
                return None
    return None


# ---------------------------
# Local image -> URL (upload)
# ---------------------------
def _try_upload_local_image(
    cfg: LingkeConfig,
    image_path: str,
    on_text: TextCB,
) -> str:
    """
    由于你给的 Lingke 文档 images 是 URL 数组，这里必须把本地图片变成 URL。
    ✅ 本函数会尝试多个常见上传端点（不同服务商命名不同）：
    - /v1/file/upload
    - /v1/files/upload
    - /v1/image/upload
    - /v1/upload
    - /upload
    - /v1/media/upload
    只要有一个成功返回 url，就继续 create。
    若全部失败，会抛异常（提示你需要先拿到 URL）。
    """
    p = Path(image_path)
    if not p.exists() or not p.is_file():
        raise RuntimeError(f"本地图片不存在：{image_path}")

    mime, _ = mimetypes.guess_type(str(p))
    if not mime:
        mime = "application/octet-stream"

    candidates = [
        "/v1/file/upload",
        "/v1/files/upload",
        "/v1/image/upload",
        "/v1/upload",
        "/v1/media/upload",
        "/upload",
    ]

    token = (cfg.api_key or "").strip()
    if not token:
        raise RuntimeError("Lingke API Key 为空")

    for path in candidates:
        url = _join_url(cfg.base_url, path)
        try:
            on_text(f"📤 Lingke 尝试上传图片: {url}\n")
            with open(p, "rb") as f:
                files = {
                    # 常见字段名：file / image / images
                    "file": (p.name, f, mime),
                }
                resp = requests.post(
                    url,
                    headers=_headers(token),
                    files=files,
                    timeout=cfg.timeout_sec,
                    verify=cfg.verify_ssl,
                )
            if resp.status_code >= 400:
                on_text(f"❌ Upload HTTP {resp.status_code} | {resp.text[:200]}\n")
                continue

            j = _safe_json(resp) or {}
            got = _pick_first_url(j)
            if got:
                on_text(f"✅ Upload OK -> {got}\n")
                return got

            # 某些服务返回 {"data":{"url":...}}
            data = j.get("data") if isinstance(j, dict) else None
            got2 = _pick_first_url(data)
            if got2:
                on_text(f"✅ Upload OK -> {got2}\n")
                return got2

            on_text(f"⚠️ Upload 返回未找到 url 字段，响应={str(j)[:200]}\n")
        except Exception as e:
            on_text(f"⚠️ Upload 异常：{e}\n")
            continue

    raise RuntimeError(
        "Lingke create 需要 images(URL数组)，但自动上传未成功。\n"
        "请确认 Lingke 是否提供上传接口；如果没有，请先把本地图片上传到图床/文件系统得到 URL，再传给 create。"
    )


# ---------------------------
# API calls
# ---------------------------
def _lingke_create(
    cfg: LingkeConfig,
    image_urls: list[str],
    on_text: TextCB,
) -> str:
    """
    POST /v1/video/create
    返回 {"id": "...", "status":"pending"...}
    """
    token = (cfg.api_key or "").strip()
    if not token:
        raise RuntimeError("Lingke API Key 为空")

    params = _preset_to_params(cfg.preset)
    payload = {
        "images": image_urls,
        "model": params["model"],
        "orientation": params["orientation"],
        "prompt": cfg.prompt,
        "size": params.get("size", "large"),
        "duration": params.get("duration", 15),
        "watermark": bool(params.get("watermark", False)),
        "private": bool(params.get("private", True)),
    }

    url = _join_url(cfg.base_url, "/v1/video/create")
    on_text(f"📡 Request URL: {url}\n")
    on_text(f"🧠 Provider: LINGKE create+poll\n")
    on_text(f"🧠 Preset: {cfg.preset}\n")
    on_text(f"🚀 开始创建任务（create）...\n")

    headers = _headers(token)
    headers["Content-Type"] = "application/json"

    resp = requests.post(
        url,
        headers=headers,
        data=json.dumps(payload, ensure_ascii=False),
        timeout=cfg.timeout_sec,
        verify=cfg.verify_ssl,
    )

    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} | {resp.text}")

    j = _safe_json(resp) or {}
    rid = (j.get("id") or "").strip()
    if not rid:
        raise RuntimeError(f"Create 返回缺少 id：{resp.text}")
    on_text(f"✅ Create OK | id={rid}\n")
    return rid


def _lingke_query(
    cfg: LingkeConfig,
    remote_id: str,
) -> dict:
    """
    GET /v1/video/query?id=xxx
    """
    token = (cfg.api_key or "").strip()
    if not token:
        raise RuntimeError("Lingke API Key 为空")

    rid = (remote_id or "").strip()
    if not rid:
        raise RuntimeError("remote_id 为空")

    url = _join_url(cfg.base_url, f"/v1/video/query?id={rid}")
    resp = requests.get(
        url,
        headers=_headers(token),
        timeout=cfg.timeout_sec,
        verify=cfg.verify_ssl,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code} | {resp.text}")

    j = _safe_json(resp)
    if not isinstance(j, dict):
        raise RuntimeError(f"Query 返回非JSON对象：{resp.text[:200]}")
    return j


# ---------------------------
# Public runners (engine style)
# ---------------------------
def run_lingke_create_and_poll(
    cfg: LingkeConfig,
    on_text: TextCB,
    on_remote_id: RemoteCB,
    on_done: DoneCB,
    on_error: ErrCB,
    stop_event,
):
    """
    ✅ 你工程统一回调风格：
    - on_text: 写日志
    - on_remote_id: 拿到 remote id 立刻回写任务
    - on_done(video_url|None)
    - on_error(str)
    """
    try:
        # 1) images: 需要 URL 数组；如果给了本地图片，先上传成 URL
        image_urls: list[str] = []
        if (cfg.image_path or "").strip():
            u = _try_upload_local_image(cfg, cfg.image_path, on_text)
            image_urls = [u]
        else:
            image_urls = []

        # 2) create
        rid = _lingke_create(cfg, image_urls, on_text)
        on_remote_id(rid)

        # 3) poll
        _poll_loop(cfg, rid, on_text, on_done, on_error, stop_event)

    except Exception as e:
        on_error(str(e))


def run_lingke_poll_existing(
    cfg: LingkeConfig,
    remote_id: str,
    on_text: TextCB,
    on_done: DoneCB,
    on_error: ErrCB,
    stop_event,
):
    """
    ✅ 续跑：只 query/poll，不 create
    """
    try:
        rid = (remote_id or "").strip()
        if not rid:
            raise RuntimeError("remote_id 为空，无法续跑")
        _poll_loop(cfg, rid, on_text, on_done, on_error, stop_event)
    except Exception as e:
        on_error(str(e))


def _poll_loop(
    cfg: LingkeConfig,
    remote_id: str,
    on_text: TextCB,
    on_done: DoneCB,
    on_error: ErrCB,
    stop_event,
):
    started = time.time()
    last_pct = None

    on_text(f"🔁 开始轮询任务状态 | id={remote_id}\n")

    while True:
        # stop
        try:
            if stop_event is not None and stop_event.is_set():
                on_error("stopped_by_user")
                return
        except Exception:
            pass

        # timeout
        if cfg.pending_timeout_sec > 0 and (time.time() - started) > cfg.pending_timeout_sec:
            on_error("pending_timeout")
            return

        try:
            j = _lingke_query(cfg, remote_id)
            st = _extract_status(j).lower()

            pct = _extract_progress_pct(j)
            if pct is not None:
                if (last_pct is None) or (abs(pct - last_pct) >= 1.0):
                    on_text(f"⏳ status={st} | progress={pct:.1f}%\n")
                    last_pct = pct
            else:
                on_text(f"⏳ status={st}\n")

            if st in ("completed", "success", "succeeded", "done"):
                video = _pick_first_url(j)
                if video:
                    on_text("✅ completed：发现 video_url\n")
                    on_done(video)
                else:
                    on_text("⚠️ completed：未解析到 video_url\n")
                    on_done(None)
                return

            if st in ("failed", "error", "canceled", "cancelled"):
                # 尝试拿 failure_reason
                reason = ""
                detail = j.get("detail")
                if isinstance(detail, dict):
                    pend = detail.get("pending_info")
                    if isinstance(pend, dict):
                        reason = (pend.get("failure_reason") or "").strip()
                on_error(reason or f"lingke_status={st}")
                return

        except Exception as e:
            # query 失败也要继续由外层重试策略处理，这里直接报错交给上层
            on_error(str(e))
            return

        time.sleep(max(1, int(cfg.poll_interval_sec or 3)))
