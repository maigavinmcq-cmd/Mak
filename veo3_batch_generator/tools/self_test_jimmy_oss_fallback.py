from __future__ import annotations

import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import AppConfig
from app.models.task import TaskItem
from app.workflow import NODE_STATUS_COMPLETED, default_workflow_definition, ensure_task_node_states, get_node_state, resolve_node_input_images
import app.workflow_executor as executor_module
from app.workflow_executor import WorkflowExecutor


class _RemoteOnlyVideoProvider:
    requires_remote_image_url = True


def main() -> None:
    uploaded: list[tuple[str, str, str, str]] = []

    def fake_upload(image_path: str, upload_api_url: str, api_key: str = "", file_field: str = "file", timeout: int = 120):
        uploaded.append((image_path, upload_api_url, api_key, file_field))
        return "https://oss.example/signed-first-frame.png?token=abc", {"mode": "local_oss"}, None

    original_upload = getattr(executor_module, "upload_image_for_public_url", None)
    executor_module.upload_image_for_public_url = fake_upload
    try:
        with tempfile.TemporaryDirectory() as tmp:
            local_image = Path(tmp) / "image_stage_1_output.png"
            local_image.write_bytes(b"fake-png")
            workflow = default_workflow_definition()
            task = TaskItem(
                row_index=38,
                pid="1730398377089795460",
                netdisk_path="",
                image_prompt="image",
                video_prompt="video",
                product_image_path=str(Path(tmp) / "source.png"),
            )
            ensure_task_node_states(task, workflow)
            image_state = get_node_state(task, "image_stage_1")
            image_state["status"] = NODE_STATUS_COMPLETED
            image_state["output_image_path"] = str(local_image)
            task.generated_image_path = str(local_image)

            video_node = next(node for node in workflow["nodes"] if node["node_id"] == "video_stage_1")
            video_state = get_node_state(task, "video_stage_1")
            inputs, missing = resolve_node_input_images(task, video_node)
            assert not missing

            executor = WorkflowExecutor(
                AppConfig(
                    image_upload_api_url="",
                    image_upload_api_key="upload-key",
                    image_upload_file_field="image",
                    request_timeout_seconds=77,
                ),
                workflow,
            )
            source = executor._preferred_video_image_source(
                inputs,
                _RemoteOnlyVideoProvider(),
                task=task,
                node=video_node,
                video_state=video_state,
            )

            assert source == "https://oss.example/signed-first-frame.png?token=abc"
            assert uploaded == [(str(local_image), "oss://sora2-mission", "upload-key", "image")]
            assert image_state["output_image_url"] == source
            assert task.generated_image_url == source
            assert not video_state.get("remote_image_upload_error")
    finally:
        if original_upload is None:
            delattr(executor_module, "upload_image_for_public_url")
        else:
            executor_module.upload_image_for_public_url = original_upload

    print("jimmy oss fallback self-test passed")


if __name__ == "__main__":
    main()
