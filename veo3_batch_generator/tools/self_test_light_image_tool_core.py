from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.api.upload_api as upload_api_module
import light_image_tool.api_adapter as api_adapter_module
from light_image_tool.config import DEFAULT_WHITE_BG_PROMPT, LightImageToolConfig, load_light_image_tool_config
from light_image_tool.image_selector import select_images, supported_image_files
from light_image_tool.api_adapter import LightImageApiAdapter
from light_image_tool.log_manager import redact_secrets
from light_image_tool.models import PIDScanResult, LightImageTaskStatus
from light_image_tool.output_manager import make_output_path, sanitize_filename
from light_image_tool.pid_scanner import PIDScanner, parse_pid_input
from light_image_tool.queue_manager import LightImageQueueManager
from light_image_tool.session_store import load_light_image_tool_session, restore_session_into_queue, save_light_image_tool_session
from light_image_tool.task_executor import LightImageTaskExecutor


def create_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\nunit-test")


def test_config_backfills_defaults(tmp: Path) -> None:
    cfg_path = tmp / "config" / "light_image_tool_config.json"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(json.dumps({"concurrency": "3", "max_concurrency": "bad"}), encoding="utf-8")

    cfg = load_light_image_tool_config(cfg_path)

    assert cfg.config_version == "light_image_tool_v1"
    assert cfg.concurrency == 3
    assert cfg.max_concurrency == 100
    assert cfg.api_task_failed_retry_count == 3
    assert cfg.api_task_failed_retry_interval_seconds == 8
    assert cfg.white_bg_prompt_template == DEFAULT_WHITE_BG_PROMPT
    written = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert written["product_info_subdir"] == "01.product_info"
    assert written["max_concurrency"] == 100


def test_config_migrates_old_concurrency_limit_to_100(tmp: Path) -> None:
    cfg_path = tmp / "config" / "light_image_tool_config.json"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(json.dumps({"concurrency": 88, "max_concurrency": 5}), encoding="utf-8")

    cfg = load_light_image_tool_config(cfg_path)

    assert cfg.concurrency == 88
    assert cfg.max_concurrency == 100

    cfg_path.write_text(json.dumps({"concurrency": 120, "max_concurrency": 5}), encoding="utf-8")
    capped = load_light_image_tool_config(cfg_path)
    assert capped.concurrency == 100
    assert capped.max_concurrency == 100


def test_image_selection_natural_sort_and_missing_names(tmp: Path) -> None:
    product_info = tmp / "01.product_info"
    product_info.mkdir(parents=True)
    for name in ["10.jpg", "2.jpg", "1.jpg", "note.txt", "main.PNG"]:
        (product_info / name).write_text("x", encoding="utf-8")

    images = supported_image_files(product_info)
    assert [p.name for p in images] == ["1.jpg", "2.jpg", "10.jpg", "main.PNG"]
    assert [p.name for p in select_images(images, "first_n", 2).selected] == ["1.jpg", "2.jpg"]
    assert [p.name for p in select_images(images, "last_n", 2).selected] == ["10.jpg", "main.PNG"]
    named = select_images(images, "custom_names", custom_names=["main.PNG", "missing.jpg", "1.jpg"])
    assert [p.name for p in named.selected] == ["main.PNG", "1.jpg"]
    assert "missing.jpg" in named.missing_names


def test_pid_scanner_exact_ambiguous_and_missing(tmp: Path) -> None:
    root = tmp / "SG"
    root.mkdir(parents=True)
    pid_dir = root / "PID001"
    info = pid_dir / "01.product_info"
    info.mkdir(parents=True)
    create_image(info / "1.png")
    create_image(info / "2.png")
    (root / "PID001 copy").mkdir()

    scanner = PIDScanner(
        product_root_dir=str(root),
        product_info_subdir="01.product_info",
        output_subdir="01.white",
        pid_match_mode="exact",
    )
    found = scanner.scan_one("PID001", "first_n", 10)
    assert found.status == "found"
    assert found.pid_dir == str(pid_dir)
    assert [Path(p).name for p in found.selected_images] == ["1.png", "2.png"]
    assert found.output_dir == str(pid_dir / "01.white")

    contains_scanner = PIDScanner(str(root), "01.product_info", "01.white", "contains")
    ambiguous = contains_scanner.scan_one("PID00", "first_n", 10)
    assert ambiguous.status == "ambiguous"
    assert len(ambiguous.candidate_dirs) == 2

    missing = scanner.scan_one("PID404", "first_n", 10)
    assert missing.status == "not_found"


