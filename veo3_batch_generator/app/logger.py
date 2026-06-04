from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path


def _project_output_logs_dir(output_dir: str | Path) -> Path:
    base = Path(output_dir)
    # Runtime output_dir is normally: <software_log_root>/<yyyy-mm-dd>.
    # Keep fallback logs beside the configured netdisk log root instead of
    # writing large rolling logs into the project workspace.
    return base.parent / "project_outputs" / "logs"


def setup_logger(output_dir: str | Path, log_path: str | Path | None = None) -> tuple[logging.Logger, Path]:
    if log_path:
        target_log_path = Path(log_path)
        try:
            target_log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            target_log_path = _project_output_logs_dir(output_dir) / target_log_path.name
            target_log_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        logs_dir = Path(output_dir) / "logs"
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logs_dir = _project_output_logs_dir(output_dir)
            logs_dir.mkdir(parents=True, exist_ok=True)
        target_log_path = logs_dir / f"run_{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.log"

    logger = logging.getLogger("veo3_batch_generator")
    logger.setLevel(logging.INFO)
    for handler in logger.handlers:
        try:
            handler.close()
        except Exception:
            pass
    logger.handlers.clear()

    formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = logging.FileHandler(target_log_path, mode="a", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger, target_log_path
