# Veo3 Batch Generator

Veo3 Batch Generator 是一个用于批量执行“产品白底图 -> 图生图 -> 图生视频 -> 后台轮询 -> 下载结果 -> 导出 Excel”的 GUI 工具。

启动方式保持不变：

```bash
python main.py
```

## 核心流程

1. 从 Excel 读取任务，按 PID 归类展示。
2. 检查网盘路径下的 `01.产品白底图`。
3. 并发执行图生图。
4. 图生图完成后并发提交图生视频任务。
5. 视频 `task_id` 提交后立即处理下一条任务，不等待视频完成。
6. 后台异步轮询已有 `video_task_id`。
7. 成功后按配置自动下载视频，并导出结果 Excel。
8. 支持暂停、继续、停止、失败重试、断点续跑。

## API 平台和模型

所有平台和模型都从 Provider Registry 读取，不在 GUI 控件里硬编码。

图生图当前注册：

- `xibapi_gpt_image2`
  - `gpt_image_2` -> GPT Image 2 -> `gpt-image-2`
  - `gpt_image_1` -> GPT Image 1
- `xibapi_nano_banana`
  - `nano_banana_2` -> Nano Banana 2 标准版 -> `nano_banana_2`
  - `nano_banana_pro` -> Nano Banana Pro -> `nano_banana_pro`
  - `nano_banana_pro_1k` -> Nano Banana Pro 1K -> `nano_banana_pro-1K`
  - `nano_banana_pro_2k` -> Nano Banana Pro 2K -> `nano_banana_pro-2K`
  - `nano_banana_pro_4k` -> Nano Banana Pro 4K -> `nano_banana_pro-4K`

Nano Banana 使用 xibapi 的 `/v1/videos` 图片生成接口，程序会把产品图转换为 Data URL 后放入 `metadata.urls`，提交后自动轮询 `task_id`，完成后下载返回的图片 URL。

如果图生图接口返回 `task_id`，程序会立即把它写入任务状态文件和任务日志。中途断电或程序退出后，下一次执行会优先使用已保存的 `image_task_id` 继续轮询图片结果，避免重复提交造成 token 浪费。该字段默认不显示在任务表中，但会保存在状态和完整导出的结果中。

图生视频当前注册：

- `xibapi_veo`
- `jimmy_veo`

视频模型统一使用逻辑 key：

- `veo_3` -> Veo 3
- `veo_3_fast` -> Veo 3 Fast
- `veo_3_1` -> Veo 3.1
- `veo_3_1_fast` -> Veo 3.1 Fast

不同平台真实传参值由 provider adapter 转换，例如同一个 `veo_3_1_fast` 在不同平台可以映射到不同 `provider_value`。

## GUI 设置保存

所有常用参数都可以在“参数配置”页修改，并保存到：

```text
config/app_config.json
```

可配置内容包括：

- 任务 Excel 默认路径
- 图生图 / 图生视频 API 配置文档路径
- 图生图平台、模型、API Key、Base URL
- 图生视频平台、模型、API Key、Base URL
- 视频默认下载根目录
- 图片任务资料根目录
- 软件日志根目录
- 图生图并发、视频提交并发、轮询并发、下载并发
- 轮询间隔、最大轮询次数、失败重试次数、请求超时
- 是否自动下载视频
- 是否自动保存图生图资料
- 是否按负责人分目录
- 是否启动时恢复上次任务

API Key 不会写入日志或导出 Excel。当前实现允许保存到本地 `config/app_config.json`，请保护好该文件。

## 下载目录规则

默认视频根目录：

```text
\\192.168.1.6\004.短视频运营中心\麦超群\01.Veo3下载视频
```

无负责人时：

```text
视频根目录 / YYYY-MM-DD / PID / PID_row2_xxx.mp4
```

有负责人时：

```text
视频根目录 / 负责人 / YYYY-MM-DD / PID / PID_row2_xxx.mp4
```

Excel 会优先识别 `负责人` 字段，也兼容 `负责人名称`、`执行人`、`分配人`。

## 图片资料规则

默认图片资料根目录：

```text
\\192.168.1.6\004.短视频运营中心\麦超群\01.Veo3任务资料
```

保存结构：

