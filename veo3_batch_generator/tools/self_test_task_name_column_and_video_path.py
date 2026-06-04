from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _install_pydantic_stub_if_needed() -> None:
    try:
        import pydantic  # noqa: F401

        return
    except ModuleNotFoundError:
        pass

    class FakeBaseModel:
        def __init__(self, **kwargs):
            defaults = {}
            for cls in reversed(type(self).mro()):
                defaults.update(getattr(cls, "__annotations__", {}))
            for name in defaults:
                if hasattr(type(self), name):
                    value = getattr(type(self), name)
                    if isinstance(value, (dict, list)):
                        value = value.copy()
                    setattr(self, name, value)
            for key, value in kwargs.items():
                setattr(self, key, value)

        def model_dump(self) -> dict:
            return dict(self.__dict__)

    def Field(default=None, default_factory=None):
        if default_factory is not None:
            return default_factory()
        return default

    def ConfigDict(**kwargs):
        return dict(kwargs)

    sys.modules["pydantic"] = types.SimpleNamespace(
        BaseModel=FakeBaseModel,
        Field=Field,
        ConfigDict=ConfigDict,
    )


def _install_pandas_stub(rows: list[dict[str, str]]) -> None:
    class FakeRow(dict):
        pass

    class FakeDataFrame:
        def __init__(self, data: list[dict[str, str]]) -> None:
            self._data = data
            self.columns = list(data[0].keys()) if data else []

        @property
        def empty(self) -> bool:
            return not self._data

        def iterrows(self):
            for index, row in enumerate(self._data):
                yield index, FakeRow(row)

    def read_excel(path, dtype=None, engine=None):
        del path, dtype, engine
        return FakeDataFrame(rows)

    def isna(value) -> bool:
        return value is None

    sys.modules["pandas"] = types.SimpleNamespace(read_excel=read_excel, isna=isna)


def main() -> None:
    _install_pydantic_stub_if_needed()
    _install_pandas_stub(
        [
            {
                "PID": "1730000000000000000",
                "任务名称": "夏季新品 30ml/香水:测试*款",
                "网盘路径": r"\\server\share\pid",
                "提示词【阶段1】": "生成图1",
            }
        ]
    )

    from app.excel_loader import load_tasks_from_excel
    from app.file_utils import stage_video_dir

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        excel_path = tmp_path / "tasks.xlsx"
        excel_path.write_text("stub", encoding="utf-8")

        tasks = load_tasks_from_excel(excel_path)
        assert len(tasks) == 1
        task = tasks[0]
        assert task.task_name == "夏季新品 30ml/香水:测试*款"

        task.batch_date = "2026-05-14"
        task.batch_id = "2026-05-14_120000"
        video_dir = stage_video_dir(tmp_path / "videos", task, group_by_owner=False)

        assert video_dir.name == "夏季新品 30ml_香水_测试_款"
        assert "row_2" not in str(video_dir)

    print("task name column and video path self-test passed")


if __name__ == "__main__":
    main()
