from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKER_PATH = ROOT / "app" / "worker.py"


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
    source = WORKER_PATH.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    worker = _class_node(tree, "ManualPollWorker")
    run_source = _method_source(source, worker, "run")
    candidate_source = _method_source(source, worker, "_poll_manual_candidate")

    assert "max_poll_workers" in run_source, "manual poll must derive worker count from poll_concurrency"
    assert "ThreadPoolExecutor(max_workers=max_poll_workers)" in run_source, "manual poll must poll concurrently"
    assert "pool.submit(self._poll_manual_candidate" in run_source, "manual poll candidates must be submitted to the pool"
    assert "_acquire_poll_lock(task_id)" in candidate_source, "manual poll must keep the per task_id poll lock"
    assert "_release_poll_lock(task_id)" in candidate_source, "manual poll must release the per task_id poll lock"
    assert "summary." not in candidate_source, "manual poll worker threads must not mutate summary counters directly"

    print("manual poll concurrency static self-test passed")


if __name__ == "__main__":
    main()