def test_output_path_is_safe_and_incremented(tmp: Path) -> None:
    tmp.mkdir(parents=True)
    existing = tmp / "PID_1_bad_name_white_bg_001.png"
    existing.write_text("x", encoding="utf-8")

    assert sanitize_filename('bad:name?.png') == "bad_name_.png"
    output = make_output_path(
        output_dir=tmp,
        pid="PID:1",
        source_image_name="bad:name?.jpg",
        task_type="pid_white_bg",
    )
    assert output.name == "PID_1_bad_name_white_bg_002.png"


def test_queue_manager_expands_manual_tasks(tmp: Path) -> None:
    source_1 = tmp / "1.png"
    source_2 = tmp / "2.png"
    create_image(source_1)
    create_image(source_2)
    manager = LightImageQueueManager()

    tasks = manager.add_manual_tasks(
        image_paths=[str(source_1), str(source_2)],
        prompt="make it clean",
        generation_count=2,
        output_dir=str(tmp / "out"),
        provider="xibapi_gpt_image2",
        model="gpt_image_2",
    )

    assert len(tasks) == 2
    assert manager.state.total_count == 2
    assert {task.status for task in tasks} == {LightImageTaskStatus.WAITING}
    assert [task.generation_index for task in tasks] == [1, 2]
    assert [task.reference_image_paths for task in tasks] == [[str(source_1), str(source_2)]] * 2
    assert [task.source_image_path for task in tasks] == [str(source_1), str(source_1)]


def test_hellobabygo_light_tool_uses_official_defaults() -> None:
    class FakeAppConfig:
        image_provider = "hellobabygo_image"
        image_model_logical_key = "gpt_image_2"
        image_api_base_url = "https://api.hellobabygo.com"
        image_size = "1024x1792"
        retry_count = 5
        retry_interval_seconds = 5
        request_timeout_seconds = 400
        poll_interval_seconds = 20
        max_poll_count = 1000

    adapter = LightImageApiAdapter()
    adapter._app_config = FakeAppConfig()

    assert adapter.default_provider_model() == ("hellobabygo_image", "auto_image")
    params = adapter._default_extra_params("hellobabygo_image")
    assert params["size"] == "1024x1024"
    assert params["image_size"] == "1024x1024"


def test_api_adapter_returns_safe_request_summary_for_reference_urls() -> None:
    captured: dict[str, object] = {}

    class FakeProvider:
        provider_key = "hellobabygo_image"

        def get_model_option(self, _model_key):
            return {"provider_value": "auto-image"}

        def generate_image(self, product_image_path, prompt, model_logical_key, api_key, extra_params=None):
            captured["product_image_path"] = product_image_path
            captured["prompt"] = prompt
            captured["model_logical_key"] = model_logical_key
            captured["api_key"] = api_key
            captured["extra_params"] = dict(extra_params or {})
            return SimpleNamespace(
                success=False,
                image_url=None,
                image_path=None,
                task_id="task_123",
                status="failed",
                error_message="intentional failure",
                raw_response={"message": "intentional failure"},
            )

    original_get_image_provider = api_adapter_module.get_image_provider
    try:
        api_adapter_module.get_image_provider = lambda _provider_key: FakeProvider()  # type: ignore[assignment]
        adapter = LightImageApiAdapter()
        adapter.default_provider_model = lambda: ("hellobabygo_image", "auto_image")  # type: ignore[method-assign]
        adapter._resolve_api_key = lambda _provider_key: "sk-test-secret"  # type: ignore[method-assign]
        adapter._default_extra_params = lambda _provider_key: {"size": "1024x1024", "image_size": "1024x1024"}  # type: ignore[method-assign]
        adapter._url_is_accessible = lambda _url, _timeout=10: True  # type: ignore[attr-defined]

        result = adapter.generate_image(
            input_image_path="https://cdn.example/ref-1.png",
            prompt="keep references",
            provider="hellobabygo_image",
            model="auto_image",
            extra_params={
                "input_images": [
                    "https://cdn.example/ref-1.png",
                    "https://cdn.example/ref-2.png",
                ],
                "requires_reference_image": True,
            },
        )
    finally:
        api_adapter_module.get_image_provider = original_get_image_provider  # type: ignore[assignment]

    summary = result["request_summary"]
    assert result["success"] is False
    assert summary["provider"] == "hellobabygo_image"
    assert summary["model"] == "auto_image"
    assert summary["provider_model"] == "auto-image"
    assert summary["size"] == "1024x1024"
    assert summary["response_format"] == "url"
    assert summary["reference_count"] == 2
    assert summary["url_count"] == 2
    assert summary["urls"] == ["https://cdn.example/ref-1.png", "https://cdn.example/ref-2.png"]
    assert "sk-test-secret" not in json.dumps(result, ensure_ascii=False)
    assert captured["extra_params"]["input_image_urls"] == summary["urls"]  # type: ignore[index]


