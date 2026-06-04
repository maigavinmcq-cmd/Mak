# -*- coding: utf-8 -*-
from __future__ import annotations
from dataclasses import dataclass
import requests

from ..utils import build_chat_completions_url, image_to_data_url, safe_json_loads, find_first_key, extract_video_url

@dataclass
class ApiyiConfig:
    api_key: str
    base_url: str
    model: str
    prompt: str
    image_path: str
    connect_timeout: int = 30
    read_timeout: int = 900
    verify_ssl: bool = True

def run_apiyi_sse(cfg: ApiyiConfig, on_text, on_done, on_error, stop_flag):
    try:
        url = build_chat_completions_url(cfg.base_url)
        headers = {
            "Authorization": f"Bearer {cfg.api_key.strip()}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        image_url = image_to_data_url(cfg.image_path)
        payload = {
            "model": cfg.model.strip(),
            "stream": True,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": cfg.prompt.strip()},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
        }

        timeout = (cfg.connect_timeout, cfg.read_timeout)
        final_video_url = None
        all_text = []

        on_text(f"📡 Request URL: {url}\n")
        on_text("🧠 Provider: APIYI SSE\n")
        on_text(f"🧠 Model: {cfg.model}\n")
        on_text("🚀 开始生成（流式）...\n\n")

        with requests.post(url, headers=headers, json=payload, stream=True, timeout=timeout, verify=cfg.verify_ssl) as r:
            if r.status_code != 200:
                on_text(f"\n❌ HTTP {r.status_code}\n")
                try:
                    r.encoding = "utf-8"
                    on_text(f"❌ Response: {r.text}\n")
                except Exception:
                    pass
                r.raise_for_status()

            r.encoding = "utf-8"

            for raw_line in r.iter_lines(decode_unicode=False):
                if stop_flag.is_set():
                    on_text("\n⛔ 已停止（用户取消）\n")
                    on_done(final_video_url)
                    return
                if not raw_line:
                    continue

                line = raw_line.decode("utf-8", errors="replace")
                if not line.startswith("data:"):
                    continue

                data_str = line[len("data:"):].strip()
                if data_str == "[DONE]":
                    break

                obj = safe_json_loads(data_str)
                if not obj:
                    continue

                content = None
                try:
                    content = obj["choices"][0]["delta"].get("content")
                except Exception:
                    content = find_first_key(obj, ["content"])

                if not content:
                    continue

                on_text(content)
                all_text.append(content)

                maybe = extract_video_url(content)
                if maybe:
                    final_video_url = maybe

        if not final_video_url:
            final_video_url = extract_video_url("".join(all_text))

        on_done(final_video_url)

    except Exception as e:
        on_error(str(e))
