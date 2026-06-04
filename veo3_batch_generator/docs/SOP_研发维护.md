# Veo3 Batch Generator 研发维护 SOP

版本日期：2026-05-11  
适用对象：研发、运维、数据运营支持人员  
工程目录：`veo3_batch_generator`

## 1. 项目定位

`veo3_batch_generator` 是一个 Windows 本地桌面工具，用于按 Excel 批量执行 Veo3 图生视频任务。

核心流程：

1. 从 Excel 读取任务，每行包含 `PID`、网盘路径、图片提示词、视频提示词。
2. 按网盘路径查找产品白底图。
3. 并发调用图生图 API，生成视频首帧图。
4. 图生图完成后，将任务放入独立的视频提交队列，并发提交图生视频任务。
5. 后台轮询视频任务状态。
6. 视频完成后写入视频链接，并异步下载视频。
7. 支持状态恢复、失败重试、结果 Excel 导出、按列筛选、批量下载视频。

当前实现重点：

- 图生图和图生视频是两个独立并发流程。
- 轮询是后台并发流程，点击“立即轮询”会马上执行一轮轮询。
- 视频下载独立于轮询线程，避免下载阻塞轮询。
- 加载 Excel 默认创建全新任务，不继承历史 `task_id`、状态、视频链接等字段。
- 只有用户在启动时明确选择恢复历史任务，才会读取 `state/task_state.json`。

## 2. 目录结构

```text
veo3_batch_generator/
├─ main.py                         # 普通启动入口，优先 PySide6，失败时 fallback 到 Tk
├─ main_qt.py                      # PyInstaller 打包入口，仅使用 PySide6
├─ requirements.txt                # Python 依赖
├─ .env.example                    # 配置模板
├─ Veo3BatchGenerator.spec         # PyInstaller 打包配置
├─ download_videos_by_pid.py       # 从结果 Excel 按 PID 下载视频的辅助脚本
├─ app/
│  ├─ gui.py                       # PySide6 主界面
│  ├─ gui_tk.py                    # Tk fallback 旧界面
│  ├─ worker.py                    # 批量任务并发执行、轮询、下载核心逻辑
│  ├─ config.py                    # 配置读取和运行根目录
│  ├─ excel_loader.py              # Excel 任务读取
│  ├─ task_manager.py              # 状态持久化、统计、结果导出
│  ├─ file_utils.py                # 文件查找、路径生成、下载、打开路径
│  ├─ logger.py                    # 日志初始化
│  ├─ api/
│  │  ├─ image_api.py              # 图生图 API
│  │  ├─ video_api.py              # 图生视频 API 和轮询 API
│  │  ├─ response_parser.py        # API 响应字段兼容解析
│  │  └─ upload_api.py             # 可选图片上传 API
│  └─ models/
│     └─ task.py                   # 任务数据模型和状态枚举
├─ outputs/
│  ├─ images/
│  ├─ videos/
│  ├─ logs/
│  └─ result_excel/
├─ state/
│  ├─ task_state.json
│  └─ task_state.json.bak
└─ dist/
   └─ Veo3BatchGenerator/          # 打包后的发布目录
```

## 3. 运行环境

推荐环境：

- Windows 10/11
- Python 3.10 或以上
- 推荐使用虚拟环境

安装依赖：

```powershell
cd C:\Users\22892\PyCharmMiscProject\veo3_batch_generator
python -m pip install --upgrade pip
pip install -r requirements.txt
```

当前依赖：

```text
PySide6>=6.6
pandas>=2.0
openpyxl>=3.1
requests>=2.31
python-dotenv>=1.0
pydantic>=2.5
```

启动开发版：

```powershell
python main.py
```

如果只验证 Qt 入口：

```powershell
python main_qt.py
```

## 4. 配置说明

配置文件：`.env`

可从 `.env.example` 复制：

```powershell
Copy-Item .env.example .env
```

关键配置项：

```env
IMAGE_API_KEY=
VIDEO_API_KEY=

IMAGE_API_BASE_URL=https://YOUR_API_HOST
IMAGE_MODEL=gpt-image-2
IMAGE_SIZE=1024x1024

VIDEO_API_BASE_URL=https://xibapi.com
VIDEO_MODEL=veo_3_1-fast-fl
VIDEO_SIZE=1080x1920

BATCH_CONCURRENCY=5
IMAGE_CONCURRENCY=5
VIDEO_SUBMIT_CONCURRENCY=5
POLL_CONCURRENCY=20
DOWNLOAD_CONCURRENCY=3

IMAGE_UPLOAD_API_URL=
IMAGE_UPLOAD_API_KEY=
IMAGE_UPLOAD_FILE_FIELD=file
```

