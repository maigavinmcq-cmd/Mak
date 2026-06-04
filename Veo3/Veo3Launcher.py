from __future__ import annotations

import argparse
import logging
import os
import socket
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

from streamlit.web import bootstrap


def _resource_dir() -> Path:
    if getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parents[1]


def _runtime_dir() -> Path:
    if getattr(sys, "frozen", False):
        local_appdata = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        runtime_dir = local_appdata / "Veo3Portable"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        return runtime_dir
    return Path(__file__).resolve().parent


def _setup_logging() -> None:
    runtime_dir = _runtime_dir()
    log_file = runtime_dir / "launcher.log"
    logging.basicConfig(
        filename=str(log_file),
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        encoding="utf-8",
    )


def _pick_port(host: str = "127.0.0.1", start: int = 8501, end: int = 8510) -> int:
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError("no_free_local_port")


def _wait_for_port(host: str, port: int, timeout_seconds: int = 30) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            try:
                sock.connect((host, port))
                return True
            except OSError:
                time.sleep(0.4)
    return False


def _wait_for_streamlit_ready(host: str, port: int, timeout_seconds: int = 45) -> bool:
    deadline = time.time() + timeout_seconds
    probe_urls = [
        f"http://{host}:{port}/_stcore/health",
        f"http://{host}:{port}/",
    ]
    while time.time() < deadline:
        for url in probe_urls:
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    status_code = getattr(response, "status", response.getcode())
                    if 200 <= status_code < 400:
                        logging.info("streamlit_ready url=%s status=%s", url, status_code)
                        return True
            except urllib.error.HTTPError as exc:
                logging.info("streamlit_probe_http_error url=%s status=%s", url, exc.code)
            except Exception:
                pass
        time.sleep(0.5)
    return False


def _terminate_process(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def _show_error(message: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Veo3 启动失败", message)
        root.destroy()
    except Exception:
        logging.exception("show_error_dialog_failed")


def _build_server_command(host: str, port: int) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--serve", "--host", host, "--port", str(port)]
    return [sys.executable, str(Path(__file__).resolve()), "--serve", "--host", host, "--port", str(port)]


def run_server(host: str, port: int) -> None:
    _setup_logging()
    app_path = _resource_dir() / "Veo3" / "Veo3Generated.py"
    if not app_path.exists():
        raise FileNotFoundError(f"streamlit_app_not_found:{app_path}")

    logging.info("server_start host=%s port=%s app=%s", host, port, app_path)
    sys.argv = ["streamlit", "run", str(app_path)]
    bootstrap.run(
        str(app_path),
        False,
        [],
        {
            "server.address": host,
            "server.port": port,
            "server.headless": True,
            "browser.gatherUsageStats": False,
            "global.developmentMode": False,
            "server.fileWatcherType": "none",
        },
    )


def run_desktop_window() -> None:
    _setup_logging()
    host = "127.0.0.1"
    port = _pick_port(host=host)
    command = _build_server_command(host, port)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(command, cwd=str(_runtime_dir()), creationflags=creationflags)
    logging.info("desktop_mode_start host=%s port=%s cmd=%s pid=%s", host, port, command, process.pid)

    if not _wait_for_port(host, port, timeout_seconds=30):
        _terminate_process(process)
        message = f"本地服务启动失败，请检查日志：{_runtime_dir() / 'launcher_error.log'}"
        logging.error("server_not_ready host=%s port=%s", host, port)
        _show_error(message)
        raise RuntimeError("embedded_server_start_failed")

    if not _wait_for_streamlit_ready(host, port, timeout_seconds=45):
        _terminate_process(process)
        message = f"Veo3 页面初始化失败，请检查日志：{_runtime_dir() / 'launcher.log'}"
        logging.error("streamlit_http_not_ready host=%s port=%s", host, port)
        _show_error(message)
        raise RuntimeError("streamlit_http_not_ready")

    from PySide6.QtCore import QUrl
    from PySide6.QtWidgets import QApplication
    from PySide6.QtWebEngineWidgets import QWebEngineView

    app = QApplication.instance() or QApplication(sys.argv)
    view = QWebEngineView()
    view.setWindowTitle("Veo3 Portable")
    view.resize(1440, 960)
    view.setMinimumSize(1100, 760)
    view.load(QUrl(f"http://{host}:{port}/"))
    view.show()

    try:
        app.exec()
    finally:
        _terminate_process(process)


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8501)
    args, _ = parser.parse_known_args()

    if args.serve:
        run_server(args.host, args.port)
    else:
        run_desktop_window()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        runtime_dir = _runtime_dir()
        error_file = runtime_dir / "launcher_error.log"
        error_file.write_text(traceback.format_exc(), encoding="utf-8")
        raise
