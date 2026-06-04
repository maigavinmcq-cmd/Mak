"""Offline smoke test for the v2 workflow engine.

Covers:
- default_workflow_definition() shape and the 4 default nodes
- TaskItem fields for prompt_stage_1..4 + node_states
- resolve_node_input_images() with BLOCKED→READY transition
- compute_runnable_status() per-node DAG resolution
- find_ready_image_nodes() / find_ready_video_nodes() / find_polling_nodes()
- aggregate_task_status() rollups
- auto_migrate_tasks_to_workflow() against pre-v2 task fields

Run as: python -m tools.self_test_workflow_engine
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.task import TaskItem, TaskStatus
from app.workflow import (
    NODE_STATUS_BLOCKED,
    NODE_STATUS_COMPLETED,
    NODE_STATUS_FAILED,
    NODE_STATUS_PENDING,
    NODE_STATUS_POLLING,
    NODE_STATUS_READY,
    NODE_STATUS_SUBMITTED,
    NODE_STATUS_WAITING_INPUT,
    default_workflow_definition,
    ensure_task_node_states,
    get_task_prompt_for_node,
    resolve_node_input_images,
    task_flow_progress_text,
)


def _make_task(**kwargs):
    defaults = dict(
        row_index=2,
        pid="1730000000000000000",
        netdisk_path=r"\\server\share\pid",
        image_prompt="旧图片提示词",
        video_prompt="旧视频提示词",
    )
    defaults.update(kwargs)
    return TaskItem(**defaults)


def test_default_workflow_shape() -> None:
    workflow = default_workflow_definition()
    nodes = workflow["nodes"]
    assert workflow["workflow_version"] == "v2_default_4_stage"
    assert [n["node_id"] for n in nodes] == [
        "image_stage_1",
        "video_stage_1",
        "image_stage_2",
        "video_stage_2",
    ]
    assert nodes[0]["prompt_field"] == "提示词【阶段1】"
    assert nodes[1]["prompt_field"] == "提示词【阶段2】"
    assert nodes[2]["prompt_field"] == "提示词【阶段3】"
    assert nodes[3]["prompt_field"] == "提示词【阶段4】"
    assert nodes[1]["input_refs"] == ["image_stage_1.output_image"]
    assert nodes[2]["input_refs"] == ["image_stage_1.output_image", "source_image_1"]
    assert nodes[3]["input_refs"] == ["image_stage_2.output_image"]


def test_dependency_resolution() -> None:
    workflow = default_workflow_definition()
    nodes = workflow["nodes"]
    task = _make_task(
        prompt_stage_1="阶段1提示词",
        prompt_stage_2="阶段2提示词",
        prompt_stage_3="阶段3提示词",
        prompt_stage_4="阶段4提示词",
        product_image_path=r"C:\tmp\source.png",
    )
    ensure_task_node_states(task, workflow)
    assert set(task.node_states) == {n["node_id"] for n in nodes}
    assert task.node_states["image_stage_1"]["status"] == NODE_STATUS_PENDING
    assert get_task_prompt_for_node(task, nodes[2]) == "阶段3提示词"

    inputs, missing = resolve_node_input_images(task, nodes[2])
    assert inputs == [r"C:\tmp\source.png"]
    assert missing == ["image_stage_1.output_image"]

    task.node_states["image_stage_1"]["status"] = NODE_STATUS_COMPLETED
    task.node_states["image_stage_1"]["output_image_path"] = r"C:\tmp\image1.png"
    inputs, missing = resolve_node_input_images(task, nodes[2])
    assert inputs == [r"C:\tmp\image1.png", r"C:\tmp\source.png"]
    assert missing == []


def test_executor_runnable_status_and_aggregate() -> None:
    # Lazy import to avoid forcing the executor's heavy deps at module load.
    from app.config import AppConfig
    from app.workflow_executor import WorkflowExecutor

    workflow = default_workflow_definition()
    config = AppConfig()
    executor = WorkflowExecutor(config, workflow)
    task = _make_task(
        prompt_stage_1="阶段1提示词",
        prompt_stage_2="阶段2提示词",
        prompt_stage_3="阶段3提示词",
        prompt_stage_4="阶段4提示词",
        product_image_path=r"C:\tmp\source.png",
    )
    ensure_task_node_states(task, workflow)

    # Initial: only stage 1 image is ready. Videos must wait for generated images,
    # never use the product image directly.
    statuses = {n["node_id"]: executor.compute_runnable_status(task, n) for n in workflow["nodes"]}
    assert statuses["image_stage_1"] == NODE_STATUS_READY
    assert statuses["video_stage_1"] in {NODE_STATUS_BLOCKED, NODE_STATUS_WAITING_INPUT}
    assert statuses["image_stage_2"] in {NODE_STATUS_BLOCKED, NODE_STATUS_WAITING_INPUT}
    assert statuses["video_stage_2"] in {NODE_STATUS_BLOCKED, NODE_STATUS_WAITING_INPUT}

    # Failure of stage 1 image blocks both video 1 and image 2 because both
    # depend on 图1.
    task.node_states["image_stage_1"]["status"] = NODE_STATUS_FAILED
    assert executor.compute_runnable_status(task, workflow["nodes"][1]) == NODE_STATUS_BLOCKED  # B
    assert executor.compute_runnable_status(task, workflow["nodes"][2]) == NODE_STATUS_BLOCKED  # C

    # Once stage 1 completes with output, video 1 and image 2 both become READY.
    task.node_states["image_stage_1"]["status"] = NODE_STATUS_COMPLETED
    task.node_states["image_stage_1"]["output_image_path"] = r"C:\tmp\image1.png"
    assert executor.compute_runnable_status(task, workflow["nodes"][1]) == NODE_STATUS_READY
    assert executor.compute_runnable_status(task, workflow["nodes"][2]) == NODE_STATUS_READY

    # find_ready_image_nodes should now offer only C (A already COMPLETED).
    ready_image_ids = [n["node_id"] for n in executor.find_ready_image_nodes(task)]
    assert ready_image_ids == ["image_stage_2"]

    # Submit B → SUBMITTED, then find_polling_nodes() should surface it.
    task.node_states["video_stage_1"]["status"] = NODE_STATUS_SUBMITTED
    task.node_states["video_stage_1"]["task_id"] = "video-task-123"
    polling_ids = [n["node_id"] for n in executor.find_polling_nodes(task)]
    assert polling_ids == ["video_stage_1"]


def test_terminal_input_errors_are_not_rescheduled_without_user_action() -> None:
    from app.config import AppConfig
    from app.workflow_executor import WorkflowExecutor

    workflow = default_workflow_definition()
    executor = WorkflowExecutor(AppConfig(), workflow)
    task = _make_task(
        prompt_stage_1="",
        prompt_stage_2="",
        prompt_stage_3="",
        prompt_stage_4="",
        product_image_path=r"C:\tmp\source.png",
    )
    ensure_task_node_states(task, workflow)

    image_state = task.node_states["image_stage_1"]
    image_state["status"] = NODE_STATUS_WAITING_INPUT
    image_state["error_message"] = "缺少提示词：阶段1"
    video_state = task.node_states["video_stage_1"]
    video_state["status"] = NODE_STATUS_BLOCKED
    video_state["error_message"] = "缺少输入图片：image_stage_1.output_image"

    assert executor.find_ready_image_nodes(task) == []
    assert executor.find_ready_video_nodes(task) == []


def test_stale_product_video_submission_is_reset() -> None:
    from app.config import AppConfig
    from app.workflow_executor import WorkflowExecutor

    workflow = default_workflow_definition()
    executor = WorkflowExecutor(AppConfig(), workflow)
    task = _make_task(
        prompt_stage_1="阶段1提示词",
        prompt_stage_2="阶段2提示词",
        product_image_path=r"C:\tmp\product.png",
    )
    ensure_task_node_states(task, workflow)
    task.node_states["image_stage_1"]["status"] = NODE_STATUS_COMPLETED
    task.node_states["image_stage_1"]["output_image_path"] = r"C:\tmp\image1.png"
    task.node_states["video_stage_1"]["status"] = NODE_STATUS_SUBMITTED
    task.node_states["video_stage_1"]["task_id"] = "wrong-product-video-task"
    task.node_states["video_stage_1"]["input_images"] = [r"C:\tmp\product.png"]
    task.video_task_id = "wrong-product-video-task"

    repaired = executor.repair_task_without_source_lookup(task)
    assert repaired == 1
    assert task.node_states["video_stage_1"]["status"] == NODE_STATUS_PENDING
    assert task.node_states["video_stage_1"]["task_id"] is None
    assert task.video_task_id is None
    assert executor.compute_runnable_status(task, workflow["nodes"][1]) == NODE_STATUS_READY


def test_auto_migrate_legacy_state() -> None:
    from app.workflow_executor import auto_migrate_tasks_to_workflow

    workflow = default_workflow_definition()
    # Legacy COMPLETED task: image generated, video URL present.
    task_legacy = _make_task(
        generated_image_path=r"C:\tmp\image2.png",
        generated_image_url="https://cdn/image2.png",
        video_url="https://cdn/video1.mp4",
        video_task_id="vid-123",
        status=TaskStatus.COMPLETED,
    )
    auto_migrate_tasks_to_workflow([task_legacy], workflow)
    assert task_legacy.node_states["image_stage_1"]["status"] == NODE_STATUS_COMPLETED
    assert task_legacy.node_states["image_stage_1"]["output_image_path"] == r"C:\tmp\image2.png"
    assert task_legacy.node_states["video_stage_1"]["status"] == NODE_STATUS_COMPLETED
    assert task_legacy.node_states["video_stage_1"]["output_video_url"] == "https://cdn/video1.mp4"

    # Legacy SUBMITTED task: video has task_id but no URL yet.
    task_submitted = _make_task(
        video_task_id="vid-pending-456",
        status=TaskStatus.VIDEO_POLLING,
    )
    auto_migrate_tasks_to_workflow([task_submitted], workflow)
    assert task_submitted.node_states["video_stage_1"]["status"] == NODE_STATUS_SUBMITTED
    assert task_submitted.node_states["video_stage_1"]["task_id"] == "vid-pending-456"

    # Legacy FAILED_IMAGE_API task.
    task_failed = _make_task(
        status=TaskStatus.FAILED_IMAGE_API,
        error_message="HTTP 500",
    )
    auto_migrate_tasks_to_workflow([task_failed], workflow)
    assert task_failed.node_states["image_stage_1"]["status"] == NODE_STATUS_FAILED


def test_flow_progress_text() -> None:
    workflow = default_workflow_definition()
    task = _make_task(prompt_stage_1="x", product_image_path=r"C:\tmp\source.png")
    text = task_flow_progress_text(task, workflow)
    assert "阶段1" in text and "阶段4" in text


def test_image_node_saves_async_task_id_before_provider_returns() -> None:
    from app.api.image_api import ImageGenerationResult
    from app.config import AppConfig
    import app.workflow_executor as workflow_executor_module
    from app.workflow_executor import WorkflowExecutor

    workflow = default_workflow_definition()
    with tempfile.TemporaryDirectory() as tmp:
        config = AppConfig(
            image_provider="fake_image_provider",
            image_api_key="fake-key",
            image_assets_root=Path(tmp),
        )
        save_snapshots = []
        callback_seen = {}

        class FakeAsyncImageProvider:
            def generate_image(self, product_image_path, prompt, model_logical_key, api_key, extra_params=None):
                callback = (extra_params or {}).get("on_task_id")
                assert callable(callback), "workflow executor must pass on_task_id to async image providers"
                callback("img-task-123", {"id": "img-task-123", "status": "queued"})
                callback_seen["task_id"] = task.image_task_id
                callback_seen["node_status"] = task.node_states["image_stage_1"]["status"]
                return ImageGenerationResult(
                    True,
                    image_path=str((extra_params or {})["output_path"]),
                    image_url="https://cdn.example/image.png",
                    task_id="img-task-123",
                    raw_response={"id": "img-task-123", "status": "completed"},
                )

        original_get_provider = workflow_executor_module.get_image_provider
        original_model_display = workflow_executor_module.model_display_name
        try:
            workflow_executor_module.get_image_provider = lambda _provider_key: FakeAsyncImageProvider()
            workflow_executor_module.model_display_name = lambda _provider, _key: "Fake Image"

            executor = WorkflowExecutor(
                config,
                workflow,
                save_emit_callback=lambda saved_task, force=False: save_snapshots.append(
                    (
                        force,
                        saved_task.image_task_id,
                        saved_task.node_states["image_stage_1"].get("status"),
                        saved_task.status,
                    )
                ),
            )
            task = _make_task(
                prompt_stage_1="stage 1",
                product_image_path=r"C:\tmp\source.png",
            )
            executor.prepare_task(task)
            outcome = executor.run_image_node(task, workflow["nodes"][0])
        finally:
            workflow_executor_module.get_image_provider = original_get_provider
            workflow_executor_module.model_display_name = original_model_display

    assert outcome.completed
    assert callback_seen == {"task_id": "img-task-123", "node_status": NODE_STATUS_POLLING}
    assert any(force and task_id == "img-task-123" and node_status == NODE_STATUS_POLLING for force, task_id, node_status, _status in save_snapshots)
    assert task.image_task_id == "img-task-123"


def test_existing_legacy_video_url_is_hydrated_for_download_even_when_node_not_pending() -> None:
    from app.config import AppConfig
    from app.workflow_executor import WorkflowExecutor

    workflow = default_workflow_definition()
    executor = WorkflowExecutor(AppConfig(), workflow)
    task = _make_task(
        prompt_stage_1="stage 1",
        prompt_stage_2="stage 2",
        product_image_path=r"C:\tmp\source.png",
        generated_image_path=r"C:\tmp\image1.png",
        video_url="https://cdn.example/video-ready-but-not-archived.mp4",
        video_task_id="vid-ready-123",
        status=TaskStatus.PENDING,
    )
    ensure_task_node_states(task, workflow)
    task.node_states["image_stage_1"]["status"] = NODE_STATUS_READY
    task.node_states["video_stage_1"]["status"] = NODE_STATUS_READY

    ok, error = executor.prepare_task(task)

    assert ok, error
    assert task.node_states["image_stage_1"]["status"] == NODE_STATUS_COMPLETED
    assert task.node_states["image_stage_1"]["output_image_path"] == r"C:\tmp\image1.png"
    assert task.node_states["video_stage_1"]["status"] == NODE_STATUS_COMPLETED
    assert task.node_states["video_stage_1"]["output_video_url"] == "https://cdn.example/video-ready-but-not-archived.mp4"
    assert [node["node_id"] for node in executor.find_downloadable_nodes(task)] == ["video_stage_1"]
    assert executor.aggregate_task_status(task) == TaskStatus.VIDEO_DOWNLOAD_PENDING


def test_existing_downloaded_node_path_is_not_cleared_by_legacy_video_url_hydration() -> None:
    from app.config import AppConfig
    from app.workflow_executor import WorkflowExecutor

    workflow = default_workflow_definition()
    executor = WorkflowExecutor(AppConfig(), workflow)
    task = _make_task(
        prompt_stage_1="stage 1",
        prompt_stage_2="stage 2",
        product_image_path=r"C:\tmp\source.png",
        generated_image_path=r"C:\tmp\image1.png",
        video_url="https://cdn.example/already-archived.mp4",
        video_file_path=None,
        video_task_id="vid-ready-123",
        status=TaskStatus.VIDEO_DOWNLOADED,
    )
    ensure_task_node_states(task, workflow)
    task.node_states["image_stage_1"]["status"] = NODE_STATUS_COMPLETED
    task.node_states["image_stage_1"]["output_image_path"] = r"C:\tmp\image1.png"
    task.node_states["video_stage_1"]["status"] = NODE_STATUS_COMPLETED
    task.node_states["video_stage_1"]["output_video_url"] = "https://cdn.example/already-archived.mp4"
    task.node_states["video_stage_1"]["output_video_local_path"] = r"C:\tmp\already-archived.mp4"

    ok, error = executor.prepare_task(task)

    assert ok, error
    assert task.node_states["video_stage_1"]["output_video_local_path"] == r"C:\tmp\already-archived.mp4"
    assert executor.aggregate_task_status(task) == TaskStatus.VIDEO_DOWNLOADED


def test_existing_image_node_path_is_not_cleared_by_partial_legacy_image_fields() -> None:
    from app.config import AppConfig
    from app.workflow_executor import WorkflowExecutor

    workflow = default_workflow_definition()
    executor = WorkflowExecutor(AppConfig(), workflow)
    task = _make_task(
        prompt_stage_1="stage 1",
        product_image_path=r"C:\tmp\source.png",
        generated_image_path=None,
        generated_image_url="https://cdn.example/image.png",
        image_task_id=None,
        status=TaskStatus.IMAGE_DONE,
    )
    ensure_task_node_states(task, workflow)
    task.node_states["image_stage_1"]["status"] = NODE_STATUS_COMPLETED
    task.node_states["image_stage_1"]["output_image_path"] = r"C:\tmp\image.png"
    task.node_states["image_stage_1"]["output_image_url"] = "https://cdn.example/image.png"
    task.node_states["image_stage_1"]["task_id"] = "img-task-1"

    ok, error = executor.prepare_task(task)

    assert ok, error
    state = task.node_states["image_stage_1"]
    assert state["output_image_path"] == r"C:\tmp\image.png"
    assert state["output_image_url"] == "https://cdn.example/image.png"
    assert state["task_id"] == "img-task-1"


def test_auto_retry_failed_image_node_uses_current_image_provider_config() -> None:
    from app.config import AppConfig
    from app.workflow_executor import WorkflowExecutor

    workflow = default_workflow_definition()
    config = AppConfig(
        image_provider="hellobabygo_image",
        image_model_logical_key="gpt_image_2",
        retry_count=5,
        auto_retry_failed_workflow_enabled=True,
        auto_retry_image_nodes=True,
    )
    executor = WorkflowExecutor(config, workflow)
    task = _make_task(
        prompt_stage_1="stage 1",
        product_image_path=r"C:\tmp\source.png",
        image_provider="hellobabygo_image",
        image_model_logical_key="gpt_image_2",
    )
    ensure_task_node_states(task, workflow)
    state = task.node_states["image_stage_1"]
    state["status"] = NODE_STATUS_FAILED
    state["provider"] = "xibapi_gpt_image2"
    state["model_logical_key"] = "gpt_image_2"
    state["error_message"] = "old provider failed"

    reset_count = executor.reset_failed_nodes_for_auto_retry(task)

    state = task.node_states["image_stage_1"]
    assert reset_count == 1
    assert state["provider"] == "hellobabygo_image"
    assert state["model_logical_key"] == "gpt_image_2"
    assert task.image_provider == "hellobabygo_image"


def test_fresh_image_submission_ignores_stale_node_provider_without_task_id() -> None:
    from app.api.image_api import ImageGenerationResult
    from app.config import AppConfig
    import app.workflow_executor as workflow_executor_module
    from app.workflow_executor import WorkflowExecutor

    workflow = default_workflow_definition()
    captured = {}

    class FakeImageProvider:
        def generate_image(self, product_image_path, prompt, model_logical_key, api_key, extra_params=None):
            captured["model_logical_key"] = model_logical_key
            return ImageGenerationResult(
                True,
                image_path=str((extra_params or {})["output_path"]),
                image_url="https://cdn.example/new-image.png",
                raw_response={"ok": True},
            )

    original_get_provider = workflow_executor_module.get_image_provider
    original_model_display = workflow_executor_module.model_display_name
    try:
        def fake_get_provider(provider_key):
            captured["provider_key"] = provider_key
            return FakeImageProvider()

        workflow_executor_module.get_image_provider = fake_get_provider
        workflow_executor_module.model_display_name = lambda _provider, key: f"display:{key}"

        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig(
                image_provider="hellobabygo_image",
                image_model_logical_key="gpt_image_2",
                image_assets_root=tmp,
                retry_count=5,
            )
            executor = WorkflowExecutor(config, workflow)
            task = _make_task(
                prompt_stage_1="stage 1",
                product_image_path=r"C:\tmp\source.png",
                image_provider="hellobabygo_image",
                image_model_logical_key="gpt_image_2",
            )
            ensure_task_node_states(task, workflow)
            state = task.node_states["image_stage_1"]
            state["status"] = NODE_STATUS_READY
            state["provider"] = "xibapi_gpt_image2"
            state["model_logical_key"] = "stale_model"
            state["task_id"] = None

            outcome = executor.run_image_node(task, workflow["nodes"][0])
    finally:
        workflow_executor_module.get_image_provider = original_get_provider
        workflow_executor_module.model_display_name = original_model_display

    state = task.node_states["image_stage_1"]
    assert outcome.completed
    assert captured["provider_key"] == "hellobabygo_image"
    assert captured["model_logical_key"] == "gpt_image_2"
    assert state["provider"] == "hellobabygo_image"
    assert state["model_logical_key"] == "gpt_image_2"


def main() -> None:
    test_default_workflow_shape()
    test_dependency_resolution()
    test_executor_runnable_status_and_aggregate()
    test_terminal_input_errors_are_not_rescheduled_without_user_action()
    test_stale_product_video_submission_is_reset()
    test_auto_migrate_legacy_state()
    test_flow_progress_text()
    test_image_node_saves_async_task_id_before_provider_returns()
    test_existing_legacy_video_url_is_hydrated_for_download_even_when_node_not_pending()
    test_existing_downloaded_node_path_is_not_cleared_by_legacy_video_url_hydration()
    test_existing_image_node_path_is_not_cleared_by_partial_legacy_image_fields()
    test_auto_retry_failed_image_node_uses_current_image_provider_config()
    test_fresh_image_submission_ignores_stale_node_provider_without_task_id()
    print("OK — all workflow engine smoke tests passed")


if __name__ == "__main__":
    main()
