# Async Image Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make large batches faster by splitting image generation into async submit and independent polling, while updating XIBAPI GPT-image2 to the new `/v1/videos` async API.

**Architecture:** Image providers may expose `submit_image_task()` and `poll_image_task_once()` in addition to the existing blocking `generate_image()`. The v2 workflow executor submits async image tasks quickly, persists task IDs, and lets the workflow poll loop advance both image and video nodes. The worker keeps UI events lightweight and wakes downstream nodes whenever an image/video completes.

**Tech Stack:** Python, PySide6 worker process, requests, existing provider registry, existing `WorkflowExecutor` and `BatchWorker`.

---

### Task 1: XIBAPI GPT-image2 Async Provider

**Files:**
- Modify: `app/api/base_provider.py`
- Modify: `app/api/image_providers/xibapi_gpt_image2_provider.py`
- Test: `tools/self_test_xibapi_gpt_image2_async_provider.py`

- [ ] Add optional image-provider async methods: `submit_image_task()` and `poll_image_task_once()`.
- [ ] Update XIBAPI GPT-image2 models to `gpt-image-2`, `gpt-image-2-2K`, `gpt-image-2-4K`.
- [ ] Submit images through `POST /v1/videos` with `metadata.size` and `metadata.urls`.
- [ ] Poll one time through `GET /v1/videos/{task_id}` and interpret `queued`, `in_progress`, `completed`, `failed`.
- [ ] Download completed image from `video_url` to the output path.
- [ ] Verify request payload and poll parsing with mocked requests.

### Task 2: Workflow Executor Async Image Path

**Files:**
- Modify: `app/workflow_executor.py`
- Test: `tools/self_test_async_image_workflow_executor.py`

- [ ] In `run_image_node()`, if provider supports async submit and no existing task ID, submit once, set node `SUBMITTED`, persist task ID, and return immediately.
- [ ] If node has task ID, call `poll_image_task_once()` once instead of blocking until completion.
- [ ] Keep legacy blocking behavior for providers without async methods.
- [ ] Preserve existing output mirroring, metadata saving, and retry behavior.
- [ ] Verify an image submit does not complete synchronously and a later poll completes the node.

### Task 3: Worker Poll Loop Drives Image and Video Nodes

**Files:**
- Modify: `app/worker.py`
- Test: `tools/self_test_worker_async_image_polling.py`

- [ ] Include image polling nodes in the workflow polling loop.
- [ ] Use `poll_concurrency` for image polling, not image submit concurrency.
- [ ] Wake task drivers after image completion so `video_stage_1`, `image_stage_2`, and downstream nodes can start.
- [ ] Update loop waiting logic so image polling nodes keep the worker alive.
- [ ] Verify a submitted image node is polled and then unlocks a dependent video/image node.

### Task 4: Runtime Concurrency Clarity

**Files:**
- Modify: `app/gui.py`
- Modify: `app/runtime/worker_process.py`

- [ ] When starting a batch, use current global concurrency values as runtime overrides while keeping the batch's provider/model/API/path snapshot.
- [ ] Log runtime concurrency values at worker start.
- [ ] Keep batch config snapshot unchanged unless user saves global settings.
- [ ] Verify the worker log reflects current concurrency settings.

### Task 5: Regression Smoke

**Files:**
- Test only.

- [ ] Run `py_compile` for changed files.
- [ ] Run async provider/executor/worker self-tests.
- [ ] Run existing workflow retry/status self-tests.
- [ ] Run `tools/smoke_mainwindow_startup.py`.
