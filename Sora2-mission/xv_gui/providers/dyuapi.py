# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
import json
import tempfile
import threading
import time
from pathlib import Path

import requests
from PIL import Image, ImageOps

from ..utils import find_first_key, guess_mime, parse_progress_percent, safe_json_loads

ALLOWED_IMAGE_SIZES = {(1280, 720), (720, 1280)}


@dataclass
class DyuapiConfig:
    api_key: str
    base_url: str
    model: str
    prompt: str
    image_path: str
    connect_timeout: int = 30
    read_timeout: int = 900
    verify_ssl: bool = True
    poll_interval_sec: int = 8
    size: str | None = None
    seconds: str | None = None
    n: str | None = "1"


def _videos_url(base_url: str) -> str:
    b = (base_url or "").strip().rstrip("/")
    low = b.lower()
    if low.endswith("/v1/videos"):
        return b
    if low.endswith("/v1"):
        return f"{b}/videos"
    return f"{b}/v1/videos"


def _status_url(base_url: str, video_id: str) -> str:
    rid = (video_id or "").strip().strip("/")
    return f"{_videos_url(base_url)}/{rid}"


def _content_url(base_url: str, video_id: str) -> str:
    return f"{_status_url(base_url, video_id)}/content"


def _auth_headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key.strip()}", "Accept": "application/json"}


def _try_parse_nested_json(value):
    cur = value
    for _ in range(3):
        if isinstance(cur, dict):
            return cur
        if not isinstance(cur, str):
            return value if isinstance(value, dict) else {}
        text = cur.strip()
        if not text or text[0] not in "{[":
            return value if isinstance(value, dict) else {}
        parsed = safe_json_loads(text)
        if parsed is None:
            try:
                parsed = json.loads(text)
            except Exception:
                return value if isinstance(value, dict) else {}
        cur = parsed
    return cur if isinstance(cur, dict) else {}


def _unwrap_error_obj(obj: dict) -> dict:
    if not isinstance(obj, dict):
        return {}
    err = obj.get("error")
    if isinstance(err, dict):
        return err
    if isinstance(err, str):
        parsed = _try_parse_nested_json(err)
        if isinstance(parsed, dict) and parsed:
            if isinstance(parsed.get("error"), dict):
                return parsed["error"]
            return parsed
    msg = obj.get("message")
    if isinstance(msg, str):
        parsed = _try_parse_nested_json(msg)
        if isinstance(parsed, dict) and parsed:
            if isinstance(parsed.get("error"), dict):
                return parsed["error"]
            return parsed
    return {}


def _extract_error_message(obj: dict, fallback: str = "") -> str:
    err_obj = _unwrap_error_obj(obj)
    if err_obj:
        msg = str(
            err_obj.get("message")
            or err_obj.get("detail")
            or err_obj.get("reason")
            or err_obj.get("error_message")
            or fallback
            or "failed"
        ).strip()
        err_type = str(err_obj.get("type") or "").strip()
        err_code = str(err_obj.get("code") or "").strip()
        extras = " / ".join(x for x in (err_type, err_code) if x)
        if extras:
            return f"{msg} ({extras})"
        return msg

    msg = find_first_key(
        obj,
        ["message", "msg", "error", "detail", "reason", "error_message", "error.code", "code"],
    ) or fallback or "failed"
    s = str(msg).replace("\n", " ").replace("\r", " ").strip()
    return s[:200] + "..." if len(s) > 200 else s


def _extract_task_id(obj: dict, raw_text: str = "") -> str:
    tid = (
        find_first_key(obj, ["id", "video_id", "task_id", "data.id", "data.video_id", "data.task_id"])
        or ""
    )
    return (tid or "").strip()


def _extract_video_url(obj: dict) -> str:
    v = find_first_key(
        obj,
        [
            "output_url",
            "video_url",
            "url",
            "result_url",
            "download_url",
            "mp4_url",
            "data.output_url",
            "data.video_url",
            "data.url",
        ],
    ) or ""
    return (v or "").strip() if isinstance(v, str) else ""


def _has_api_error(obj: dict) -> bool:
    if not isinstance(obj, dict) or not obj:
        return False
    if _unwrap_error_obj(obj):
        return True
    err = obj.get("error")
    if isinstance(err, str) and err.strip():
        return True
    code = str(find_first_key(obj, ["error.code", "code", "type"]) or "").strip().lower()
    return code in {"invalid_request", "invalid_request_error", "error"}


def _pick_target_size(width: int, height: int) -> tuple[int, int]:
    if width > height:
        return 1280, 720
    if width < height:
        return 720, 1280
    return 1280, 720


def _prepare_reference_image(cfg: DyuapiConfig) -> tuple[Path, str, str | None]:
    image_path = Path(cfg.image_path)
    with Image.open(image_path) as img:
        src_w, src_h = img.size
        if (src_w, src_h) in ALLOWED_IMAGE_SIZES:
            return image_path, f"{src_w}x{src_h}", None

        target_w, target_h = _pick_target_size(src_w, src_h)
        fitted = ImageOps.contain(img.convert("RGB"), (target_w, target_h), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (target_w, target_h), (0, 0, 0))
        offset_x = (target_w - fitted.size[0]) // 2
        offset_y = (target_h - fitted.size[1]) // 2
        canvas.paste(fitted, (offset_x, offset_y))

        tmp = tempfile.NamedTemporaryFile(prefix="hellobabygo_", suffix=".png", delete=False)
        tmp_path = Path(tmp.name)
        tmp.close()
        canvas.save(tmp_path, format="PNG")
        note = (
            f"图片尺寸自动处理: {src_w}x{src_h} -> {target_w}x{target_h} "
            f"(等比缩放并补边，不修改原图)"
        )
        return tmp_path, f"{target_w}x{target_h}", note


