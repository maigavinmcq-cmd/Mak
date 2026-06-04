# White Background Folder Audit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a command-line Python tool that audits `01.产品白底图` folders and exports the results to Excel.

**Architecture:** A single script owns scanning, result modeling, Excel export, and CLI parsing. A small unittest module exercises the filesystem behavior with temporary directories and verifies workbook creation.

**Tech Stack:** Python standard library, `openpyxl`, `unittest`.

---

### Task 1: Scanning Behavior

**Files:**
- Create: `check_empty_product_white_bg.py`
- Create: `tests/test_check_empty_product_white_bg.py`

- [ ] **Step 1: Write failing tests**

```python
import tempfile
import unittest
from pathlib import Path

from check_empty_product_white_bg import scan_white_bg_folders


class WhiteBgAuditTests(unittest.TestCase):
    def test_scan_marks_empty_and_non_empty_target_folders(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            empty = root / "A" / "01.产品白底图"
            filled = root / "B" / "01.产品白底图"
            empty.mkdir(parents=True)
            filled.mkdir(parents=True)
            (filled / "photo.JPG").write_bytes(b"x")

            rows = scan_white_bg_folders(root)

            statuses = {Path(row.folder_path).parent.name: row.status for row in rows}
            self.assertEqual(statuses, {"A": "无图片", "B": "有图片"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_check_empty_product_white_bg -v`
Expected: FAIL or ERROR because `check_empty_product_white_bg` does not exist.

- [ ] **Step 3: Write minimal implementation**

Create `AuditRow`, `is_image_file`, and `scan_white_bg_folders`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_check_empty_product_white_bg -v`
Expected: PASS.

### Task 2: Direct-Only Image Detection

**Files:**
- Modify: `tests/test_check_empty_product_white_bg.py`
- Modify: `check_empty_product_white_bg.py`

- [ ] **Step 1: Write failing test**

```python
def test_scan_ignores_images_inside_target_subfolders(self):
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        target = root / "A" / "01.产品白底图"
        nested = target / "nested"
        nested.mkdir(parents=True)
        (nested / "photo.png").write_bytes(b"x")

        rows = scan_white_bg_folders(root)

        self.assertEqual(rows[0].status, "无图片")
        self.assertEqual(rows[0].image_count, 0)
```

- [ ] **Step 2: Run test to verify it fails if implementation recurses too deeply**

Run: `python -m unittest tests.test_check_empty_product_white_bg -v`
Expected: PASS only when direct-file logic is correct.

- [ ] **Step 3: Keep implementation direct-only**

Use `Path.iterdir()` and filter `child.is_file()`.

### Task 3: Excel Export And CLI

**Files:**
- Modify: `tests/test_check_empty_product_white_bg.py`
- Modify: `check_empty_product_white_bg.py`

- [ ] **Step 1: Write failing test**

```python
from openpyxl import load_workbook
from check_empty_product_white_bg import export_to_excel

def test_export_to_excel_creates_expected_headers(self):
    with tempfile.TemporaryDirectory() as temp_dir:
        output = Path(temp_dir) / "audit.xlsx"
        rows = scan_white_bg_folders(Path(temp_dir))

        export_to_excel(rows, output)

        wb = load_workbook(output)
        ws = wb.active
        self.assertEqual(ws["A1"].value, "序号")
        self.assertEqual(ws["B1"].value, "状态")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_check_empty_product_white_bg -v`
Expected: FAIL or ERROR because `export_to_excel` is missing.

- [ ] **Step 3: Implement Excel export and CLI**

Use `openpyxl.Workbook`, write headers, write one row per audit result, highlight empty rows, and parse optional `--root` and `--output` arguments.

- [ ] **Step 4: Run full verification**

Run: `python -m unittest tests.test_check_empty_product_white_bg -v`
Expected: all tests pass.
