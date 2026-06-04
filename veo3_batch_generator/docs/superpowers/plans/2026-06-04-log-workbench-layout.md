# Log Workbench Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the cramped bottom runtime log with a compact event strip and a dedicated log workbench for full inspection.

**Architecture:** Keep the main console focused on the task table by default-collapsing the log panel into a one-line event strip. Add a `LogWorkbenchDialog` in `app/gui.py` that reads the existing in-memory UI log entries, supports filtering and error aggregation, and opens without changing backend execution behavior.

**Tech Stack:** PySide6 desktop UI, existing `MainWindow` log entry cache, static Python self-tests, `py_compile`.

---

### Task 1: Static Test Coverage

**Files:**
- Create: `tools/self_test_log_workbench_ui_static.py`

- [ ] **Step 1: Write a static test**

Create a test that asserts `LogWorkbenchDialog`, `open_log_workbench`, compact log widgets, and default collapsed layout exist.

- [ ] **Step 2: Run test and verify it fails**

Run: `python tools\self_test_log_workbench_ui_static.py`

Expected: failure because the dialog and compact event strip are not implemented yet.

### Task 2: Compact Runtime Event Strip

**Files:**
- Modify: `app/gui.py`
- Modify: `app/ui_layout.py`

- [ ] **Step 1: Default the log panel to collapsed**

Add a `log_panel_default_collapsed` layout option and preserve user toggles after the window is shown.

- [ ] **Step 2: Rebuild the log header**

Show `运行事件`, latest log summary, `日志工作台`, `复制错误`, `打开日志文件`, and `展开/折叠` in the compact strip. Hide INFO/WARN/ERROR filters, auto-scroll, copy, and clear controls while collapsed.

### Task 3: Log Workbench Dialog

**Files:**
- Modify: `app/gui.py`

- [ ] **Step 1: Add `LogWorkbenchDialog`**

Build a large modal with filter fields, error aggregation chips, a log table, and a detail pane.

- [ ] **Step 2: Add filtering behavior**

Support level, category, PID, task_id, keyword, and diagnostic/PERF hiding.

- [ ] **Step 3: Add copy/open actions**

Allow copying selected log details, copying filtered logs, copying latest errors, and opening the batch log file.

### Task 4: History Integration and Verification

**Files:**
- Modify: `app/gui.py`
- Test: `tools/self_test_log_workbench_ui_static.py`

- [ ] **Step 1: Parse loaded batch logs into `_ui_log_entries`**

When a batch is loaded, convert recent `run.log` lines into the existing UI log cache so the workbench sees history, not only live logs.

- [ ] **Step 2: Verify**

Run:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python tools\self_test_log_workbench_ui_static.py
python -m py_compile app\gui.py app\ui_layout.py
```

Expected: tests pass and files compile.
