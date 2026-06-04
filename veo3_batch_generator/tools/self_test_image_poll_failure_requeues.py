from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.api.image_api import ImageGenerationResult
from app.api.image_providers.hellobabygo_image_provider import HelloBabyGoImageProvider
from app.config import AppConfig
from app.models.task import TaskItem
from app.workflow import NODE_STATUS_FAILED, default_workflow_definition, ensure_task_node_states
import app.workflow_executor as workflow_executor


class _FakeResponse:
    ok = True
    status_code = 200

    def json(self):
        return {
            "id": "task_dead",
            "status": "failed",
            "error": {"message": "provider confirmed failure"},
        }


class _FakeRequests:
    @staticmethod
    def get(*args, **kwargs):
        return _FakeResponse()


class _UnknownStatusFailureProvider:
    provider_key = "fake_image"
    provider_name = "Fake Image"
    supports_async_image_tasks = False

    def generate_image(self, *args, **kwargs):
        return ImageGenerationResult(
            False,
            task_id="task_dead",
            status=None,
            error_message="confirmed failed but provider forgot status",
        )


def _test_hellobabygo_failed_poll_marks_failed_status() -> None:
    provider = HelloBabyGoImageProvider()
    import app.api.image_providers.hellobabygo_image_provider as provider_module

    original_requests = provider_module.requests
    provider_module.requests = _FakeRequests
    try:
        result = provider.poll_image_task(
            "task_dead",
            "test-key",
            extra_params={
                "output_path": "",
                "base_url": "https://api.hellobabygo.com",
                "timeout": 1,
                "poll_interval_seconds": 1,
                "max_poll_count": 1,
            },
        )
    finally:
        provider_module.requests = original_requests

    assert not result.success
    assert result.task_id == "task_dead"
    assert result.status == "failed", "confirmed failed poll result must carry status='failed'"


def _test_executor_does_not_treat_unknown_failure_with_task_id_as_queued() -> None:
    config = AppConfig(
        image_provider="fake_image",
        image_model_logical_key="fake_model",
        image_api_key="test-key",
        image_assets_root=Path("outputs/self_test_image_poll_failure/assets"),
        video_download_root=Path("outputs/self_test_image_poll_failure/videos"),
        software_log_root=Path("outputs/self_test_image_poll_failure/logs"),
    )
    workflow = default_workflow_definition()
    task = TaskItem(
        row_index=43,
        pid="1729482708973553395",
        netdisk_path="",
        product_image_path="C:/tmp/source.png",
        image_prompt="make image",
        video_prompt="make video",
    )
    ensure_task_node_states(task, workflow)
    node = workflow["nodes"][0]

    original_get_provider = workflow_executor.get_image_provider
    original_model_display_name = workflow_executor.model_display_name
    workflow_executor.get_image_provider = lambda _key: _UnknownStatusFailureProvider()
    workflow_executor.model_display_name = lambda _provider, _key: "Fake Image"
    try:
        executor = workflow_executor.WorkflowExecutor(
            config,
            workflow,
            log_callback=lambda *_args: None,
            save_emit_callback=lambda *_args: None,
        )
        outcome = executor.run_image_node(task, node)
    finally:
        workflow_executor.get_image_provider = original_get_provider
        workflow_executor.model_display_name = original_model_display_name

    state = task.node_states["image_stage_1"]
    assert not outcome.success
    assert outcome.status == NODE_STATUS_FAILED
    assert state["status"] == NODE_STATUS_FAILED
    assert state.get("task_id") == "task_dead", "failed task_id may be kept for audit until retry reset"

    reset = executor.manual_reset_node(task, "image_stage_1")
    assert reset.success
    assert task.node_states["image_stage_1"].get("task_id") is None, "retry reset must clear stale task_id"


def main() -> None:
    _test_hellobabygo_failed_poll_marks_failed_status()
    _test_executor_does_not_treat_unknown_failure_with_task_id_as_queued()
    print("image poll failure requeue self-test passed")


if __name__ == "__main__":
    main()