def test_api_adapter_uploads_reference_when_mapped_url_unreachable(tmp: Path) -> None:
    source = tmp / "source.png"
    create_image(source)
    captured: dict[str, object] = {}
    uploads: list[str] = []

    class FakeProvider:
        def get_model_option(self, _model_key):
            return {"provider_value": "auto-image"}

        def generate_image(self, product_image_path, prompt, model_logical_key, api_key, extra_params=None):
            captured["extra_params"] = dict(extra_params or {})
            return SimpleNamespace(
                success=False,
                image_url=None,
                image_path=None,
                task_id="task_456",
                status="failed",
                error_message="intentional failure",
                raw_response={"message": "intentional failure"},
            )

    def fake_upload(image_path, upload_api_url, api_key="", file_field="file", timeout=120):
        uploads.append(str(image_path))
        assert upload_api_url == "https://upload.example/images"
        assert file_field == "file"
        assert api_key == "upload-secret"
        return "https://cdn.example/uploaded-source.png", {"url": "https://cdn.example/uploaded-source.png"}, None

    original_get_image_provider = api_adapter_module.get_image_provider
    original_upload = getattr(api_adapter_module, "upload_image_for_public_url", None)
    try:
        api_adapter_module.get_image_provider = lambda _provider_key: FakeProvider()  # type: ignore[assignment]
        setattr(api_adapter_module, "upload_image_for_public_url", fake_upload)
        adapter = LightImageApiAdapter()
        adapter._resolve_api_key = lambda _provider_key: "sk-test-secret"  # type: ignore[method-assign]
        adapter._default_extra_params = lambda _provider_key: {  # type: ignore[method-assign]
            "size": "1024x1024",
            "image_size": "1024x1024",
            "image_upload_api_url": "https://upload.example/images",
            "image_upload_api_key": "upload-secret",
            "image_upload_file_field": "file",
            "timeout": 30,
        }
        adapter._to_public_url = lambda _value: "https://media.example/broken-source.png"  # type: ignore[method-assign]
        adapter._url_is_accessible = lambda url, _timeout=10: url == "https://cdn.example/uploaded-source.png"  # type: ignore[attr-defined]

        result = adapter.generate_image(
            input_image_path=str(source),
            prompt="use uploaded reference",
            provider="hellobabygo_image",
            model="auto_image",
            extra_params={"input_images": [str(source)], "requires_reference_image": True},
        )
    finally:
        api_adapter_module.get_image_provider = original_get_image_provider  # type: ignore[assignment]
        if original_upload is None:
            delattr(api_adapter_module, "upload_image_for_public_url")
        else:
            api_adapter_module.upload_image_for_public_url = original_upload  # type: ignore[assignment]

    assert uploads == [str(source)]
    assert captured["extra_params"]["input_image_urls"] == ["https://cdn.example/uploaded-source.png"]  # type: ignore[index]
    assert result["request_summary"]["urls"] == ["https://cdn.example/uploaded-source.png"]
    assert result["request_summary"]["url_count"] == 1


