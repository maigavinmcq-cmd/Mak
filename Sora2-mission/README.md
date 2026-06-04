# 方案A：模块化版本（图生视频 GUI）

## 运行
```bash
pip install -r requirements.txt
python main.py
```

## 说明
- 加密配置：默认使用 `cryptography` 保存到 `config.enc`；如果没装 cryptography，会自动退化到明文 `config.json`
- 任务持久化：`tasks.json`
- 账单流水：`logs/billing_ledger.jsonl`
- 批量下载去重索引：`<Download Root>/download_index.json`