配置读取位置：

- 源码运行时：项目根目录 `.env`
- exe 运行时：exe 所在目录 `.env`

这个行为由 `app/config.py` 的 `_runtime_root()` 控制。打包后不要把 `.env` 放到 `_internal` 目录里，应放在 `Veo3BatchGenerator.exe` 同级目录。

### 并发配置建议

| 配置项 | 作用 | 默认值 | 建议 |
|---|---:|---:|---|
| `BATCH_CONCURRENCY` | GUI 中“并发数量”的基础值 | 5 | 常规 5 到 10 |
| `IMAGE_CONCURRENCY` | 图生图并发 | 5 | 受图生图 API 限流影响 |
| `VIDEO_SUBMIT_CONCURRENCY` | 图生视频提交并发 | 5 | 受视频 API 限流影响 |
| `POLL_CONCURRENCY` | 轮询并发 | 20 | 可高于提交并发 |
| `DOWNLOAD_CONCURRENCY` | 下载并发 | 3 | 网络盘写入慢时不要太高 |

GUI 中修改“并发数量”时，会同步影响图生图、视频提交、轮询和下载并发：

- `image_concurrency = concurrency`
- `video_submit_concurrency = concurrency`
- `poll_concurrency = max(concurrency, 20)`
- `download_concurrency = min(max(concurrency, 1), 5)`

## 5. Excel 输入规范

优先按表头读取以下字段：

```text
PID
网盘路径
图片提示词
视频提示词
```

如果表头不匹配，程序会按前四列读取：

```text
第 1 列：PID
第 2 列：网盘路径
第 3 列：图片提示词
第 4 列：视频提示词
```

空行过滤规则：

- `PID`
- `网盘路径`
- `图片提示词`
- `视频提示词`

以上四项全部为空时，该行不会进入任务列表。

重要行为：

- 点击“加载任务”会从 Excel 创建全新任务。
- 不会继承历史状态、旧 `video_task_id`、旧视频链接、旧生成图片路径。
- 如需继续历史任务，必须在程序启动时选择“恢复任务”。

## 6. 网盘目录规范

每条任务的 `网盘路径` 下要求存在产品图目录：

```text
{网盘路径}\01.产品白底图\
```

支持图片格式：

```text
.png
.jpg
.jpeg
.webp
.bmp
```

生成首帧图默认保存：

```text
{网盘路径}\03.VEO首帧图片\
```

视频默认保存：

```text
{网盘路径}\03.Veo3视频\
```

如果网盘目录不可写或创建失败，程序会回退到项目输出目录：

```text
outputs/images/{PID}/
outputs/videos/{PID}/
```

## 7. GUI 操作 SOP

### 7.1 首次启动

1. 确认 `.env` 已配置 API Key 和 API 地址。
2. 运行 `python main.py` 或双击 exe。
3. 如弹出“恢复任务”：
   - 需要继续上次未完成任务时，选择“是”。
   - 需要从 Excel 重新开始时，选择“否”。

### 7.2 加载全新任务

1. 选择任务 Excel。
2. 选择输出目录。
3. 设置并发数量、失败重试次数、轮询间隔、最大轮询次数。
4. 点击“加载任务”。
5. 表格会显示全新任务状态，默认应为 `PENDING` 或待处理状态。

注意：加载 Excel 不会读取历史状态。

### 7.3 检查任务

点击“检查任务”会并发检查每条任务的产品白底图是否存在。

常见结果：

- 找到图片：产品图状态显示“已找到”。
- 未找到目录：`SKIPPED_NO_PRODUCT_IMAGE_FOLDER`
- 目录存在但无支持图片：`SKIPPED_NO_PRODUCT_IMAGE`

### 7.4 开始执行

点击“开始执行”后，程序进入并发生成流程：

1. 并发查找产品图。
2. 并发调用图生图 API。
3. 图生图完成后，任务进入视频提交队列。
4. 视频提交成功后写入 `video_task_id`。
5. 后台轮询线程持续查询视频结果。
6. 视频完成后写入 `video_url`。
7. 下载线程池异步下载视频。

### 7.5 立即轮询

点击“立即轮询”：