```text
图片资料根目录 / YYYY-MM-DD / PID /
  PID_row2_generated_image.png
  PID_row2_product_image.png
  PID_row2_image_prompt.txt
  PID_row2_video_prompt.txt
  PID_row2_metadata.json
```

## 日志、状态和导出

默认软件日志根目录：

```text
\\192.168.1.6\004.短视频运营中心\麦超群\01.Veo3软件日志
```

保存结构：

```text
软件日志根目录 / YYYY-MM-DD /
  logs/
  states/
  exports/
  configs/
```

结果 Excel 会导出到 `exports` 目录，状态文件保存到 `states` 目录，配置快照保存到 `configs` 目录。

## 预览和快捷操作

任务表点击以下列会显示预览：

- 产品白底图路径
- 生成图片路径
- 视频本地路径
- 视频链接

支持 `png`、`jpg`、`jpeg`、`webp`、`bmp` 图片预览。本地 `mp4` 和远程视频链接会优先尝试内嵌播放，环境不支持时可使用打开按钮。

双击快捷操作：

- 双击 PID：打开 TikTok Shop 商品页
- 双击网盘路径：打开本地文件夹
- 双击图片、视频路径或链接：打开对应文件或 URL

任务表右键菜单支持打开、复制、重置任务、下载单条任务视频、下载单条任务图片资料。

## 多条件筛选

任务队列支持多项筛选。筛选规则是：

- 同一个字段多个值为 OR
- 不同字段之间为 AND

支持字段包括 PID、负责人、任务状态、图生图状态、视频状态、图生图平台、图生图模型、图生视频平台、图生视频模型、批次ID、任务添加日期、是否有视频链接、是否有错误信息。

筛选后会同步显示当前筛选任务数、完成数、失败数、跳过数、轮询中数量、完成百分比和成功率。筛选方案可以保存到 `config/app_config.json`。

## 任务分组

任务队列支持按多个字段组合分组，例如：

- 负责人
- PID
- 负责人 -> PID
- 批次ID -> 负责人 -> PID
- 任务状态 -> 负责人

分组会先应用筛选，再基于筛选后的任务生成。分组标题行支持双击展开/折叠，并显示总任务、完成、失败、跳过、轮询中、完成率和成功率。

分组字段会保存到 `config/app_config.json`：

```json
{
  "enable_group_view": true,
  "group_by_fields": ["负责人", "PID"]
}
```

## 网盘路径 Http 映射

默认启用网盘路径转 Http：

```json
{
  "enable_netdisk_http_mapping": true,
  "netdisk_local_prefix": "\\\\192.168.1.6\\004.短视频运营中心\\01.产品信息\\",
  "netdisk_http_prefix": "https://media.pennitech.top:48443"
}
```

程序会把 Excel 的网盘路径转换成 Http 路径，再拼接：

```text
Http路径 / 01.产品白底图 /
```

白底图查找优先级：

1. Excel 中的 `产品白底图URL`、`白底图URL`、`产品图URL`、`图片URL`
2. Http 目录列表中解析到的第一张图片
3. 如果关闭 Http 映射，则回退本地网盘路径查找

如果 Http 目录无法列出文件且 Excel 没有明确图片 URL，任务会标记为 `SKIPPED_NO_PRODUCT_IMAGE_URL`。

## 字段显示

任务表支持字段隐藏/显示，设置会保存到 `config/app_config.json`。右键菜单或筛选区的“字段显示设置”可打开字段选择窗口。

快捷方案包括：

- 显示全部
- 隐藏全部
- 恢复默认字段
- 只看执行状态
- 只看下载结果
- 只看错误任务

字段隐藏只影响 GUI 展示；默认导出仍保留完整字段。设置页可勾选“导出时仅导出当前显示字段”。

## 不重新编译 exe 的维护方式

路径、平台、模型、API Key、轮询参数、并发参数、下载规则都保存在 `config/app_config.json`。这些参数变化后只需要在 GUI 中保存设置并重启或继续执行后续新任务，不需要重新编译 exe。

只有以下情况通常需要重新打包 exe：

- 修改 Python 代码
- 新增 provider adapter
- 新增依赖库
- 修改图标或内置资源

## 旧状态文件兼容

程序会忽略旧状态中的 `image_task_id`，并为缺失字段补默认值：

