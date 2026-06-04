# UI Worker Process Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move long-running batch execution out of the PySide6 UI process so heavy API, download, netdisk, logging, and state-save work cannot freeze the front-end.

**Architecture:** The UI process starts a separate Python worker process for a batch run. The worker process reuses the existing `BatchWorker` execution engine, writes task state through the existing `TaskManager`, and emits lightweight JSONL events for logs, stats, task updates, and completion. The UI polls those events on a timer and only refreshes visible data at a throttled rate.

**Tech Stack:** PySide6 UI, Python `subprocess`, JSONL event files, existing `BatchWorker`, existing `BatchManager` and `TaskManager`.

---

### Task 1: Runtime Message Files

**Files:**
- Create: `app/runtime/__init__.py`
- Create: `app/runtime/worker_events.py`

- [ ] **Step 1: Add JSONL helpers**

Create helpers that write compact line-delimited JSON events and read them from a byte offset without blocking the UI.

- [ ] **Step 2: Verify helpers compile**

Run:

```powershell
python -m py_compile app\runtime\worker_events.py
```

Expected: exit code `0`.

---

### Task 2: Worker Process Entrypoint

**Files:**
- Create: `app/runtime/worker_process.py`
- Modify: `app/worker.py`

- [ ] **Step 1: Build process CLI**

The CLI accepts `--batch-id`, `--mode`, `--failed-only`, `--poll-only`, `--selected-task-keys-json`, `--events-path`, and `--commands-path`.

- [ ] **Step 2: Load batch and state**

Use `load_config()`, `BatchManager`, and `TaskManager` to load the target batch's `task_state.json`.

- [ ] **Step 3: Run existing execution engine**

Instantiate `BatchWorker` with the batch config and workflow snapshot, connect its signals to JSONL event writers, then call `worker.run()` inside the worker process.

- [ ] **Step 4: Add command watcher**

Start a daemon thread that reads command events and calls `worker.pause()`, `worker.resume()`, `worker.stop()`, or `worker.request_poll_cycle()`.

- [ ] **Step 5: Verify import and compile**

Run:

```powershell
python -m py_compile app\runtime\worker_process.py app\worker.py
```

Expected: exit code `0`.

---

### Task 3: UI Process Supervisor

**Files:**
- Modify: `app/gui.py`
- Create: `tools/self_test_process_worker_static.py`

- [ ] **Step 1: Write static regression test**

Assert that `start_worker()` launches a subprocess and does not instantiate `BatchWorker` directly for normal execution.

- [ ] **Step 2: Add process fields to MainWindow**

Track `worker_process`, `worker_events_path`, `worker_commands_path`, and `worker_event_offset`.

- [ ] **Step 3: Start process from UI**

Replace the direct in-process `BatchWorker` startup with `subprocess.Popen([sys.executable, "-m", "app.runtime.worker_process", ...])`.

- [ ] **Step 4: Poll events with QTimer**

Every 300-500ms, read new worker events and update logs, stats, task row refresh queue, completion status, and batch cards.

- [ ] **Step 5: Keep UI controls responsive**

After the process starts, close the loading overlay immediately and show “后台执行中”.

- [ ] **Step 6: Verify static regression**

Run:

```powershell
python tools\self_test_process_worker_static.py
```

Expected: pass.

---

### Task 4: Pause, Resume, Stop, Poll Commands

**Files:**
- Modify: `app/gui.py`
- Modify: `app/runtime/worker_process.py`

- [ ] **Step 1: Write commands to JSONL**

`pause_worker()`, `resume_worker()`, `stop_worker()`, and immediate poll should append command events for process workers.

- [ ] **Step 2: Preserve QThread fallback**

If no worker process is active, keep using the existing QThread methods.

- [ ] **Step 3: Verify compile**

Run:

```powershell
python -m py_compile app\gui.py app\runtime\worker_process.py
```

Expected: exit code `0`.

---

### Task 5: Verification

**Files:**
- Test: `tools/self_test_start_worker_nonblocking_static.py`
- Test: `tools/self_test_process_worker_static.py`
- Test: existing workflow/provider static tests

- [ ] **Step 1: Run compile**

```powershell
python -m py_compile app\gui.py app\worker.py app\runtime\worker_events.py app\runtime\worker_process.py
```

- [ ] **Step 2: Run regression tests**

```powershell
python tools\self_test_start_worker_nonblocking_static.py
python tools\self_test_process_worker_static.py
python tools\self_test_ui_batch_switch_nonblocking_static.py
python tools\self_test_batch_switch_stop_responsive_static.py
python tools\self_test_workflow_retry_failed_static.py
```

Expected: all pass.

---

## Self-Review

- No placeholders remain.
- The plan keeps the first delivery scoped to process isolation for batch execution.
- Existing in-process QThread behavior is kept as fallback for manual poll or emergency compatibility.