- 如果当前有 worker 正在运行，会触发后台轮询线程立即执行一轮。
- 如果当前没有 worker，会启动一次 `poll_only` 模式，只轮询已有 `video_task_id` 的任务。
- 轮询结果会直接写入 GUI 日志。

### 7.6 暂停、继续、停止

- 暂停：停止提交新任务和轮询，但不会杀掉已经发出的 API 请求。
- 继续：恢复提交和轮询。
- 停止：停止继续提交新任务，保留已获得的 `video_task_id`。下次恢复任务后可继续轮询。

### 7.7 失败重试

点击“重新执行失败任务”会重新处理失败任务。

对于视频 API 失败、视频失败、视频超时任务，程序会清空：

- `video_task_id`
- `video_url`
- `video_file_path`
- `video_poll_count`

然后重新进入提交流程。

### 7.8 表格筛选

GUI 支持 Excel 表格筛选：

- 选择“全部列”时，对所有列做部分匹配。
- 选择单列时，只筛选该列。
- 筛选仅影响表格显示，不影响任务数据。

## 8. 任务状态说明

任务状态定义在 `app/models/task.py`。

| 状态 | 含义 |
|---|---|
| `PENDING` | 待处理 |
| `CHECKING_PRODUCT_IMAGE` | 正在检查产品图 |
| `GENERATING_IMAGE` | 正在图生图 |
| `IMAGE_DONE` | 图生图完成 |
| `VIDEO_SUBMITTING` | 正在提交视频任务 |
| `VIDEO_SUBMITTED` | 视频任务已提交，已有 `video_task_id` |
| `VIDEO_POLLING` | 正在轮询视频结果 |
| `COMPLETED` | 视频链接已生成 |
| `VIDEO_DOWNLOADED` | 视频已下载到本地 |
| `VIDEO_TIMEOUT` | 超过最大轮询次数 |
| `FAILED_IMAGE_API` | 图生图 API 失败 |
| `FAILED_VIDEO_API` | 视频 API 失败 |
| `FAILED_UNKNOWN` | 未知异常 |
| `SKIPPED_NO_PRODUCT_IMAGE_FOLDER` | 缺少产品图目录 |
| `SKIPPED_NO_PRODUCT_IMAGE` | 产品图目录下没有支持的图片 |
| `SKIPPED_EMPTY_PROMPT` | 图片提示词或视频提示词为空 |

中断恢复时，`TaskManager.normalize_interrupted_states()` 会将运行中状态归一化：

- 图生图中断：有生成图则转为 `IMAGE_DONE`，否则回到 `PENDING`。
- 视频提交中断：有 `video_task_id` 则转为 `VIDEO_SUBMITTED`。
- 轮询中断：有 `video_task_id` 且无视频链接则转为 `VIDEO_SUBMITTED`。
- 已有视频链接则转为 `COMPLETED`。

## 9. 状态文件和断点续跑

状态文件：

```text
state/task_state.json
state/task_state.json.bak
```

状态保存策略：

- 使用紧凑 JSON，减少磁盘写入体积。
- `image_raw_response` 和 `video_raw_response` 不落盘。
- `generated_image_url` 如果是 `data:image...`，不落盘。
- 每条任务日志只保留最近 50 条。
- 常规状态保存有节流，终态或关键节点强制保存。
- `.bak` 备份默认最多 60 秒更新一次，强制保存时会更新。

维护注意：

- 不要手工编辑正在运行中的 `task_state.json`。
- 如果状态文件损坏，程序会尝试读取 `.bak`。
- 如果要彻底重新开始，先退出程序，再备份并移走 `state/task_state.json` 和 `.bak`。
- 加载 Excel 默认不会合并旧状态，所以普通重新加载不需要删除状态文件。

## 10. API 维护说明

### 10.1 图生图 API

实现文件：

```text
app/api/image_api.py
```

请求方式：

```text
POST {IMAGE_API_BASE_URL}/v1/images/generations
Content-Type: application/json
Authorization: Bearer {IMAGE_API_KEY}
```

请求字段：

```json
{
  "model": "gpt-image-2",
  "prompt": "图片提示词",
  "size": "1024x1024",
  "image": ["data:image/png;base64,..."]
}
```

响应兼容字段由 `app/api/response_parser.py` 解析，支持多种返回结构：

- `b64`
- `b64_json`
- `base64`
- `image_base64`
- `url`
- `image_url`
- `data[0]`

如果返回 base64，会保存为本地图片。  
如果返回图片 URL，会尝试下载到本地。

