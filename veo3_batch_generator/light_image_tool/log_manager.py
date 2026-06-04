from __future__ import annotations

import json
import re
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Any

from light_image_tool.config import PROJECT_ROOT
from light_image_tool.models import LightImageTaskLogEntry, now_text


SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(api[_-]?key['\"]?\s*[:=]\s*['\"]?)([^'\"\s,}]+)", re.IGNORECASE),
    re.compile(r"(Authorization['\"]?\s*[:=]\s*['\"]?Bearer\s+)([^'\"\s,}]+)", re.IGNORECASE),
    re.compile(r"((?:OSSAccessKeyId|Signature)=)([^&\s\"'}]+)", re.IGNORECASE),
]


def redact_secrets(text: Any) -> str:
    value = str(text or "")
    for pattern in SECRET_PATTERNS:
        if pattern.pattern.startswith("(api") or pattern.pattern.startswith("(Authorization"):
            value = pattern.sub(r"\1****", value)
        elif "OSSAccessKeyId" in pattern.pattern:
            value = pattern.sub(r"\1****", value)
        else:
            value = pattern.sub("sk-****", value)
    return value


class LightImageLogManager:
    def __init__(self, queue_id: str, root: str | Path | None = None) -> None:
        base = Path(root) if root else PROJECT_ROOT / "logs" / "light_image_tool"
        self.root = base
        self.queue_dir = base / "queues" / queue_id
        self.exports_dir = self.queue_dir / "exports"
        self.run_log_path = self.queue_dir / "run.log"
        self.diagnostics_log_path = self.queue_dir / "diagnostics.log"
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        level: str,
        category: str,
        message: str,
        *,
        task_uid: str | None = None,
        detail: str | None = None,
        diagnostics: bool = False,
    ) -> LightImageTaskLogEntry:
        entry = LightImageTaskLogEntry(
            time=now_text(),
            level=level,
            category=category,
            task_uid=task_uid,
            message=redact_secrets(message),
            detail=redact_secrets(detail) if detail else None,
        )
        self._append(self.diagnostics_log_path if diagnostics else self.run_log_path, asdict(entry))
        return entry

    def exception(self, category: str, message: str, exc: BaseException, task_uid: str | None = None) -> None:
        self.log(
            "ERROR",
            category,
            message,
            task_uid=task_uid,
            detail=traceback.format_exc(),
            diagnostics=True,
        )
        self.log("ERROR", category, f"{message}：{exc}", task_uid=task_uid)

    @staticmethod
    def _append(path: Path, payload: dict[str, Any]) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:
            pass
