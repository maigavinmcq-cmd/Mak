# -*- coding: utf-8 -*-
from __future__ import annotations

import mimetypes
import re


def normalize_base_url(base_url: str) -> str:
    return (base_url or "").strip().rstrip("/")


def safe_json(resp) -> dict:
    try:
        return resp.json()
    except Exception:
        return {}


def find_first_key(obj: dict, keys: list[str]):
    for key in keys:
        cur = obj
        ok = True
        for part in key.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok:
            return cur
    return None


def parse_progress_percent(text: str) -> float | None:
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*%", text or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


def extract_error_message(obj: dict, fallback: str = "") -> str:
    msg = find_first_key(obj, ["message", "msg", "error", "reason", "detail", "error_message"]) or fallback or "failed"
    s = str(msg).replace("\n", " ").replace("\r", " ").strip()
    return s[:200] + "..." if len(s) > 200 else s


def guess_mime(path: str) -> str:
    return mimetypes.guess_type(path)[0] or "application/octet-stream"