### 10.2 图生视频 API

实现文件：

```text
app/api/video_api.py
```

提交视频：

```text
POST {VIDEO_API_BASE_URL}/v1/videos
Content-Type: application/json
Authorization: Bearer {VIDEO_API_KEY}
```

请求字段：

```json
{
  "model": "veo_3_1-fast-fl",
  "prompt": "视频提示词",
  "size": "1080x1920",
  "images": ["data:image/png;base64,..."]
}
```

轮询视频：

```text
GET {VIDEO_API_BASE_URL}/v1/videos/{task_id}
Authorization: Bearer {VIDEO_API_KEY}
```

轮询逻辑：

- `status == completed` 且能解析到 `video_url`，任务完成。
- `status == failed`，写入 `FAILED_VIDEO_API`。
- 网络超时、HTTP 429、500、502、503、504 会按可恢复状态处理，后续继续轮询。
- 可重试失败会尝试重新提交视频任务。

## 11. 并发架构

核心文件：

```text
app/worker.py
```

### 11.1 主执行流程

`BatchWorker.run()` 做三件事：

1. 创建视频下载线程池。
2. 启动后台轮询线程。
3. 运行图生图和视频提交双阶段 pipeline。

### 11.2 图生图阶段

入口：

```text
_run_image_stage()
```

职责：

- 检查产品图。
- 判断提示词是否为空。
- 判断是否复用已有生成图。
- 调用图生图 API。
- 图生图成功后返回任务对象，交给视频提交队列。

### 11.3 视频提交阶段

入口：

```text
_run_video_submit_stage()
```

职责：

- 读取生成图路径或生成图 URL。
- 调用视频提交 API。
- 写入 `video_task_id`。
- 设置 `VIDEO_SUBMITTED`。
- 触发立即轮询事件。

### 11.4 后台轮询阶段

入口：

```text
_poll_loop()
_poll_cycle()
_poll_one()
```

职责：

- 周期性扫描 `VIDEO_SUBMITTED` 和 `VIDEO_POLLING` 任务。
- 并发调用查询接口。
- 成功后写入 `video_url`。
- 失败时写入错误状态。
- 完成后将下载任务放入下载线程池。

### 11.5 下载阶段

入口：

```text
_queue_completed_video_download()
_download_task_and_emit()
```

职责：

- 视频完成后异步下载。
- 下载成功则状态为 `VIDEO_DOWNLOADED`。
- 下载失败时保留 `video_url`，任务仍可人工或批量下载。

## 12. 输出文件说明

日志目录：

```text
outputs/logs/run_YYYYMMDD_HHMMSS.log
```

导出结果：

```text
outputs/result_excel/Veo3视频生成结果_YYYYMMDD_HHMMSS.xlsx
```

结果字段包括：

- PID
- 网盘路径
- 图片提示词
- 视频提示词
- 产品白底图路径
- 生成图片路径
- 生成图片 URL
- 图生图任务 ID
- 视频任务 ID
- 视频提交时间
- 视频轮询开始时间
- 视频轮询结束时间
- 视频轮询次数
- 视频链接
- 视频本地路径
- 任务状态
- 错误信息
- 开始时间
- 结束时间
- 耗时秒数

## 13. 视频结果下载和分发辅助流程

当前工程包含辅助脚本：

```text
download_videos_by_pid.py
```

用途：

- 从结果 Excel 中读取视频链接。
- 按 PID 创建目录。
- 下载视频到本地。

注意：

- 视频 CDN 链接可能有过期时间。
- 如果链接已过期，优先使用结果 Excel 中的“视频本地路径”列，从网盘或本地已有文件复制。
- 给多人分发素材时，建议按 PID 整组分配，不要拆散同一个 PID 下的视频。

最近一次手工分发采用策略：

- 源目录：`C:\Users\22892\Downloads\Veo3_videos_by_PID_20260511`
- 7 份输出目录：`C:\Users\22892\Downloads\Veo3_videos_split_7_20260511`
- 分配原则：按 PID 整组分配，尽量平衡视频数量和总大小。
- 本机同盘分发目录可用硬链接节省空间，跨机器拷贝时会变成真实文件。

如需产品化这个分发流程，建议新增独立脚本：

```text
scripts/split_videos_by_pid.py
```

参数建议：

```powershell
python scripts/split_videos_by_pid.py --source-dir <按PID下载目录> --parts 7 --output-dir <分发目录>
```

