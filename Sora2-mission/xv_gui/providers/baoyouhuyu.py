# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
import mimetypes
import re
import threading
import time
from pathlib import Path
import hashlib
import tempfile

import requests

from ..utils import find_first_key, parse_progress_percent, safe_json_loads


@dataclass
class BaoyouhuyuConfig:
    api_key: str
    base_url: str
    model: str
    prompt: str
    image_path: str
    seconds: str = "8"
    connect_timeout: int = 60
    read_timeout: int = 180
    verify_ssl: bool = True
    poll_interval_sec: int = 3


_RESIZE_TARGETS_PORTRAIT = (720, 1280)
_RESIZE_TARGETS_LANDSCAPE = (1280, 720)
_RESIZE_TARGETS_SQUARE = (1024, 1024)
_HTTP_RETRYABLE_STATUS = {429, 500, 502, 503, 504, 520, 522, 524}
_REQUEST_RETRY_MAX = 3


def _norm_base(base_url: str) -> str:
    return (base_url or "").strip().rstrip("/")


def _videos_url(base_url: str) -> str:
    base = _norm_base(base_url)
    low = base.lower()
    if low.endswith("/v1/videos"):
        return base
    if low.endswith("/v1"):
        return f"{base}/videos"
    return f"{base}/v1/videos"


def _status_url(base_url: str, task_id: str) -> str:
    rid = (task_id or "").strip().strip("/")
    if "/v1/videos/" in rid:
        rid = rid.split("/v1/videos/", 1)[-1].strip("/")
    return f"{_videos_url(base_url)}/{rid}"


def _content_url(base_url: str, task_id: str) -> str:
    rid = (task_id or "").strip().strip("/")
    if "/v1/videos/" in rid:
        rid = rid.split("/v1/videos/", 1)[-1].strip("/")
    return f"{_videos_url(base_url)}/{rid}/content"


def _extract_error_message(obj: dict, fallback: str = "") -> str:
    msg = (
        find_first_key(obj, ["error.message", "message", "msg", "error", "detail", "reason"])
        or fallback
        or "failed"
    )
    s = str(msg).replace("\n", " ").replace("\r", " ").strip()
    if len(s) > 200:
        s = s[:200] + "..."
    return s


def _extract_task_id(obj: dict, raw_text: str = "") -> str:
    rid = (
        find_first_key(obj, ["id", "task_id", "video_id", "data.id", "data.task_id", "data.video_id"])
        or ""
    )
    rid = str(rid).strip()
    if rid:
        return rid
    m = re.search(r'"id"\s*:\s*"([^"]+)"', raw_text or "")
    return (m.group(1).strip() if m else "")


def _infer_seconds(model: str, fallback: str = "8") -> str:
    m = re.search(r"(\d{1,3})\s*s\b", (model or "").lower())
    if m:
        return m.group(1)
    return (fallback or "8").strip() or "8"


def _pick_target_size(model: str, src_w: int, src_h: int) -> tuple[int, int]:
    ml = (model or "").lower()
    if "portrait" in ml:
        return _RESIZE_TARGETS_PORTRAIT
    if "landscape" in ml:
        return _RESIZE_TARGETS_LANDSCAPE
    if "square" in ml:
        return _RESIZE_TARGETS_SQUARE

    if src_w == src_h:
        return _RESIZE_TARGETS_SQUARE
    if src_w > src_h:
        return _RESIZE_TARGETS_LANDSCAPE
    return _RESIZE_TARGETS_PORTRAIT


def _load_image_size(image_path: str) -> tuple[int, int] | None:
    try:
        from PIL import Image  # type: ignore
        with Image.open(image_path) as im:
            return int(im.size[0]), int(im.size[1])
    except Exception:
        return None