- `owner`
- `image_provider`
- `image_model_logical_key`
- `video_provider`
- `video_model_logical_key`
- `task_added_date`
- `batch_date`
- `batch_id`

旧任务不会因为字段变化导致启动失败。

## 手动立即轮询

控制台执行按钮区新增 `手动轮询` 按钮，批次卡片右键菜单新增 `手动轮询该批次`。该功能会立即扫描当前批次中所有已有 `video_task_id`、尚未拿到 `video_url` 的任务，并逐条查询视频结果。

手动轮询和自动后台轮询相互独立：
- 自动轮询仍然遵守 `max_poll_count`
- 手动轮询不受 `max_poll_count` 限制
- `VIDEO_TIMEOUT` 任务可以再次手动轮询
- `FAILED_VIDEO_API` 但仍保留 `video_task_id` 的任务也可以再次手动轮询
- 同一个 `video_task_id` 同一时间只会被一个轮询流程查询，避免自动轮询和手动轮询互相覆盖状态

任务状态文件和结果 Excel 会记录：
- `manual_poll_count`
- `last_manual_poll_time`
- `last_manual_poll_result`

相关配置保存在 `config/app_config.json`：
```json
{
  "enable_manual_poll_button": true,
  "manual_poll_ignore_max_count": true,
  "manual_poll_include_timeout_tasks": true,
  "manual_poll_include_failed_tasks": true
}
```

## 视频下载失败补偿

视频链接通常带签名有效期。自动下载失败时，程序会先按 `retry_count` 和 `retry_interval_seconds` 自动重试；如果默认网盘归档目录下载失败，会尝试保存到本机 `Downloads/Veo3下载视频`。

如果网盘和本机兜底都失败，并且任务仍有 `video_task_id`，程序会把旧视频链接视为可能过期，清空旧链接并把任务放回 `VIDEO_SUBMITTED`，随后自动重新轮询 task_id 获取新的视频链接再下载。

任务状态和导出结果会记录：
- `video_download_status`
- `video_download_attempt_count`
- `last_video_download_time`
- `last_video_download_error`

## 失败任务自动重新入队

任务执行过程中出现 `FAILED_IMAGE_API`、`FAILED_VIDEO_API`、`FAILED_UNKNOWN`、`VIDEO_FAILED`、`VIDEO_TIMEOUT` 等失败状态时，程序会按“失败重试次数”自动清除失败状态并重新进入队列执行。

重试规则：

- 图片阶段失败：清除图片失败状态，重新进入图片生成队列。
- 视频提交失败：清除旧 `video_task_id`、视频链接和下载状态，回到视频提交队列。
- 视频轮询返回失败或超时：清除旧 `video_task_id`，重新提交视频任务。
- 每次自动重试都会写入日志，并记录 `auto_retry_count`、`last_auto_retry_time`、`last_auto_retry_stage`、`last_auto_retry_reason`。

如果自动重试次数用完，任务会保留失败状态。用户点击“重试失败”时，会重新放行这些失败任务再次进入队列。

## 批次隔离和重复导入

每次点击“导入新批次”都会创建全新的 `batch_id`，即使 Excel 文件、PID、网盘路径、图片提示词和视频提示词完全相同，也不会复用历史批次。

批次数据按以下目录隔离：

```text
软件日志根目录 / batches / batch_id /
  batch_info.json
  task_state.json
  run.log
  exports/
```

`batch_index.json` 只会追加新批次或更新同一个 `batch_id` 的统计信息，不会因为导入新批次而清空历史批次列表。程序启动时也会扫描 `batches` 目录，把索引缺失但目录仍存在的批次补回列表。

任务唯一标识为：

```text
batch_id::PID::row_Excel行号
```

默认图片和视频归档路径也包含 `batch_id`，避免同一天重复导入相同任务时覆盖历史文件：

```text
图片任务资料根目录 / YYYY-MM-DD / batch_id / PID /
视频下载根目录 / YYYY-MM-DD / batch_id / PID /
视频下载根目录 / 负责人 / YYYY-MM-DD / batch_id / PID /
```

导出文件默认保存为：

```text
软件日志根目录 / batches / batch_id / exports / Veo3视频生成结果_batch_id_YYYYMMDD_HHMMSS.xlsx
```

可运行以下自测脚本验证重复导入隔离逻辑：

```bash
python tools/self_test_batch_isolation.py
```
