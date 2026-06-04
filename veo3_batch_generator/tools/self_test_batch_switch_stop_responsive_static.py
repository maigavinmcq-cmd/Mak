from __future__ import annotations

import ast
from pathlib import Path


def _class_node(tree: ast.Module, name: str) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"missing class {name}")


def _method_source(source: str, class_node: ast.ClassDef, name: str) -> str:
    for node in class_node.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"missing method {class_node.name}.{name}")


def main() -> None:
    executor_source = Path("app/workflow_executor.py").read_text(encoding="utf-8-sig")
    executor_tree = ast.parse(executor_source)
    executor_class = _class_node(executor_tree, "WorkflowExecutor")
    run_image_node = _method_source(executor_source, executor_class, "run_image_node")
    assert '"stop_event": self._stop_event' in run_image_node
    assert "image polling stopped before completion" in run_image_node
    assert "NODE_STATUS_POLLING" in run_image_node

    gui_source = Path("app/gui.py").read_text(encoding="utf-8-sig")
    gui_tree = ast.parse(gui_source)
    main_window = _class_node(gui_tree, "MainWindow")
    run_with_feedback = _method_source(gui_source, main_window, "run_with_feedback")
    start_worker = _method_source(gui_source, main_window, "start_worker")
    worker_finished = _method_source(gui_source, main_window, "worker_finished")
    assert "QUEUED_FOR_RUN" in run_with_feedback
    assert "enqueue_batch_run(" in start_worker
    running_branch = start_worker[start_worker.index("if self.is_background_running():") : start_worker.index("batch = self.current_batch")]
    assert "stop_worker()" not in running_branch
    assert "start_next_queued_batch(" in worker_finished

    for provider_path, class_name in [
        ("app/api/image_providers/hellobabygo_image_provider.py", "HelloBabyGoImageProvider"),
        ("app/api/image_providers/xibapi_nano_banana_provider.py", "XibapiNanoBananaProvider"),
    ]:
        provider_source = Path(provider_path).read_text(encoding="utf-8-sig")
        provider_tree = ast.parse(provider_source)
        provider_class = _class_node(provider_tree, class_name)
        generate_image = _method_source(provider_source, provider_class, "generate_image")
        poll_image_task = _method_source(provider_source, provider_class, "poll_image_task")
        poll_until_done = _method_source(provider_source, provider_class, "_poll_until_done")
        assert "stop_event" in generate_image
        assert "stop_event" in poll_image_task
        assert "stop_event" in poll_until_done
        assert "_sleep_or_cancel" in poll_until_done
        assert "_stop_requested" in poll_until_done

    print("batch switch/queue static self-test passed")


if __name__ == "__main__":
    main()