def _build_data(cfg: DyuapiConfig, *, resolved_size: str) -> dict:
    return {
        "prompt": (cfg.prompt or "").strip(),
        "model": (cfg.model or "").strip(),
        "size": resolved_size.strip(),
        "seconds": str((cfg.seconds or "4")).strip(),
        "n": str((cfg.n or "1")).strip(),
    }


def _fetch_content_url(cfg: DyuapiConfig, task_id: str) -> str:
    headers = _auth_headers(cfg.api_key)
    timeout = (cfg.connect_timeout, cfg.read_timeout)
    r = requests.get(_content_url(cfg.base_url, task_id), headers=headers, timeout=timeout, verify=cfg.verify_ssl)
    r.encoding = "utf-8"
    if r.status_code != 200:
        return ""
    obj = safe_json_loads(r.text) or {}
    if _has_api_error(obj):
        return ""
    raw = (r.text or "").strip()
    if raw.startswith(("http://", "https://")):
        return raw
    return _extract_video_url(obj)


def run_dyuapi_create_only(
    cfg: DyuapiConfig,
    on_text,
    on_remote_id,
    on_error,
    stop_flag: threading.Event,
):
    fp = None
    temp_image_path: Path | None = None
    try:
        if stop_flag.is_set():
            on_error("stopped")
            return

        url = _videos_url(cfg.base_url)
        headers = _auth_headers(cfg.api_key)
        timeout = (cfg.connect_timeout, cfg.read_timeout)
        original_image_path = Path(cfg.image_path)
        if not original_image_path.exists() or not original_image_path.is_file():
            on_error(f"dyuapi_image_not_found:{original_image_path}")
            return

        prepared_image_path, resolved_size, image_note = _prepare_reference_image(cfg)
        if prepared_image_path != original_image_path:
            temp_image_path = prepared_image_path
        if image_note:
            on_text(f"{image_note}\n")

        data = _build_data(cfg, resolved_size=resolved_size)
        on_text(f"请求地址: {url}\n")
        on_text("提供方: HELLOBABYGO create_only\n")
        on_text(f"模型: {cfg.model}\n")
        on_text(f"尺寸: {data['size']} | 时长: {data['seconds']}s | 数量: {data['n']}\n")

        fp = open(prepared_image_path, "rb")
        files = {
            "input_reference": (prepared_image_path.name, fp, guess_mime(str(prepared_image_path))),
        }
        r = requests.post(url, headers=headers, data=data, files=files, timeout=timeout, verify=cfg.verify_ssl)
        r.encoding = "utf-8"
        obj = safe_json_loads(r.text) or {}
        if r.status_code not in (200, 201) or _has_api_error(obj):
            msg = _extract_error_message(obj, fallback=f"HTTP {r.status_code}")
            on_error(f"create_http:{msg}")
            return
        rid = _extract_task_id(obj, r.text)
        if not rid:
            msg = _extract_error_message(obj, fallback="missing id")
            on_error(f"create_no_id:{msg}")
            return
        on_text(f"创建成功 | remote_id={rid}\n")
        on_remote_id(rid)
    except Exception as e:
        on_error(str(e))
    finally:
        try:
            if fp:
                fp.close()
        except Exception:
            pass
        try:
            if temp_image_path and temp_image_path.exists():
                temp_image_path.unlink()
        except Exception:
            pass


def run_dyuapi_poll_existing(
    cfg: DyuapiConfig,
    task_id: str,
    on_text,
    on_done,
    on_error,
    stop_flag: threading.Event,
):
    try:
        headers = _auth_headers(cfg.api_key)
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
            obj = safe_json_loads(r.text) or {}
            if r.status_code != 200 or _has_api_error(obj):
                msg = _extract_error_message(obj, fallback=f"HTTP {r.status_code}")
                on_error(f"poll_http:{msg}")
                return

            status = (find_first_key(obj, ["status", "state", "data.status", "data.state"]) or "").strip()
            progress = find_first_key(obj, ["progress", "percent", "percentage", "data.progress"])
            video_url = _extract_video_url(obj)

            pval = None
            if isinstance(progress, (int, float)):
                pval = float(progress)
                if 0 <= pval <= 1.0:
                    pval *= 100.0
            elif isinstance(progress, str):
                try:
                    pval = float(progress.strip().rstrip("%"))
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
                if not video_url:
                    on_text("> 查询接口未返回下载地址，尝试 /content 兜底\n")
                    video_url = _fetch_content_url(cfg, task_id)
                if video_url:
                    on_done(video_url)
                    return
                on_error("completed_but_no_video_url")
                return
            if any(k in status_l for k in fail_keywords):
                msg = _extract_error_message(obj, fallback=status or "failed")
                on_error(f"task_failed:{msg}")
                return

            interval = max(1, int(getattr(cfg, "poll_interval_sec", 8) or 8))
            for _ in range(interval):
                if stop_flag.is_set():
                    on_error("stopped")
                    return
                time.sleep(1)
    except Exception as e:
        on_error(str(e))


def run_dyuapi_create_and_poll(
    cfg: DyuapiConfig,
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
        run_dyuapi_poll_existing(cfg, rid, on_text, on_done, on_error, stop_flag)

    run_dyuapi_create_only(cfg, on_text, _rid_cb, on_error, stop_flag)


def run_dyuapi_poll_once(
    cfg: DyuapiConfig,
    task_id: str,
    on_text,
    on_done,
    on_error,
    stop_flag: threading.Event | None = None,
):
    evt = stop_flag or threading.Event()
    run_dyuapi_poll_existing(cfg, task_id, on_text, on_done, on_error, evt)
