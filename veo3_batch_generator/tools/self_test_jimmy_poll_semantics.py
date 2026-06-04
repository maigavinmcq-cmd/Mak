from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.api.video_providers import jimmy_veo_provider as provider_module
from app.api.video_providers.jimmy_veo_provider import JimmyVeoProvider


class FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload
        self.ok = 200 <= status_code < 300
        self.text = str(payload)

    def json(self) -> Any:
        if isinstance(self._payload, BaseException):
            raise self._payload
        return self._payload


def with_fake_get(payload: Any, fn: Callable[[], Any]) -> Any:
    original_get = provider_module.requests.get

    def fake_get(*args: Any, **kwargs: Any) -> FakeResponse:
        return FakeResponse(200, payload)

    provider_module.requests.get = fake_get
    try:
        return fn()
    finally:
        provider_module.requests.get = original_get


def main() -> None:
    provider = JimmyVeoProvider()
    rate_limited = with_fake_get(
        {
            "code": 40000,
            "msg": "\u8bf7\u6c42\u8fc7\u4e8e\u9891\u7e41\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5",
            "data": None,
        },
        lambda: provider.poll_video_task("v_rate_limited", "sk-test"),
    )
    assert rate_limited.success is True
    assert rate_limited.finished is False
    assert rate_limited.failed is False
    assert rate_limited.retryable_failure is True
    assert rate_limited.status == "rate_limited"

    completed = with_fake_get(
        {
            "code": 20000,
            "data": {"status": "success", "video_url": "https://cdn.example/video.mp4"},
        },
        lambda: provider.poll_video_task("v_completed", "sk-test"),
    )
    assert completed.success is True
    assert completed.finished is True
    assert completed.failed is False
    assert completed.video_url == "https://cdn.example/video.mp4"
    print("jimmy poll rate-limit and success status semantics passed")


if __name__ == "__main__":
    main()
