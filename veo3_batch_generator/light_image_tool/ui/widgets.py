from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QLabel, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from light_image_tool.image_selector import is_supported_image


class ImageDropList(QWidget):
    files_added = Signal(list)
    rejected = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setObjectName("dropPanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        self.hint = QLabel("点击选择或拖拽图片到这里")
        self.hint.setAlignment(Qt.AlignCenter)
        self.hint.setObjectName("dropHint")
        self.list_widget = QListWidget()
        self.list_widget.setObjectName("thumbList")
        self.list_widget.setMinimumHeight(120)
        layout.addWidget(self.hint)
        layout.addWidget(self.list_widget, 1)

    def set_files(self, paths: list[str]) -> None:
        self.list_widget.clear()
        for path in paths:
            item = QListWidgetItem(Path(path).name)
            item.setToolTip(path)
            item.setData(Qt.UserRole, path)
            self.list_widget.addItem(item)
        self.hint.setText(f"已选择 {len(paths)} 张图片" if paths else "点击选择或拖拽图片到这里")

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        accepted: list[str] = []
        rejected: list[str] = []
        for url in event.mimeData().urls():
            if not url.isLocalFile():
                rejected.append(url.toString())
                continue
            path = Path(url.toLocalFile())
            if path.is_file() and is_supported_image(path):
                accepted.append(str(path))
            else:
                rejected.append(str(path))
        if accepted:
            self.files_added.emit(accepted)
        if rejected:
            self.rejected.emit(rejected)
        event.acceptProposedAction()