def test_api_adapter_fails_before_submit_when_url_unreachable_without_upload(tmp: Path) -> None:
    source = tmp / "source.png"
    create_image(source)

    class FakeProvider:
        def get_model_option(self, _model_key):
            return {"provider_value": "auto-image"}

        def generate_image(self, *_args, **_kwargs):
            raise AssertionError("provider should not be called when reference URL is inaccessible")

    original_get_image_provider = api_adapter_module.get_image_provider
    try:
        api_adapter_module.get_image_provider = lambda _provider_key: FakeProvider()  # type: ignore[assignment]
        adapter = LightImageApiAdapter()
        adapter._resolve_api_key = lambda _provider_key: "sk-test-secret"  # type: ignore[method-assign]
        adapter._default_extra_params = lambda _provider_key: {  # type: ignore[method-assign]
            "size": "1024x1024",
            "image_size": "1024x1024",
            "image_upload_api_url": "",
            "timeout": 30,
        }
        adapter._to_public_url = lambda _value: "https://media.example/broken-source.png"  # type: ignore[method-assign]
        adapter._url_is_accessible = lambda _url, _timeout=10: False  # type: ignore[attr-defined]

        result = adapter.generate_image(
            input_image_path=str(source),
            prompt="must not submit bad urls",
            provider="hellobabygo_image",
            model="auto_image",
            extra_params={"input_images": [str(source)], "requires_reference_image": True},
        )
    finally:
        api_adapter_module.get_image_provider = original_get_image_provider  # type: ignore[assignment]

    assert result["success"] is False
    assert "参考图公网 URL 不可访问" in result["error_message"]
    assert "未配置图片上传接口" in result["error_message"]
    assert result["request_summary"]["url_count"] == 0


def test_upload_api_supports_sora2_local_oss_uploader(tmp: Path) -> None:
    source = tmp / "source.png"
    create_image(source)
    sora_root = tmp / "Sora2-mission"
    provider_dir = sora_root / "xv_gui" / "providers"
    provider_dir.mkdir(parents=True)
    (provider_dir / "oss_uploader.py").write_text(
        "def upload_file_and_sign_url(local_image_path):\n"
        "    return 'https://oss.example/signed.png?token=abc'\n",
        encoding="utf-8",
    )
    old_root = os.environ.get("SORA2_MISSION_ROOT")
    os.environ["SORA2_MISSION_ROOT"] = str(sora_root)
    try:
        url, raw, error = upload_api_module.upload_image_for_public_url(
            str(source),
            "oss://sora2-mission",
            api_key="ignored",
        )
    finally:
        if old_root is None:
            os.environ.pop("SORA2_MISSION_ROOT", None)
        else:
            os.environ["SORA2_MISSION_ROOT"] = old_root

    assert error is None
    assert url == "https://oss.example/signed.png?token=abc"
    assert raw["mode"] == "local_oss"


def test_local_oss_upload_forces_https_signed_url(tmp: Path) -> None:
    source = tmp / "source.png"
    create_image(source)
    sora_root = tmp / "Sora2-mission"
    provider_dir = sora_root / "xv_gui" / "providers"
    provider_dir.mkdir(parents=True)
    (provider_dir / "oss_uploader.py").write_text(
        "def upload_file_and_sign_url(local_image_path):\n"
        "    return 'http://oss.example/signed.png?OSSAccessKeyId=abc&Signature=def'\n",
        encoding="utf-8",
    )
    old_root = os.environ.get("SORA2_MISSION_ROOT")
    os.environ["SORA2_MISSION_ROOT"] = str(sora_root)
    try:
        url, _raw, error = upload_api_module.upload_image_for_public_url(str(source), "oss://sora2-mission")
    finally:
        if old_root is None:
            os.environ.pop("SORA2_MISSION_ROOT", None)
        else:
            os.environ["SORA2_MISSION_ROOT"] = old_root

    assert error is None
    assert url.startswith("https://oss.example/")


