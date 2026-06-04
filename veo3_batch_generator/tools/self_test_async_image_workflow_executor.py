from __future__ import annotations

import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.api.image_api import ImageGenerationResult
from app.config import AppConfig
from app.models.task import TaskItem
from app.workflow import NODE_STATUS_COMPLETED, NODE_STATUS_POLLING, NODE_STATUS_SUBMITTED, NODE_TYPE_IMAGE
from app import workflow_executor as executor_module
from app.workflow_executor import WorkflowExecutor


class FakeAsyncImageProvider:
    provider_key = "fake_async"
    provider_name = "Fake Async"
    supports_async_image_tasks = True
    model_options = [{"logical_key": "gpt_image_2", "display_name": "GPT Image 2", "provider_value": "gpt-image-2"}]
    submit_extra_params = None

    def get_model_option(self, logical_key):
        return self.model_options[0]

    def submit_image_task(self, product_image_path, prompt, model_logical_key, api_key, extra_params=None):
        self.submit_extra_params = dict(extra_params or {})
        return ImageGenerationResult(False, task_id="gpt_img_unit_001", status="queued", raw_response={"status": "queued"})

    def poll_image_task_once(self, task_id, api_key, extra_params=None):
        return ImageGenerationResult(
            True,
            image_path=str(Path(extra_params["output_path"])),
            image_url="https://example.com/result.png",
            task_id=task_id,
            status="completed",
            raw_response={"status": "completed", "video_url": "https://example.com/result.png"},
        )


def main() -> None:
    original_get_image_provider = executor_module.get_image_provider
    original_model_display_name = executor_module.model_display_name
    provider = FakeAsyncImageProvider()
    events: list[tuple[str, str]] = []
    emitted: list[str] = []

    def log(level, message, task=None):
        events.append((level, message))

    def save_emit(task, force=False):
        emitted.append(str((task.node_states.get("image_stage_1") or {}).get("status") or ""))

    workflow = {
        "workflow_version": "unit_async_image",
        "nodes": [
            {
                "node_id": "image_stage_1",
                "node_name": "image",
                "node_type": NODE_TYPE_IMAGE,
                "prompt_field": "stage1",
                "input_refs": ["source_image_1"],
                "output_key": "image_1",
                "enabled": True,
            }
        ],
    }

    try:
        executor_module.get_image_provider = lambda _key: provider
        executor_module.model_display_name = lambda _provider, _key: "GPT Image 2"
        with tempfile.TemporaryDirectory() as tmp:
            cfg = AppConfig(
                image_assets_root=Path(tmp),
                image_provider="fake_async",
                image_model_logical_key="gpt_image_2",
                image_api_key="unit-key",
                image_upload_api_url="oss://sora2-mission",
                image_upload_api_key="upload-key",
                image_upload_file_field="image",
                auto_save_image_assets=False,
            )
            task = TaskItem(
                row_index=2,
                pid="PID001",
                netdisk_path="",
                image_prompt="make an image",
                video_prompt="",
                product_image_url="https://example.com/source.png",
                batch_id="batch_unit",
                task_uid="batch_unit::PID001::row_2",
            )
            executor = WorkflowExecutor(cfg, workflow, log_callback=log, save_emit_callback=save_emit)
            ok, error = executor.prepare_task(task)
            assert ok, error
            node = workflow["nodes"][0]

            submit_outcome = executor.run_image_node(task, node)
            state = task.node_states["image_stage_1"]
            assert submit_outcome.submitted
            assert state["task_id"] == "gpt_img_unit_001"
            assert state["status"] in {NODE_STATUS_SUBMITTED, NODE_STATUS_POLLING}
            assert provider.submit_extra_params["image_upload_api_url"] == "oss://sora2-mission"
            assert provider.submit_extra_params["image_upload_api_key"] == "upload-key"
            assert provider.submit_extra_params["image_upload_file_field"] == "image"

            poll_outcome = executor.run_image_node(task, node)
            state = task.node_states["image_stage_1"]
            assert poll_outcome.completed
            assert state["status"] == NODE_STATUS_COMPLETED
            assert state["output_image_url"] == "https://example.com/result.png"
            assert task.generated_image_url == "https://example.com/result.png"
    finally:
        executor_module.get_image_provider = original_get_image_provider
        executor_module.model_display_name = original_model_display_name

    assert emitted
    assert any("image submitted task_id" in message for _level, message in events)
    print("self_test_async_image_workflow_executor: PASS")


if __name__ == "__main__":
    main()