## 14. 打包发布 SOP

使用 PyInstaller 打包。

打包入口：

```text
main_qt.py
```

打包配置：

```text
Veo3BatchGenerator.spec
```

执行命令：

```powershell
cd C:\Users\22892\PyCharmMiscProject\veo3_batch_generator
python -m PyInstaller --noconfirm --clean Veo3BatchGenerator.spec
```

输出目录：

```text
dist\Veo3BatchGenerator\
```

发布时需要拷贝整个目录，不要只拷贝 exe：

```text
dist\Veo3BatchGenerator\
├─ Veo3BatchGenerator.exe
├─ _internal\
├─ .env
├─ .env.example
└─ README_RUN.txt
```

发布 zip：

```powershell
Compress-Archive -Path dist\Veo3BatchGenerator -DestinationPath dist\Veo3BatchGenerator_windows.zip -Force
```

打包注意事项：

- `main.py` 有 Tk fallback，打包时会带入更多不需要的依赖，所以生产包使用 `main_qt.py`。
- `Veo3BatchGenerator.spec` 排除了 torch、scipy、matplotlib、tensorflow 等无关可选依赖，避免包体过大和构建超时。
- `.env` 必须放在 exe 同级目录。
- exe 运行根目录由 `app/config.py` 的 `_runtime_root()` 决定。

打包后冒烟测试：

```powershell
$p = Start-Process -FilePath "dist\Veo3BatchGenerator\Veo3BatchGenerator.exe" -WorkingDirectory "dist\Veo3BatchGenerator" -PassThru
Start-Sleep -Seconds 8
if ($p.HasExited) { "EXITED $($p.ExitCode)" } else { "RUNNING $($p.Id)"; Stop-Process -Id $p.Id -Force }
```

期望：

```text
RUNNING <pid>
```

## 15. 测试和验证 SOP

### 15.1 语法检查

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python -m py_compile main.py main_qt.py app\worker.py app\gui.py app\config.py app\task_manager.py app\file_utils.py app\api\video_api.py app\api\image_api.py app\excel_loader.py
```

### 15.2 状态文件检查

检查状态文件是否能解析：

```powershell
python -c "import json; data=json.load(open('state/task_state.json',encoding='utf-8')); print(len(data.get('tasks',[])))"
```

检查是否有异常大字符串：

```powershell
python -c "import json; data=json.load(open('state/task_state.json',encoding='utf-8')); print(max((len(v) for t in data.get('tasks',[]) for v in t.values() if isinstance(v,str)), default=0))"
```

正常情况下，不应再出现超大 base64 字符串。

### 15.3 全新加载任务验证

关键预期：

- 加载 Excel 后，新任务状态应为 `PENDING`。
- 不应继承旧 `video_task_id`。
- 不应继承旧 `video_url`。

可用最小脚本验证 `TaskManager.set_tasks()`：

```powershell
python -c "from app.task_manager import TaskManager; from app.models.task import TaskItem, TaskStatus; from pathlib import Path; p=Path('state/test_fresh_state.json'); m=TaskManager(p); old=TaskItem(row_index=2,pid='A',netdisk_path='x',image_prompt='i',video_prompt='v'); old.video_task_id='old_id'; old.status=TaskStatus.VIDEO_SUBMITTED; m.tasks=[old]; new=TaskItem(row_index=2,pid='A',netdisk_path='x',image_prompt='i2',video_prompt='v2'); m.set_tasks([new]); print(m.tasks[0].status, m.tasks[0].video_task_id, m.tasks[0].image_prompt); p.unlink(missing_ok=True); p.with_suffix('.json.bak').unlink(missing_ok=True)"
```

期望输出：

```text
PENDING None i2
```

## 16. 常见故障处理

### 16.1 API Key 为空

现象：

```text
API Key 为空
```

处理：

- 检查 `.env` 是否在正确目录。
- 源码运行时放项目根目录。
- exe 运行时放 exe 同级目录。
- 检查 `IMAGE_API_KEY` 和 `VIDEO_API_KEY`。

### 16.2 图生图 API 地址未配置

现象：

```text
IMAGE_API_BASE_URL 未配置真实域名
```

处理：

- 在 `.env` 设置真实的 `IMAGE_API_BASE_URL`。
- 不要保留 `https://YOUR_API_HOST`。

### 16.3 产品图目录不存在

状态：

```text
SKIPPED_NO_PRODUCT_IMAGE_FOLDER
```

