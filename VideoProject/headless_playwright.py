# headless_playwright.py
import os
import sys
import asyncio
from dataclasses import dataclass
from typing import Callable, Optional, List, Tuple


@dataclass
class HeadlessConfig:
    concurrency: int = 3
    wait_ms: int = 3000
    timeout_ms: int = 20000
    delay_s: float = 0.35

    # Playwright Chromium launch timeout（只管 launch 本身）
    launch_timeout_s: int = 20

    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    )
    viewport: dict = None  # e.g. {"width": 1280, "height": 720}

    # 打包后浏览器路径（ms-playwright / _internal/ms-playwright）
    browsers_path: Optional[str] = None

    # chromium launch args
    launch_args: List[str] = None

    def normalize(self) -> "HeadlessConfig":
        c = HeadlessConfig(**self.__dict__)

        # 合理范围（跟你原来一致）
        c.concurrency = max(1, min(int(c.concurrency), 6))
        c.wait_ms = max(0, min(int(c.wait_ms), 60000))
        c.timeout_ms = max(5000, min(int(c.timeout_ms), 120000))
        c.delay_s = max(0.0, min(float(c.delay_s), 5.0))
        c.launch_timeout_s = max(5, min(int(c.launch_timeout_s), 60))

        if c.viewport is None:
            c.viewport = {"width": 1280, "height": 720}

        if c.launch_args is None:
            c.launch_args = [
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-dev-shm-usage",
                "--disable-features=site-per-process",
            ]
        return c


def _default_logger(msg: str) -> None:
    print(msg)


def app_dir() -> str:
    """exe 所在目录 / 脚本目录"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def ensure_playwright_browsers_path(
    base_dir: Optional[str] = None
) -> Tuple[str, bool, str]:
    """
    复刻你原来的探测逻辑：
    - onedir: _internal/ms-playwright
    - 或者同级 ms-playwright
    返回 (path, ok, why)
    """
    base = base_dir or app_dir()
    cand1 = os.path.join(base, "_internal", "ms-playwright")
    cand2 = os.path.join(base, "ms-playwright")
    bundled = cand1 if os.path.isdir(cand1) else cand2

    # 设置环境变量（Playwright 会优先读）
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = bundled
    os.environ["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"

    if not os.path.isdir(bundled):
        return bundled, False, "ms-playwright 目录不存在（打包时未携带浏览器）"

    has_chromium = False
    try:
        for name in os.listdir(bundled):
            if name.startswith("chromium"):
                has_chromium = True
                break
    except Exception:
        pass

    if not has_chromium:
        return bundled, False, "ms-playwright 内未发现 chromium（可能被杀软删除/打包不完整）"

    return bundled, True, "OK"


async def _activate_redirects_async(
    redirect_urls: List[str],
    cfg: HeadlessConfig,
    logger: Callable[[str], None],
) -> List[str]:
    """
    纯 async：返回 failed_redirect_urls
    """
    if not redirect_urls:
        return []

    cfg = cfg.normalize()

    # browsers_path：优先用 cfg.browsers_path，否则自动探测
    if cfg.browsers_path:
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = cfg.browsers_path
        os.environ["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"
        logger(f"Playwright browsers path = {cfg.browsers_path} (from config)")
    else:
        path, ok, why = ensure_playwright_browsers_path()
        logger(f"Playwright browsers path = {path} ({why})")
        if not ok:
            # 直接全部失败（保持你原逻辑的安全性）
            return redirect_urls[:]

    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        logger(f"❌ 缺少 playwright.async_api：{e}")
        return redirect_urls[:]

    sem = asyncio.Semaphore(cfg.concurrency)
    failed: List[str] = []

    async def safe_launch(p):
        # 只限制 launch 时间
        return await asyncio.wait_for(
            p.chromium.launch(
                headless=True,
                args=cfg.launch_args,
            ),
            timeout=cfg.launch_timeout_s,
        )

    async with async_playwright() as p:
        browser = await safe_launch(p)

        async def activate_one(url_item: str, idx: int):
            async with sem:
                context = None
                try:
                    context = await browser.new_context(
                        user_agent=cfg.user_agent,
                        viewport=cfg.viewport,
                    )
                    page = await context.new_page()
                    page.set_default_timeout(cfg.timeout_ms)

                    logger(f"[Headless激活 {idx}/{len(redirect_urls)}] {url_item}")
                    await page.goto(url_item, wait_until="domcontentloaded")
                    await page.wait_for_timeout(cfg.wait_ms)

                    # 你原来这里打印 FINAL: <hidden>，我保留不暴露真实 final URL
                    logger("  -> FINAL: <hidden>")
                    await asyncio.sleep(cfg.delay_s)
                    return True
                except Exception as e:
                    logger(f"❌ [Headless FAIL] {url_item} | {e}")
                    failed.append(url_item)
                    return False
                finally:
                    if context:
                        try:
                            await context.close()
                        except Exception:
                            pass

        tasks = [activate_one(u, i + 1) for i, u in enumerate(redirect_urls)]
        await asyncio.gather(*tasks)

        await browser.close()

    return failed


def activate_redirects_playwright(
    redirect_urls: List[str],
    *,
    concurrency: int = 3,
    wait_ms: int = 3000,
    timeout_ms: int = 20000,
    delay_s: float = 0.35,
    browsers_path: Optional[str] = None,
    logger: Optional[Callable[[str], None]] = None,
) -> List[str]:
    """
    ✅ 你业务层调用的“同步函数”：
      failed = activate_redirects_playwright([...], concurrency=3, ...)
    内部用 asyncio.run 执行 async 版本，并返回失败列表。

    注意：如果你将来在“已有 event loop”的 async 环境（如 FastAPI）里用，
    请改用 _activate_redirects_async 或者提供 async 包装。
    """
    lg = logger or _default_logger

    cfg = HeadlessConfig(
        concurrency=concurrency,
        wait_ms=wait_ms,
        timeout_ms=timeout_ms,
        delay_s=delay_s,
        browsers_path=browsers_path,
    ).normalize()

    try:
        return asyncio.run(_activate_redirects_async(redirect_urls, cfg, lg))
    except RuntimeError:
        # 兼容：某些环境 event loop 已存在
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(_activate_redirects_async(redirect_urls, cfg, lg))
        finally:
            try:
                loop.close()
            except Exception:
                pass
