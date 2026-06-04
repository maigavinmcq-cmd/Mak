# downloader_core.py
import os
import re
import time
import requests
from http.client import IncompleteRead
from urllib.parse import urlparse, quote
from concurrent.futures import ThreadPoolExecutor, as_completed

from yt_dlp import YoutubeDL


# =========================
# 规则与常量（保持与你原项目一致）
# =========================

SHORT_URL_PATTERN = re.compile(
    r'https?://(?:sora\.chatgpt\.com|mcq\.com)/p/(s_[a-zA-Z0-9]+)'
)

REAL_VIDEO_PREFIX = "https://oscdn2.dyysy.com/MP4/"
REDIRECT_PREFIX_DEFAULT = "https://dyysy.com/"


# =========================
# 基础工具
# =========================

def is_tiktok_url(url: str) -> bool:
    return "tiktok.com/" in (url or "").lower()


def safe_filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    name = os.path.basename(parsed.path) or "video.mp4"
    for ch in r'\/:*?"<>|':
        name = name.replace(ch, "_")
    return name


def check_url_accessible(url: str, timeout: int = 10):
    """HTTP 预检查：仅对直链 mp4 做可用性检查"""
    try:
        resp = requests.get(url, stream=True, timeout=timeout)
        status = resp.status_code
        resp.close()
        if status == 200:
            return True, None
        return False, f"HTTP {status}"
    except Exception as e:
        return False, str(e)


def build_redirect_link(original_url: str, prefix: str = REDIRECT_PREFIX_DEFAULT) -> str:
    """
    https://dyysy.com/?url=<encoded_link>
    """
    u = (original_url or "").strip()
    if not u:
        return ""
    param = "?url=" + quote(u, safe="")
    if "?" in prefix:
        return prefix + "&" + param.lstrip("?")
    return prefix + param


def process_short_link(url: str) -> tuple[str, str | None]:
    """
    返回 (real_url, redirect_url_or_None)
    - sora/mcq 短链 => real mp4 + redirect 激活页
    - 其它 => 原样
    """
    clean = (url or "").strip()
    m = SHORT_URL_PATTERN.search(clean)
    if m:
        sora_id = m.group(1)
        real_mp4 = f"{REAL_VIDEO_PREFIX}{sora_id}.mp4"
        redirect = build_redirect_link(clean, REDIRECT_PREFIX_DEFAULT)
        return real_mp4, redirect
    return clean, None


# =========================
# 下载实现
# =========================

