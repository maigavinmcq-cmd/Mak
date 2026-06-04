from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    source = (ROOT / "app" / "batch_manager.py").read_text(encoding="utf-8")
    assert "def _unique_tmp_json_path" in source, "batch JSON writes need per-process unique temp paths"
    assert "os.getpid()" in source and "threading.get_ident()" in source, "temp paths should be unique across processes and threads"
    assert "path.suffix + \".tmp\"" not in source, "fixed .tmp path can collide across UI and worker processes"
    assert "for attempt in range(5)" in source, "batch JSON writes should retry transient network-share failures"
    assert "except (FileNotFoundError, PermissionError)" in source, "network share replace failures should be retried"

    process_source = (ROOT / "app" / "runtime" / "worker_process.py").read_text(encoding="utf-8")
    assert "批次统计暂时无法写入网络盘" in process_source, "worker process should warn, not fail, when final batch stats cannot be written"
    assert '"return_code": 0' in process_source, "worker should still finish successfully when only batch stats write fails"
    print("batch manager atomic static checks passed")


if __name__ == "__main__":
    main()