def _candidate_sizes(model: str, src_w: int, src_h: int) -> list[tuple[int, int]]:
    """
    Try multiple known buckets for providers that enforce strict inpaint size.
    """
    primary = _pick_target_size(model, src_w, src_h)
    # Keep portrait first for sora-2-vip style models without explicit orientation.
    # Order matters: first matched size will be used.
    common = [
        _RESIZE_TARGETS_PORTRAIT,
        _RESIZE_TARGETS_LANDSCAPE,
        _RESIZE_TARGETS_SQUARE,
        (1376, 768),
        (768, 1376),
        (1360, 768),
        (768, 1360),
    ]
    ordered = [primary] + common
    out = []
    seen = set()
    for w, h in ordered:
        k = (int(w), int(h))
        if k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out


def _render_resized_copy(image_path: str, model: str, target: tuple[int, int], on_text=None) -> str:
    """
    Return image path that exactly matches target size.
    Original file is never overwritten.
    """
    try:
        from PIL import Image, ImageOps  # type: ignore
    except Exception:
        if on_text:
            on_text("未检测到 Pillow，跳过自动缩放。\n")
        return image_path

    p = Path(image_path)
    try:
        with Image.open(p) as im:
            src_w, src_h = im.size
            target_w, target_h = int(target[0]), int(target[1])
            if (src_w, src_h) == (target_w, target_h):
                return image_path

            # Stable output name to avoid repeated duplicate processing
            sig = hashlib.sha1(
                f"{p.resolve()}|{model}|{target_w}x{target_h}".encode("utf-8")
            ).hexdigest()[:10]
            out_dir = Path(tempfile.gettempdir()) / "sora2_auto_resized"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"{p.stem}_{target_w}x{target_h}_{sig}.png"

            if not out_path.exists():
                work = im.convert("RGB")
                fit = ImageOps.pad(
                    work,
                    (target_w, target_h),
                    method=getattr(Image, "Resampling", Image).LANCZOS,
                    color=(0, 0, 0),
                )
                fit.save(out_path, format="PNG", optimize=True)

            if on_text:
                on_text(
                    f"自动缩放图片: {src_w}x{src_h} -> {target_w}x{target_h}\n"
                    f"使用副本: {out_path}\n"
                )
            return str(out_path)
    except Exception as e:
        if on_text:
            on_text(f"自动缩放失败，继续使用原图: {e}\n")
        return image_path


def _is_size_mismatch_error(raw_text: str, parsed_obj: dict | None = None) -> bool:
    s = (raw_text or "").lower()
    if "inpaint image must match the requested width and height" in s:
        return True
    obj = parsed_obj or {}
    msg = str(find_first_key(obj, ["error.message", "message", "error"]) or "").lower()
    return "requested width and height" in msg and "inpaint image" in msg


def _request_with_retry(method: str, url: str, *, on_text=None, op: str = "request", **kwargs):
    last_exc = None
    for i in range(1, _REQUEST_RETRY_MAX + 1):
        try:
            resp = requests.request(method, url, **kwargs)
            if resp.status_code in _HTTP_RETRYABLE_STATUS and i < _REQUEST_RETRY_MAX:
                if on_text:
                    on_text(f"{op} HTTP {resp.status_code}，重试 {i}/{_REQUEST_RETRY_MAX}\n")
                time.sleep(min(6, 1.5 * i))
                continue
            return resp
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exc = e
            if i < _REQUEST_RETRY_MAX:
                if on_text:
                    on_text(f"{op} 超时/网络波动，重试 {i}/{_REQUEST_RETRY_MAX}: {e}\n")
                time.sleep(min(6, 1.5 * i))
                continue
            raise
    if last_exc:
        raise last_exc
    raise RuntimeError(f"{op} failed without response")