处理：

- 检查 Excel `网盘路径` 是否正确。
- 检查路径下是否存在 `01.产品白底图`。
- 检查当前机器是否有网络盘访问权限。

### 16.4 产品图目录为空

状态：

```text
SKIPPED_NO_PRODUCT_IMAGE
```

处理：

- 放入 `.png`、`.jpg`、`.jpeg`、`.webp` 或 `.bmp` 文件。
- 检查文件是否为 0 字节。

### 16.5 视频轮询超时

状态：

```text
VIDEO_TIMEOUT
```

处理：

- 增大“最大轮询次数”。
- 检查视频 API 后台是否仍在生成。
- 对超时任务执行“重新执行失败任务”。

### 16.6 视频链接下载失败

可能原因：

- CDN 链接过期。
- 网络不通。
- 目标网盘目录无写权限。

处理：

- 如果 `video_url` 存在，可以尝试立即下载。
- 如果链接过期，需重新轮询或重新生成。
- 如果结果 Excel 中已有“视频本地路径”，可从该路径复制归档。

### 16.7 状态文件过大

历史问题：

- 曾经将 API 原始响应或 `data:image` 写入 state，导致 `task_state.json` 变成数百 MB。

当前策略：

- `TaskManager._compact_task_dump()` 会清理大字段。

处理：

- 退出程序。
- 备份旧 `state/task_state.json`。
- 重新启动并恢复一次，程序会按当前压缩策略重写 state。

## 17. 研发维护注意事项

### 17.1 修改并发流程时

重点检查：

- `BatchWorker.run()`
- `_run_pipeline()`
- `_run_image_stage()`
- `_run_video_submit_stage()`
- `_poll_cycle()`
- `_queue_completed_video_download()`
- `_save_emit()`

原则：

- 不要让下载阻塞轮询线程。
- 不要让视频提交等待全部图生图完成。
- 状态更新要通过 `_set_status()` 或 `_save_emit()`，避免 GUI 和 state 不一致。
- 终态、拿到 `video_task_id`、拿到 `video_url` 时要强制保存。

### 17.2 修改状态字段时

需要同步检查：

- `app/models/task.py`
- `app/task_manager.py`
- `app/gui.py` 表格列
- `export_excel()` 导出字段
- 历史 state 兼容性

### 17.3 修改 Excel 字段时

需要同步检查：

- `app/excel_loader.py`
- GUI 表头 `TABLE_HEADERS`
- `TaskManager.export_excel()`
- 辅助下载脚本里的列名

### 17.4 修改打包配置时

需要同步检查：

- `main_qt.py`
- `Veo3BatchGenerator.spec`
- `.env` 是否复制到发布目录
- `dist\Veo3BatchGenerator\README_RUN.txt`
- 打包后冒烟测试

### 17.5 编码注意事项

项目中部分历史文件曾出现中文乱码。后续维护建议：

- 统一使用 UTF-8 保存源文件。
- PowerShell 查看中文文件时注意控制台编码。
- 不要用错误编码批量重写 Python 源码。
- 修改中文 UI 文案后必须运行 `py_compile`。

## 18. 交接清单

研发接手前请确认：

- [ ] 能在本机安装依赖并运行 `python main.py`
- [ ] `.env` 中 API Key 和 API 地址有效
- [ ] 测试 Excel 能正常加载
- [ ] 产品图目录规范已确认
- [ ] 图生图 API 可返回图片
- [ ] 视频 API 可返回 `task_id`
- [ ] 轮询能拿到 `video_url`
- [ ] 视频能下载到目标目录
- [ ] 状态恢复流程已验证
- [ ] 结果 Excel 可导出
- [ ] PyInstaller 可重新打包
- [ ] 打包后的 exe 可在目标电脑启动

## 19. 推荐后续优化

1. 将 `download_videos_by_pid.py` 升级为正式 `scripts/` 工具，支持从“视频本地路径”复制和从 URL 下载两种模式。
2. 新增 `scripts/split_videos_by_pid.py`，把素材分发功能产品化。
3. 给 API 层补充单元测试，覆盖常见响应结构。
4. 给 `TaskManager` 补历史 state 兼容测试。
5. 将 GUI 中并发配置拆成四个独立输入项，让运营可分别调图生图、视频提交、轮询、下载并发。
6. 为失败任务增加失败原因分类统计。
7. 增加“只轮询选中任务”能力，方便小批量补救。