def test_local_oss_upload_normalizes_mislabeled_webp_to_png(tmp: Path) -> None:
    from PIL import Image

    source = tmp / "source.jpeg"
    source.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (2, 2), (20, 120, 200)).save(source, format="WEBP")
    sora_root = tmp / "Sora2-mission"
    provider_dir = sora_root / "xv_gui" / "providers"
    provider_dir.mkdir(parents=True)
    marker = tmp / "uploaded_path.txt"
    (provider_dir / "oss_uploader.py").write_text(
        "from pathlib import Path\n"
        f"MARKER = Path({str(marker)!r})\n"
        "def upload_file_and_sign_url(local_image_path):\n"
        "    p = Path(local_image_path)\n"
        "    MARKER.write_text(str(p) + '\\n' + p.read_bytes()[:8].hex(), encoding='utf-8')\n"
        "    return 'https://oss.example/normalized.png?token=abc'\n",
        encoding="utf-8",
    )
    old_root = os.environ.get("SORA2_MISSION_ROOT")
    os.environ["SORA2_MISSION_ROOT"] = str(sora_root)
    try:
        url, raw, error = upload_api_module.upload_image_for_public_url(
            str(source),
            "oss://sora2-mission",
        )
    finally:
        if old_root is None:
            os.environ.pop("SORA2_MISSION_ROOT", None)
        else:
            os.environ["SORA2_MISSION_ROOT"] = old_root

    uploaded_path, head_hex = marker.read_text(encoding="utf-8").splitlines()
    assert error is None
    assert url == "https://oss.example/normalized.png?token=abc"
    assert uploaded_path.endswith(".png")
    assert head_hex == "89504e470d0a1a0a"
    assert raw["normalized"] is True


def test_redacts_oss_signed_url_credentials() -> None:
    text = (
        "first_url=https://oss.example/a.png?"
        "OSSAccessKeyId=LTAIsecret&Expires=1779181348&Signature=abc123"
    )

    redacted = redact_secrets(text)

    assert "LTAIsecret" not in redacted
    assert "abc123" not in redacted
    assert "OSSAccessKeyId=****" in redacted
    assert "Signature=****" in redacted