def _build_create_args(cfg: BaoyouhuyuConfig):
    image_path = (cfg.image_path or "").strip()
    if not image_path:
        raise ValueError("baoyouhuyu_missing_input_reference")
    if image_path.lower().startswith(("http://", "https://")):
        raise ValueError("baoyouhuyu_input_reference_must_be_local_file")

    p = Path(image_path)
    if not p.exists() or not p.is_file():
        raise ValueError(f"baoyouhuyu_input_reference_not_found:{image_path}")

    model = (cfg.model or "").strip() or "sora-2-vip"
    seconds = (cfg.seconds or "").strip() or _infer_seconds(model, "8")
    prompt = (cfg.prompt or "").strip()

    data = {
        "model": model,
        "prompt": prompt,
        "seconds": seconds,
    }
    mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
    fh = p.open("rb")
    files = {"input_reference": (p.name, fh, mime)}
    return data, files, fh


def _parse_status_payload(obj: dict) -> tuple[str, float | None, str | None, str | None]:
    status = str(find_first_key(obj, ["status", "state"]) or "").strip()
    progress = find_first_key(obj, ["progress", "percent", "percentage", "data.progress"])
    maybe_url = _extract_video_url(obj)

    pval = None
    if isinstance(progress, (int, float)):
        pval = float(progress)
        if 0.0 <= pval <= 1.0:
            pval *= 100.0
    elif isinstance(progress, str):
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)", progress)
        if m:
            try:
                pval = float(m.group(1))
            except Exception:
                pval = None

    if pval is None:
        pval = parse_progress_percent(str(obj))

    err_obj = obj.get("error") if isinstance(obj, dict) else None
    err_msg = ""
    if isinstance(err_obj, dict):
        em = err_obj.get("message") or err_obj.get("code")
        err_msg = str(em).strip() if em else ""

    url = maybe_url.strip() if isinstance(maybe_url, str) and maybe_url.strip() else None
    return status, pval, url, (err_msg or None)


def _collect_strings(v, out: list[str]):
    if isinstance(v, str):
        s = v.strip()
        if s:
            out.append(s)
        return
    if isinstance(v, dict):
        for vv in v.values():
            _collect_strings(vv, out)
        return
    if isinstance(v, list):
        for it in v:
            _collect_strings(it, out)


def _extract_video_url(obj: dict) -> str | None:
    """
    Be tolerant to provider payload variants.
    """
    # Strict/high-confidence keys first
    candidates = [
        "video_url",
        "download_url",
        "result_url",
        "mp4_url",
        "content_url",
        "output_url",
        "play_url",
        "media_url",
        "file_url",
        "url",
    ]
    for k in candidates:
        v = find_first_key(obj, [k])
        if isinstance(v, str):
            s = v.strip()
            if not s:
                continue
            low = s.lower()
            if low.startswith("http://") or low.startswith("https://"):
                if (".mp4" in low) or ("/content" in low) or ("video" in low):
                    return s

    # Fallback: scan all strings and pick the most likely video URL
    bucket: list[str] = []
    _collect_strings(obj, bucket)
    scored: list[tuple[int, str]] = []
    for s in bucket:
        low = s.lower()
        if not (low.startswith("http://") or low.startswith("https://")):
            continue
        score = 0
        if ".mp4" in low:
            score += 5
        if "/content" in low:
            score += 4
        if "video" in low:
            score += 2
        if "download" in low:
            score += 2
        if score > 0:
            scored.append((score, s))
    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1]
    return None


def _brief_status_snapshot(obj: dict, limit: int = 360) -> str:
    try:
        s = str(obj).replace("\n", " ").replace("\r", " ")
        s = re.sub(r"\s+", " ", s).strip()
        if len(s) > limit:
            s = s[:limit] + "..."
        return s
    except Exception:
        return "<snapshot_failed>"


