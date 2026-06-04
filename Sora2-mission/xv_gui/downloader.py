# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import re
import time
import json
import queue
import shutil
import hashlib
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Dict, Any, Tuple, List

import requests


# ----------------------------
# Fallback: calc_sha256
# ----------------------------
def _calc_sha256_fallback(fp: str, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(fp, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


try:
    # 你工程里如果 utils 有 calc_sha256 就用；没有也不炸
    from .utils import calc_sha256 as _calc_sha256  # type: ignore
except Exception:
    _calc_sha256 = _calc_sha256_fallback


def _safe_filename(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r'[\\/:*?"<>|]+', "_", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:180] if len(s) > 180 else s


def _ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)


def _download_stream(url: str, out_fp: Path, timeout=(10, 60), headers: dict | None = None) -> None:
    req_headers = {"User-Agent": "Mozilla/5.0"}
    if headers:
        req_headers.update(headers)
    with requests.get(url, stream=True, timeout=timeout, headers=req_headers) as r:
        r.raise_for_status()
        tmp_fp = out_fp.with_suffix(out_fp.suffix + ".part")
        with open(tmp_fp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
        tmp_fp.replace(out_fp)


def run_batch_download(
    root_tk,
    tasks: list,
    out_root: Path,
    index_path: Path,
    retries: int,
    skip_dup: bool,
    workers: int,
    stop_flag: threading.Event,
    log_cb: Callable[[str], None],
    ui_update_overall: Optional[Callable[[int, int], None]] = None,
    ui_update_item: Optional[Callable[[str, str], None]] = None,
    ui_done: Optional[Callable[[int, int, int], None]] = None,
    api_key: str = "",
):
    """
    ✅ 改造点：
    1) calc_sha256 缺失不再报错
    2) “异常但文件已落盘且size>0” → 判定成功，避免假失败
    3) 对 500/503 做指数退避
    """
    _ensure_dir(out_root)

    # --- build unique links ---
    items: list[Tuple[str, str, str, str, str, str, str, str, str]] = []
    # (task_id, url, batch_name, group, model, note, provider, remote_id, base_url)
    seen = set()
    for t in tasks:
        tid = getattr(t, "task_id", "")
        url = (getattr(t, "video_url", "") or "").strip()
        if not url:
            continue
        if skip_dup and url in seen:
            continue
        seen.add(url)
        items.append((
            tid,
            url,
            (getattr(t, "batch_name", "") or ""),
            (getattr(t, "group", "") or "Ungrouped"),
            (getattr(t, "model", "") or ""),
            (getattr(t, "note", "") or ""),
            (getattr(t, "provider", "") or ""),
            (getattr(t, "remote_id", "") or ""),
            (getattr(t, "base_url", "") or ""),
        ))

    total = len(items)
    if total <= 0:
        log_cb("ℹ️ 没有可下载链接。\n")
        if ui_done:
            ui_done(0, 0, 0)
        return

    log_cb(f"\n📥 并发批量下载开始：{total} 个不同链接 | workers={workers} | retries={retries} | skip={skip_dup}\n")

    # --- load index (for dedup) ---
    index: Dict[str, Any] = {}
    try:
        if index_path.exists():
            index = json.loads(index_path.read_text(encoding="utf-8", errors="ignore")) or {}
    except Exception:
        index = {}

    q: queue.Queue = queue.Queue()
    for it in items:
        q.put(it)

    lock = threading.Lock()
    done = 0
    ok = 0
    failed = 0
    skipped = 0

    def update_overall():
        if ui_update_overall:
            ui_update_overall(done, total)

    def mark_index(url: str, fp: str, sha: str):
        index[url] = {"file": fp, "sha256": sha, "ts": time.time()}
        try:
            index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def worker():
        nonlocal done, ok, failed, skipped
        while not stop_flag.is_set():
            try:
                tid, url, batch_name, group, model, note, provider, remote_id, base_url = q.get_nowait()
            except queue.Empty:
                return

            # out path
            batch_dir_name = _safe_filename(batch_name)
            group_name = _safe_filename(group)
            batch_root = out_root if not batch_dir_name else (out_root / batch_dir_name)
            gdir = batch_root if not group_name else (batch_root / group_name)
            _ensure_dir(gdir)

            rid_for_name = (remote_id or "").strip() or "no_remote_id"
            base = _safe_filename(f"{tid}_{rid_for_name}") + ".mp4"
            out_fp = gdir / base
            if out_fp.exists():
                stem = out_fp.stem
                suffix = out_fp.suffix
                n = 2
                while True:
                    cand = gdir / f"{stem}_{n}{suffix}"
                    if not cand.exists():
                        out_fp = cand
                        break
                    n += 1

            req_headers: dict[str, str] = {}
            dl_url = url
            pv = (provider or "").strip().lower()
            if pv == "baoyouhuyu":
                rid = (remote_id or "").strip()
                base = (base_url or "").strip().rstrip("/")
                if rid and (not dl_url or "/content" not in dl_url):
                    if base.lower().endswith("/v1/videos"):
                        dl_url = f"{base}/{rid}/content"
                    elif base.lower().endswith("/v1"):
                        dl_url = f"{base}/videos/{rid}/content"
                    else:
                        dl_url = f"{base}/v1/videos/{rid}/content"
                if (api_key or "").strip():
                    req_headers["Authorization"] = f"Bearer {api_key.strip()}"
                else:
                    with lock:
                        done += 1
                        failed += 1
                        log_cb(f"❌ [DL] {tid} 下载失败：baoyouhuyu 需要 Bearer token，但当前未提供。\n")
                        update_overall()
                    q.task_done()
                    continue

            # already downloaded
            dedupe_key = dl_url
            if skip_dup and dedupe_key in index:
                old = index.get(dedupe_key, {})
                old_fp = old.get("file", "")
                if old_fp and Path(old_fp).exists():
                    with lock:
                        done += 1
                        skipped += 1
                        log_cb(f"⏭️ [DL] {tid} 跳过重复：{old_fp}\n")
                        update_overall()
                    q.task_done()
                    continue

            # try download with retries
            last_err = None
            for attempt in range(1, retries + 2):
                if stop_flag.is_set():
                    break
                try:
                    log_cb(f"[DL] {tid}    🔁 尝试 {attempt}/{retries+1}\n")

                    _download_stream(dl_url, out_fp, timeout=(10, 90), headers=req_headers)

                    # hash (best-effort)
                    sha = ""
                    try:
                        sha = _calc_sha256(str(out_fp))
                    except Exception as e:
                        # 不要因为 hash 失败就判失败
                        sha = ""
                        log_cb(f"[DL] {tid}    ⚠️ sha256 计算失败（忽略）：{e}\n")

                    # 强成功：文件存在且>0
                    if out_fp.exists() and out_fp.stat().st_size > 0:
                        mark_index(dedupe_key, str(out_fp), sha)
                        with lock:
                            done += 1
                            ok += 1
                            log_cb(f"✅ [DL] {tid} 下载完成：{out_fp}\n")
                            update_overall()
                        last_err = None
                        break

                    raise RuntimeError("download wrote empty file")

                except Exception as e:
                    last_err = e
                    # 如果异常发生但文件已落盘（常见：hash/rename/偶发），也当成功
                    try:
                        if out_fp.exists() and out_fp.stat().st_size > 0:
                            sha = ""
                            try:
                                sha = _calc_sha256(str(out_fp))
                            except Exception:
                                sha = ""
                            mark_index(dedupe_key, str(out_fp), sha)
                            with lock:
                                done += 1
                                ok += 1
                                log_cb(f"✅ [DL] {tid} 已落盘（异常忽略，判定成功）：{out_fp}\n")
                                update_overall()
                            last_err = None
                            break
                    except Exception:
                        pass

                    msg = str(e)
                    log_cb(f"[DL] {tid}    ⚠️ 本次失败：{msg}\n")

                    # 指数退避：500/503 更友好
                    wait = 1.2 * (2 ** (attempt - 1))
                    if "500" in msg or "503" in msg:
                        wait = min(wait, 20.0)
                    else:
                        wait = min(wait, 10.0)
                    time.sleep(wait)

            if last_err is not None:
                with lock:
                    done += 1
                    failed += 1
                    log_cb(f"❌ [DL] {tid} 下载失败：{last_err}\n")
                    update_overall()

            q.task_done()

    threads = []
    for _ in range(max(1, int(workers or 1))):
        th = threading.Thread(target=worker, daemon=True)
        threads.append(th)
        th.start()

    for th in threads:
        th.join()

    if ui_done:
        ui_done(ok, skipped, failed)
