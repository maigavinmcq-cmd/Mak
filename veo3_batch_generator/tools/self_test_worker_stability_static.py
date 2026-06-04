from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_worker_has_diagnostics_and_stop_source() -> None:
    worker = read("app/worker.py")
    assert "diagnostics.log" in worker or "DiagnosticsLogger" in worker, "worker must write per-batch diagnostics.log"
    assert "USER_CLICK_STOP" in worker, "only explicit user stop should be tagged USER_CLICK_STOP"
    assert "NO_ACTIVE_WORK" in worker, "natural scheduler drain must have its own stop reason"
    assert "WORKER_EXCEPTION" in worker, "worker crashes must be tagged and diagnosable"
    assert "traceback.format_exc" in worker, "worker exceptions must include traceback"


def test_process_worker_emits_traceback_on_crash() -> None:
    source = read("app/runtime/worker_process.py")
    assert "traceback.format_exc" in source, "worker process top-level crash events must include traceback"


def test_provider_sessions_are_reused() -> None:
    image = read("app/api/image_providers/xibapi_gpt_image2_provider.py")
    video = read("app/api/video_providers/hellobabygo_veo_provider.py")
    xibapi_video = read("app/api/video_api.py")
    file_utils = read("app/file_utils.py")
    assert "requests.Session" in image, "xibapi image provider should reuse a requests.Session"
    assert "requests.Session" in video, "HelloBabyGo video provider should reuse a requests.Session"
    assert "requests.Session" in xibapi_video, "xibapi video API should reuse a requests.Session"
    assert "requests.Session" in file_utils, "shared file download helpers should reuse a requests.Session"


if __name__ == "__main__":
    test_worker_has_diagnostics_and_stop_source()
    test_process_worker_emits_traceback_on_crash()
    test_provider_sessions_are_reused()
    print("worker stability static checks passed")