def run_baoyouhuyu_create_only(
    cfg: BaoyouhuyuConfig,
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
            "Accept": "application/json",
        }
        timeout = (cfg.connect_timeout, cfg.read_timeout)

        on_text(f"请求地址: {url}\n")
        on_text("提供方: BAOYOUHUYU 仅创建\n")
        on_text(f"模型: {cfg.model}\n")

        local_cfg = BaoyouhuyuConfig(**cfg.__dict__)

        size = _load_image_size(local_cfg.image_path)
        attempted = 0
        if size:
            cands = _candidate_sizes(local_cfg.model, size[0], size[1])
        else:
            cands = [(-1, -1)]  # no PIL / unreadable image -> one direct attempt

        r = None
        obj = {}
        for i, wh in enumerate(cands, start=1):
            attempted = i
            try_cfg = BaoyouhuyuConfig(**local_cfg.__dict__)
            if wh[0] > 0 and wh[1] > 0:
                try_cfg.image_path = _render_resized_copy(
                    try_cfg.image_path, try_cfg.model, wh, on_text=on_text
                )
            data, files, fh = _build_create_args(try_cfg)
            try:
                r = _request_with_retry(
                    "POST",
                    url,
                    on_text=on_text,
                    op="create",
                    headers=headers,
                    data=data,
                    files=files,
                    timeout=timeout,
                    verify=cfg.verify_ssl,
                )
            finally:
                fh.close()

            r.encoding = "utf-8"
            obj = safe_json_loads(r.text) or {}
            if r.status_code in (200, 201):
                break

            if _is_size_mismatch_error(r.text, obj) and i < len(cands):
                on_text(f"尺寸不匹配，自动尝试下一档尺寸（{i}/{len(cands)}）\n")
                continue
            break

        if r is None:
            on_error("create_http:no_response")
            return
        if r.status_code not in (200, 201):
            msg = _extract_error_message(obj, fallback=f"HTTP {r.status_code}")
            if attempted > 1:
                msg = f"{msg} | 已尝试尺寸档位={attempted}"
            on_error(f"create_http:{msg}")
            return

        rid = _extract_task_id(obj, r.text)
        if not rid:
            msg = _extract_error_message(obj, fallback="missing id")
            on_error(f"create_no_id:{msg}")
            return

        create_status = str(find_first_key(obj, ["status", "state"]) or "").strip().lower()
        if create_status and create_status not in (
            "queue",
            "queued",
            "pending",
            "processing",
            "running",
            "submitted",
        ):
            on_error(f"create_bad_status:{create_status}")
            return

        if create_status:
            on_text(f"创建状态={create_status}\n")
        on_text(f"创建成功 | remote_id={rid}\n")
        on_remote_id(rid)

    except Exception as e:
        on_error(str(e))


def run_baoyouhuyu_poll_existing(
    cfg: BaoyouhuyuConfig,
    task_id: str,
    on_text,
    on_done,
    on_error,
    stop_flag: threading.Event,
):
    try:
        headers = {"Authorization": f"Bearer {cfg.api_key.strip()}"}
        timeout = (cfg.connect_timeout, cfg.read_timeout)
        status_url = _status_url(cfg.base_url, task_id)

        done_keywords = {"succeeded", "success", "completed", "done", "finished"}
        fail_keywords = {"failed", "error", "rejected", "canceled", "cancelled"}

        last_status = None
        last_progress = None
        final_video_url = None

        on_text(f"状态地址: {status_url}\n")

        while True:
            if stop_flag.is_set():
                on_error("stopped")
                return

            try:
                r = _request_with_retry(
                    "GET",
                    status_url,
                    on_text=on_text,
                    op="poll",
                    headers=headers,
                    timeout=timeout,
                    verify=cfg.verify_ssl,
                )
            except requests.exceptions.Timeout as e:
                # timeout during poll is usually transient; keep task alive
                on_text(f"轮询超时，继续轮询: {e}\n")
                interval = max(1, int(getattr(cfg, "poll_interval_sec", 3) or 3))
                for _ in range(interval):
                    if stop_flag.is_set():
                        on_error("stopped")
                        return
                    time.sleep(1)
                continue
            except Exception as e:
                on_error(f"poll_net_err:{e}")
                return

            r.encoding = "utf-8"
            if r.status_code == 404:
                on_error("poll_http:HTTP 404 record_not_found")
                return
            if r.status_code != 200:
                obj = safe_json_loads(r.text) or {}
                msg = _extract_error_message(obj, fallback=f"HTTP {r.status_code}")
                on_error(f"poll_http:{msg}")
                return

            obj = safe_json_loads(r.text) or {}
            status, pval, maybe_url, err_msg = _parse_status_payload(obj)

            if status and status != last_status:
                on_text(f"> 状态: {status}\n")
                last_status = status
            if pval is not None and (last_progress is None or abs(pval - last_progress) >= 0.1):
                on_text(f"> 进度: {pval:.1f}%\n")
                last_progress = pval

            if maybe_url:
                final_video_url = maybe_url

            if err_msg:
                on_error(f"task_failed:{err_msg}")
                return

            status_l = status.lower() if status else ""
            if final_video_url or any(k in status_l for k in done_keywords) or obj.get("completed_at"):
                if not final_video_url:
                    final_video_url = _content_url(cfg.base_url, task_id)
                if not final_video_url:
                    on_text(f"> completed但未解析到视频链接 | snapshot={_brief_status_snapshot(obj)}\n")
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