def test_reference_probe_accepts_get_signed_url_when_head_forbidden() -> None:
    calls: list[str] = []

    class FakeResponse:
        def __init__(self, status_code: int, content_type: str):
            self.status_code = status_code
            self.headers = {"Content-Type": content_type}

        def close(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()
            return False

    original_head = api_adapter_module.requests.head
    original_get = api_adapter_module.requests.get

    def fake_head(_url, **_kwargs):
        calls.append("HEAD")
        return FakeResponse(403, "text/html")

    def fake_get(_url, **kwargs):
        calls.append("GET")
        assert "Range" not in dict(kwargs.get("headers") or {})
        return FakeResponse(200, "image/png")

    try:
        api_adapter_module.requests.head = fake_head  # type: ignore[assignment]
        api_adapter_module.requests.get = fake_get  # type: ignore[assignment]

        ok = LightImageApiAdapter._probe_image_url("https://oss.example/signed.png?Signature=abc", 3)
    finally:
        api_adapter_module.requests.head = original_head  # type: ignore[assignment]
        api_adapter_module.requests.get = original_get  # type: ignore[assignment]

    assert ok is True
    assert calls == ["HEAD", "GET"]


def test_light_image_session_round_trips_queue_and_scan_results(tmp: Path) -> None:
    source = tmp / "1.png"
    create_image(source)
    manager = LightImageQueueManager()
    tasks = manager.add_manual_tasks(
        image_paths=[str(source)],
        prompt="remember me",
        generation_count=1,
        output_dir=str(tmp / "out"),
        provider="hellobabygo_image",
        model="auto_image",
    )
    manager.update_task_status(tasks[0], LightImageTaskStatus.GENERATING)
    scan_results = [
        PIDScanResult(
            pid="PID001",
            status="found",
            pid_dir=str(tmp / "PID001"),
            product_info_dir=str(tmp / "PID001" / "01.product_info"),
            output_dir=str(tmp / "PID001" / "01.white"),
            selected_images=[str(source)],
            error_message=None,
        )
    ]
    session_path = tmp / "session.json"

    save_light_image_tool_session(
        session_path,
        queue_manager=manager,
        scan_results=scan_results,
        selected_task_uid=tasks[0].task_uid,
    )
    session = load_light_image_tool_session(session_path)
    restored = LightImageQueueManager()
    restored_scans = restore_session_into_queue(restored, session)

    assert len(restored.tasks) == 1
    assert restored.tasks[0].prompt == "remember me"
    assert restored.tasks[0].status == LightImageTaskStatus.STOPPED
    assert restored.state.queue_id == manager.state.queue_id
    assert restored_scans[0].pid == "PID001"


def test_queue_manager_groups_pid_images_per_generation(tmp: Path) -> None:
    source_1 = tmp / "PID001" / "01.product_info" / "1.png"
    source_2 = tmp / "PID001" / "01.product_info" / "2.png"
    create_image(source_1)
    create_image(source_2)
    output_dir = tmp / "PID001" / "01.white"
    manager = LightImageQueueManager()
    scan_result = PIDScanResult(
        pid="PID001",
        status="found",
        pid_dir=str(tmp / "PID001"),
        product_info_dir=str(source_1.parent),
        output_dir=str(output_dir),
        selected_images=[str(source_1), str(source_2)],
        error_message=None,
    )

    tasks = manager.add_pid_scan_tasks(
        scan_results=[scan_result],
        prompt_template="PID {pid}; images {image_name}; count {total_images}",
        generation_count=3,
        provider="hellobabygo_image",
        model="gpt_image_2",
    )

    assert len(tasks) == 3
    assert manager.state.total_count == 3
    assert [task.generation_index for task in tasks] == [1, 2, 3]
    assert [task.reference_image_paths for task in tasks] == [[str(source_1), str(source_2)]] * 3
    assert {task.prompt for task in tasks} == {"PID PID001; images 1.png, 2.png; count 2"}
    assert {task.source_image_path for task in tasks} == {str(source_1)}


def test_executor_submits_reference_image_group(tmp: Path) -> None:
    source_1 = tmp / "1.png"
    source_2 = tmp / "2.png"
    create_image(source_1)
    create_image(source_2)
    manager = LightImageQueueManager()
    tasks = manager.add_manual_tasks(
        image_paths=[str(source_1), str(source_2)],
        prompt="one prompt for all references",
        generation_count=1,
        output_dir=str(tmp / "out"),
        provider="hellobabygo_image",
        model="gpt_image_2",
    )

    captured: dict[str, object] = {}

    class FakeApiAdapter:
        def generate_image(self, input_image_path, prompt, provider=None, model=None, extra_params=None):
            captured["input_image_path"] = input_image_path
            captured["prompt"] = prompt
            captured["extra_params"] = dict(extra_params or {})
            return {"success": False, "error_message": "intentional test failure"}

    class DummyLogManager:
        queue_dir = tmp / "queue"

        def log(self, *_args, **_kwargs):
            return None

        def exception(self, *_args, **_kwargs):
            return None

    executor = LightImageTaskExecutor(
        manager,
        api_adapter=FakeApiAdapter(),  # type: ignore[arg-type]
        log_manager=DummyLogManager(),  # type: ignore[arg-type]
    )

    executor._run_one(tasks[0])

    assert captured["input_image_path"] == str(source_1)
    assert captured["prompt"] == "one prompt for all references"
    assert captured["extra_params"]["input_images"] == [str(source_1), str(source_2)]  # type: ignore[index]


def test_executor_writes_request_summary_to_diagnostics(tmp: Path) -> None:
    source = tmp / "1.png"
    create_image(source)
    manager = LightImageQueueManager()
    tasks = manager.add_manual_tasks(
        image_paths=[str(source)],
        prompt="diagnose request",
        generation_count=1,
        output_dir=str(tmp / "out"),
        provider="hellobabygo_image",
        model="auto_image",
    )

    class FakeApiAdapter:
        def generate_image(self, input_image_path, prompt, provider=None, model=None, extra_params=None):
            return {
                "success": False,
                "error_message": "intentional failure",
                "request_summary": {
                    "provider": provider,
                    "model": model,
                    "provider_model": "auto-image",
                    "size": "1024x1024",
                    "reference_count": 1,
                    "url_count": 1,
                    "urls": ["https://cdn.example/ref.png"],
                    "prompt_chars": len(prompt),
                },
            }

    class CapturingLogManager:
        queue_dir = tmp / "queue"

        def __init__(self):
            self.entries = []

        def log(self, level, category, message, *, task_uid=None, detail=None, diagnostics=False):
            self.entries.append(
                {
                    "level": level,
                    "category": category,
                    "message": message,
                    "task_uid": task_uid,
                    "detail": detail,
                    "diagnostics": diagnostics,
                }
            )

        def exception(self, *_args, **_kwargs):
            return None

    log_manager = CapturingLogManager()
    executor = LightImageTaskExecutor(
        manager,
        api_adapter=FakeApiAdapter(),  # type: ignore[arg-type]
        log_manager=log_manager,  # type: ignore[arg-type]
    )

    executor._run_one(tasks[0])

    summaries = [entry for entry in log_manager.entries if entry["category"] == "api_payload"]
    assert summaries
    assert summaries[0]["diagnostics"] is True
    assert "https://cdn.example/ref.png" in summaries[0]["detail"]
    assert any(entry.category == "api_payload" for entry in tasks[0].logs)


def test_executor_retries_hellobabygo_task_failed_response(tmp: Path) -> None:
    source = tmp / "1.png"
    create_image(source)
    manager = LightImageQueueManager()
    tasks = manager.add_manual_tasks(
        image_paths=[str(source)],
        prompt="retry transient task_failed",
        generation_count=1,
        output_dir=str(tmp / "out"),
        provider="hellobabygo_image",
        model="gpt_image_2",
    )
    calls = 0

    class FakeApiAdapter:
        def generate_image(self, input_image_path, prompt, provider=None, model=None, extra_params=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {
                    "success": False,
                    "error_message": "{'code': 'task_failed', 'message': '没有按照预期生成图片'}",
                    "request_summary": {
                        "provider": provider,
                        "model": model,
                        "provider_model": "gpt-image-2",
                        "size": "1024x1024",
                        "reference_count": 1,
                        "url_count": 1,
                        "urls": ["https://cdn.example/ref.png"],
                    },
                }
            return {"success": True, "image_bytes": b"image-result", "request_summary": {"provider": provider}}

    class CapturingLogManager:
        queue_dir = tmp / "queue"

        def __init__(self):
            self.entries = []

        def log(self, level, category, message, *, task_uid=None, detail=None, diagnostics=False):
            self.entries.append((level, category, message, detail, diagnostics))

        def exception(self, *_args, **_kwargs):
            return None

    log_manager = CapturingLogManager()
    executor = LightImageTaskExecutor(
        manager,
        api_adapter=FakeApiAdapter(),  # type: ignore[arg-type]
        log_manager=log_manager,  # type: ignore[arg-type]
        task_failed_retry_count=2,
        task_failed_retry_interval_seconds=1,
    )
    executor._sleep_or_stopped = lambda _seconds: False  # type: ignore[method-assign]

    executor._run_one(tasks[0])

    assert calls == 2
    assert tasks[0].status == LightImageTaskStatus.COMPLETED
    assert any(entry.category == "api_retry" for entry in tasks[0].logs)
    assert any(entry[1] == "api_retry" for entry in log_manager.entries)


def test_executor_stops_waiting_tasks_in_one_batch(tmp: Path) -> None:
    source = tmp / "1.png"
    create_image(source)
    manager = LightImageQueueManager()
    tasks = manager.add_manual_tasks(
        image_paths=[str(source)],
        prompt="stop quickly",
        generation_count=250,
        output_dir=str(tmp / "out"),
        provider="xibapi_gpt_image2",
        model="gpt_image_2",
    )

    class DummyLogManager:
        queue_dir = tmp / "queue"

        def log(self, *_args, **_kwargs):
            return None

        def exception(self, *_args, **_kwargs):
            return None

    save_calls = 0

    def fake_save_state(_queue_dir):
        nonlocal save_calls
        save_calls += 1

    updates: list[str] = []
    manager.save_state = fake_save_state  # type: ignore[method-assign]
    executor = LightImageTaskExecutor(
        manager,
        api_adapter=None,  # type: ignore[arg-type]
        log_manager=DummyLogManager(),  # type: ignore[arg-type]
        on_task_updated=lambda task: updates.append(getattr(task, "task_uid", "__bulk__")),
    )

    executor._mark_waiting_as_stopped()

    assert {task.status for task in tasks} == {LightImageTaskStatus.STOPPED}
    assert save_calls == 1
    assert updates == ["__bulk__"]


def test_queue_manager_resets_stale_active_tasks(tmp: Path) -> None:
    source = tmp / "1.png"
    create_image(source)
    manager = LightImageQueueManager()
    tasks = manager.add_manual_tasks(
        image_paths=[str(source)],
        prompt="recover stale active task",
        generation_count=1,
        output_dir=str(tmp / "out"),
        provider="hellobabygo_image",
        model="gpt_image_2",
    )
    manager.update_task_status(tasks[0], LightImageTaskStatus.QUEUED)

    recovered = manager.reset_active_to_waiting()

    assert recovered == 1
    assert tasks[0].status == LightImageTaskStatus.WAITING
    assert tasks[0].started_at is None
    assert any(entry.category == "queue" for entry in tasks[0].logs)


def test_executor_can_detach_ui_callbacks(tmp: Path) -> None:
    class DummyLogManager:
        queue_dir = tmp / "queue"

        def log(self, *_args, **_kwargs):
            return None

    executor = LightImageTaskExecutor(
        LightImageQueueManager(),
        api_adapter=None,  # type: ignore[arg-type]
        log_manager=DummyLogManager(),  # type: ignore[arg-type]
        on_task_updated=lambda _task: None,
        on_log=lambda _level, _message: None,
        on_done=lambda: None,
    )

    executor.detach_ui_callbacks()

    assert executor.on_task_updated is None
    assert executor.on_log is None
    assert executor.on_done is None


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        test_config_backfills_defaults(tmp / "cfg")
        test_config_migrates_old_concurrency_limit_to_100(tmp / "cfg_migrate")
        test_image_selection_natural_sort_and_missing_names(tmp / "images")
        test_pid_scanner_exact_ambiguous_and_missing(tmp / "pid")
        test_output_path_is_safe_and_incremented(tmp / "output")
        test_queue_manager_expands_manual_tasks(tmp / "queue")
        test_hellobabygo_light_tool_uses_official_defaults()
        test_api_adapter_returns_safe_request_summary_for_reference_urls()
        test_api_adapter_uploads_reference_when_mapped_url_unreachable(tmp / "upload_fallback")
        test_api_adapter_fails_before_submit_when_url_unreachable_without_upload(tmp / "upload_missing")
        test_upload_api_supports_sora2_local_oss_uploader(tmp / "local_oss_upload")
        test_local_oss_upload_forces_https_signed_url(tmp / "local_oss_https")
        test_local_oss_upload_normalizes_mislabeled_webp_to_png(tmp / "local_oss_normalize")
        test_redacts_oss_signed_url_credentials()
        test_reference_probe_accepts_get_signed_url_when_head_forbidden()
        test_light_image_session_round_trips_queue_and_scan_results(tmp / "session")
        test_queue_manager_groups_pid_images_per_generation(tmp / "pid_queue")
        test_executor_submits_reference_image_group(tmp / "executor_refs")
        test_executor_writes_request_summary_to_diagnostics(tmp / "executor_summary")
        test_executor_retries_hellobabygo_task_failed_response(tmp / "executor_retry")
        test_executor_stops_waiting_tasks_in_one_batch(tmp / "stop_batch")
        test_queue_manager_resets_stale_active_tasks(tmp / "stale_active")
        test_executor_can_detach_ui_callbacks(tmp / "detach")
    print("self_test_light_image_tool_core passed")


if __name__ == "__main__":
    main()