def download_video_http(
    url: str,
    outdir: str,
    progress_callback=None,        # progress_callback(pct:int)
    index: int | None = None,
    max_retries: int = 8
):
    """
    直链 mp4 下载（支持断点续传 .part + 重试）
    返回：None 表示成功；str 表示失败原因
    """
    os.makedirs(outdir, exist_ok=True)

    base_name = safe_filename_from_url(url)
    filename = f"{index:03d}_{base_name}" if index is not None else base_name

    file_path = os.path.join(outdir, filename)
    temp_path = file_path + ".part"

    base_headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        )
    }

    last_err = None
    session = requests.Session()

    def cb(p):
        if progress_callback:
            try:
                progress_callback(int(p))
            except Exception:
                pass

    for attempt in range(1, max_retries + 1):
        try:
            existing_size = os.path.getsize(temp_path) if os.path.exists(temp_path) else 0
            headers = dict(base_headers)
            if existing_size > 0:
                headers["Range"] = f"bytes={existing_size}-"

            resp = session.get(url, headers=headers, stream=True, timeout=(10, 120))
            resp.raise_for_status()

            total_size = None
            content_length = resp.headers.get("content-length")
            content_range = resp.headers.get("Content-Range")

            if content_range:
                try:
                    total_size = int(content_range.split("/")[-1])
                except Exception:
                    total_size = None
            elif content_length:
                try:
                    remaining = int(content_length)
                    total_size = existing_size + remaining
                except Exception:
                    total_size = None

            mode = "ab" if existing_size > 0 else "wb"
            downloaded = existing_size

            with open(temp_path, mode) as f:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size:
                        pct = int(downloaded * 100 / total_size)
                        pct = max(0, min(100, pct))
                        cb(pct)

            if total_size and downloaded < total_size:
                raise IncompleteRead(downloaded, total_size - downloaded)

            cb(100)

            if os.path.exists(file_path):
                os.remove(file_path)
            os.rename(temp_path, file_path)
            return None

        except (IncompleteRead,
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last_err = e
            time.sleep(2 * attempt)
            continue
        except Exception as e:
            last_err = e
            time.sleep(2 * attempt)
            continue

    if os.path.exists(temp_path):
        try:
            os.remove(temp_path)
        except OSError:
            pass

    return f"下载失败：{last_err}"


def extract_tiktok_vid(url: str) -> str:
    m = re.search(r'/video/(\d+)', url or "")
    return m.group(1) if m else "tiktok_video"


def download_tiktok(
    url: str,
    outdir: str,
    progress_callback=None,
    index: int | None = None,
    ffmpeg_path: str | None = None
):
    """
    TikTok 用 yt-dlp 下载
    返回：None 成功；str 失败原因
    """
    os.makedirs(outdir, exist_ok=True)
    vid = extract_tiktok_vid(url)
    filename = f"{index:03d}_{vid}.mp4" if index is not None else f"{vid}.mp4"
    outtmpl = os.path.join(outdir, filename)

    def cb(p):
        if progress_callback:
            try:
                progress_callback(int(p))
            except Exception:
                pass

    ydl_opts = {
        "outtmpl": outtmpl,
        "format": "mp4/best",
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "ffmpeg_location": os.path.dirname(ffmpeg_path) if ffmpeg_path else None,
    }

    try:
        cb(5)
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        cb(100)
        return None
    except Exception as e:
        return f"TikTok 下载失败：{e}"


# =========================
# Headless 激活（把“怎么激活”抽成注入函数）
# =========================

def noop_activate_redirects(redirect_urls: list[str], concurrency: int = 3, **kwargs) -> list[str]:
    """
    默认不做激活：返回全部失败（或你也可以返回 [] 表示忽略激活）
    新项目里建议实现真正的 Playwright 激活，然后注入进来。
    """
    return redirect_urls[:]  # 默认：都算失败（更安全、更符合你原逻辑）


# =========================
# 核心业务编排：从开始到下载
# =========================

def prepare_urls_with_optional_headless(
    orig_urls: list[str],
    enable_headless: bool,
    activate_redirects_fn,                  # (redirect_urls, concurrency, **kwargs)-> failed_redirects
    workers_for_headless: int = 3,
    headless_kwargs: dict | None = None
) -> tuple[list[str], set[str]]:
    """
    1) short link => real mp4 + redirect url
    2) tiktok 不需要激活
    3) enable_headless 时：批量激活 redirect_urls；把激活失败映射为 failed_real_urls
    返回：real_urls, failed_real_set
    """
    headless_kwargs = headless_kwargs or {}

    real_urls: list[str] = []
    redirect_urls: list[str] = []
    mapping_real_from_redirect: dict[str, str] = {}

    for u in orig_urls:
        if is_tiktok_url(u):
            real_urls.append(u)
            continue

        real, redirect = process_short_link(u)
        real_urls.append(real)
        if redirect:
            redirect_urls.append(redirect)
            mapping_real_from_redirect[redirect] = real

    failed_real: set[str] = set()

    if enable_headless and redirect_urls:
        # 去重保序
        redirect_urls = list(dict.fromkeys(redirect_urls))
        failed_redirects = activate_redirects_fn(
            redirect_urls,
            concurrency=max(1, min(int(workers_for_headless), 6)),
            **headless_kwargs
        )
        for r in failed_redirects:
            rr = mapping_real_from_redirect.get(r)
            if rr:
                failed_real.add(rr)

    return real_urls, failed_real


def run_download_pipeline(
    orig_urls: list[str],
    outdir: str,
    *,
    workers: int = 3,
    enable_precheck: bool = False,
    enable_headless: bool = True,
    activate_redirects_fn=noop_activate_redirects,
    headless_kwargs: dict | None = None,
    ffmpeg_path: str | None = None,
    # 回调（新项目可对接 UI / CLI）
    on_log=None,                    # on_log(str)
    on_overall_progress=None,        # on_overall_progress(done:int, total:int)
    on_current_progress=None,        # on_current_progress(pct:int)
):
    """
    这是你要的“从开始到下载”的完整业务流程（无 UI）：
    - 去重
    - 解析短链 + 可选 headless 激活
    - 可选预检查（直链 mp4 才 check；tiktok 跳过）
    - 并发下载（tiktok 用 yt-dlp；mp4 用 http）
    返回：result dict（成功/失败/跳过等统计 + 每条结果）
    """

    def log(msg: str):
        if on_log:
            try:
                on_log(msg)
            except Exception:
                pass

    # 0) 去重（保序）
    seen = set()
    deduped = []
    for u in orig_urls:
        u = (u or "").strip()
        if not u:
            continue
        if u not in seen:
            seen.add(u)
            deduped.append(u)

    if not deduped:
        return {
            "total_input": 0,
            "total_effective": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "items": [],
        }

    log(f"输入链接：{len(orig_urls)}，去重后：{len(deduped)}")

    # 1) 解析 + 激活
    real_urls, failed_real = prepare_urls_with_optional_headless(
        deduped,
        enable_headless=enable_headless,
        activate_redirects_fn=activate_redirects_fn,
        workers_for_headless=min(workers, 3),
        headless_kwargs=headless_kwargs or {},
    )

    # 2) 过滤激活失败
    filtered_urls = []
    items = []
    for u in real_urls:
        if u in failed_real:
            items.append({"url": u, "status": "failed", "reason": "Headless 激活失败"})
        else:
            filtered_urls.append(u)

    if not filtered_urls:
        return {
            "total_input": len(orig_urls),
            "total_effective": 0,
            "success": 0,
            "failed": len(items),
            "skipped": 0,
            "items": items,
        }

    # 3) 预检查
    valid_urls = []
    if enable_precheck:
        log(f"预检查启用：开始检查 {len(filtered_urls)} 条...")
        for u in filtered_urls:
            if is_tiktok_url(u):
                valid_urls.append(u)
                items.append({"url": u, "status": "skipped_precheck"})
                continue
            ok, err = check_url_accessible(u)
            if ok:
                valid_urls.append(u)
                items.append({"url": u, "status": "precheck_ok"})
            else:
                items.append({"url": u, "status": "failed", "reason": f"不可用：{err}"})
        log(f"预检查结束：可下载 {len(valid_urls)} 条")
    else:
        valid_urls = filtered_urls

    if not valid_urls:
        return {
            "total_input": len(orig_urls),
            "total_effective": 0,
            "success": 0,
            "failed": sum(1 for it in items if it["status"] == "failed"),
            "skipped": sum(1 for it in items if it["status"].startswith("skipped")),
            "items": items,
        }

    # 4) 并发下载
    total = len(valid_urls)
    done = 0
    success = 0
    failed = sum(1 for it in items if it["status"] == "failed")

    def report_overall():
        if on_overall_progress:
            try:
                on_overall_progress(done, total)
            except Exception:
                pass

    def download_one(url: str, index: int):
        if is_tiktok_url(url):
            err = download_tiktok(
                url, outdir,
                progress_callback=on_current_progress,
                index=index,
                ffmpeg_path=ffmpeg_path
            )
        else:
            err = download_video_http(
                url, outdir,
                progress_callback=on_current_progress,
                index=index
            )
        return err

    workers = max(1, min(int(workers), 16))
    log(f"开始下载：{total} 条，并发={workers}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = []
        for idx, url in enumerate(valid_urls, start=1):
            futures.append((url, idx, pool.submit(download_one, url, idx)))

        for url, idx, fut in futures:
            err = fut.result()
            done += 1
            report_overall()

            if err:
                failed += 1
                items.append({"url": url, "status": "failed", "reason": err, "index": idx})
            else:
                success += 1
                items.append({"url": url, "status": "success", "index": idx})

    log(f"全部完成：成功={success}，失败={failed}")
    return {
        "total_input": len(orig_urls),
        "total_effective": total,
        "success": success,
        "failed": failed,
        "skipped": sum(1 for it in items if it["status"].startswith("skipped")),
        "items": items,
    }