def run_baoyouhuyu_create_and_poll(
    cfg: BaoyouhuyuConfig,
    on_text,
    on_remote_id,
    on_done,
    on_error,
    stop_flag: threading.Event,
):
    def _on_rid(rid: str):
        try:
            on_remote_id(rid)
        finally:
            run_baoyouhuyu_poll_existing(cfg, rid, on_text, on_done, on_error, stop_flag)

    run_baoyouhuyu_create_only(cfg, on_text, _on_rid, on_error, stop_flag)


def run_baoyouhuyu_poll_once(
    cfg: BaoyouhuyuConfig,
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

        headers = {"Authorization": f"Bearer {cfg.api_key.strip()}"}
        timeout = (cfg.connect_timeout, cfg.read_timeout)
        status_url = _status_url(cfg.base_url, task_id)

        r = _request_with_retry(
            "GET",
            status_url,
            on_text=on_text,
            op="poll_once",
            headers=headers,
            timeout=timeout,
            verify=cfg.verify_ssl,
        )
        r.encoding = "utf-8"
        if r.status_code == 404:
            on_error("poll_http:HTTP 404 record_not_found")
            return
        if r.status_code != 200:
            obj = safe_json_loads(r.text) or {}
            msg = _extract_error_message(obj, fallback=f"HTTP {r.status_code}")
            on_error(f"poll_http:{msg}")
            return

        obj = safe_json_loads(r.text) or {}
        status, pval, maybe_url, err_msg = _parse_status_payload(obj)
        if status:
            on_text(f"> 状态: {status}\n")
        if pval is not None:
            on_text(f"> 进度: {pval:.1f}%\n")

        if err_msg:
            on_error(f"task_failed:{err_msg}")
            return

        done_keywords = {"succeeded", "success", "completed", "done", "finished"}
        fail_keywords = {"failed", "error", "rejected", "canceled", "cancelled"}
        status_l = status.lower() if status else ""

        if maybe_url or any(k in status_l for k in done_keywords) or obj.get("completed_at"):
            final_url = maybe_url or _content_url(cfg.base_url, task_id)
            if not maybe_url:
                on_text(f"> completed但未解析到视频链接 | snapshot={_brief_status_snapshot(obj)}\n")
            on_done(final_url or None)
            return

        if any(k in status_l for k in fail_keywords):
            msg = _extract_error_message(obj, fallback=status or "failed")
            on_error(f"task_failed:{msg}")
            return
        return

    except requests.exceptions.Timeout as e:
        # single poll timeout should not flip task to failed
        on_text(f"单次轮询超时，稍后继续: {e}\n")
        return
    except Exception as e:
        on_error(f"poll_net_err:{e}")
