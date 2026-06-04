

# 📄 Python 模块化 OSS 图片上传 & 签名 URL 生成 SOP

## 1️⃣ 模块功能概述

* 扫描本地指定文件夹里的图片（png/jpg/jpeg/webp）
* 上传到阿里云 OSS 私有 Bucket
* 为每张图片生成指定有效期的签名 URL（HTTPS）
* 批量生成 JSON 输出，供后续视频 API `images` 字段直接使用

**目标**：把 OSS 上传和签名 URL 生成模块独立出来，研发可直接接入现有 API 任务调用链路。

---

## 2️⃣ 前置条件

1. 阿里云 OSS Bucket 已创建

   * ACL 设置为 **私有（private）**
   * 拥有 AccessKeyId / AccessKeySecret 或 RAM 子账号权限
2. Python 3.8+ 环境
3. 安装阿里云 OSS SDK：

```bash
pip install oss2
```

4. 本地待上传图片存放在一个文件夹里

   * 文件名规范：英文或数字，避免空格 / 中文 / 特殊字符

---

## 3️⃣ 配置文件 / 环境变量

建议研发团队把配置信息放在 `config.py` 或环境变量中：

```python
# config.py
OSS_ACCESS_KEY_ID = "你的AccessKeyId"
OSS_ACCESS_KEY_SECRET = "你的AccessKeySecret"
OSS_BUCKET_NAME = "mak-video-assets-20260307"
OSS_ENDPOINT = "oss-cn-hangzhou.aliyuncs.com"  # 根据 Bucket 地域修改
OSS_SIGN_EXPIRE = 3600  # 签名 URL 有效期，秒
LOCAL_IMAGE_DIR = "D:/images"  # 本地待上传图片文件夹
OUTPUT_JSON_PATH = "signed_urls.json"  # 输出文件
```

> ⚠️ 注意：**AccessKeyId/Secret 不要写在前端**，必须由后端模块调用生成 URL。

---

## 4️⃣ 模块化 Python 脚本

```python
# oss_image_uploader.py
import os
import json
import oss2
from config import OSS_ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET, OSS_BUCKET_NAME, OSS_ENDPOINT, OSS_SIGN_EXPIRE, LOCAL_IMAGE_DIR, OUTPUT_JSON_PATH

class OSSUploader:
    def __init__(self, access_key_id, access_key_secret, bucket_name, endpoint, expire_time=3600):
        self.auth = oss2.Auth(access_key_id, access_key_secret)
        self.bucket = oss2.Bucket(self.auth, endpoint, bucket_name)
        self.expire_time = expire_time

    def upload_file(self, local_path, object_name):
        """
        上传本地文件到 OSS
        """
        try:
            self.bucket.put_object_from_file(object_name, local_path)
            print(f"✅ 上传成功: {local_path} → {object_name}")
            return True
        except Exception as e:
            print(f"❌ 上传失败: {local_path} → {object_name}, 错误: {e}")
            return False

    def generate_signed_url(self, object_name):
        """
        生成私有 Bucket 的签名 URL
        """
        try:
            url = self.bucket.sign_url('GET', object_name, self.expire_time)
            return url
        except Exception as e:
            print(f"❌ 生成签名 URL 失败: {object_name}, 错误: {e}")
            return None

    def batch_upload_and_sign(self, local_dir):
        """
        扫描文件夹批量上传并生成签名 URL
        """
        signed_urls = []
        for filename in os.listdir(local_dir):
            if filename.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                local_path = os.path.join(local_dir, filename)
                object_name = filename  # OSS 对象名与文件名一致
                if self.upload_file(local_path, object_name):
                    url = self.generate_signed_url(object_name)
                    if url:
                        signed_urls.append(url)
        return signed_urls

def main():
    uploader = OSSUploader(
        access_key_id=OSS_ACCESS_KEY_ID,
        access_key_secret=OSS_ACCESS_KEY_SECRET,
        bucket_name=OSS_BUCKET_NAME,
        endpoint=OSS_ENDPOINT,
        expire_time=OSS_SIGN_EXPIRE
    )

    print("📂 开始批量上传并生成签名 URL...")
    signed_urls = uploader.batch_upload_and_sign(LOCAL_IMAGE_DIR)

    # 输出 JSON 文件，供后续 API 调用
    output_data = {"images": signed_urls}
    with open(OUTPUT_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    print(f"✅ 完成！签名 URL 已保存到 {OUTPUT_JSON_PATH}")
    print(f"示例 images 数组长度: {len(signed_urls)}")

if __name__ == "__main__":
    main()
```

---

## 5️⃣ 使用说明（研发团队）

### 步骤 1：准备配置

* 修改 `config.py` 的 AccessKey / Bucket / Endpoint / 本地文件夹路径
* 配置签名有效期，例如 3600 秒（1 小时）

### 步骤 2：执行脚本

```bash
python oss_image_uploader.py
```

* 脚本会扫描指定文件夹，上传图片
* 自动生成签名 URL
* 输出 JSON 文件 `signed_urls.json`

### 步骤 3：使用输出的 JSON

* `signed_urls.json` 文件里：

```json
{
  "images": [
    "https://your-bucket.oss-cn-hangzhou.aliyuncs.com/product_001.png?OSSAccessKeyId=xxx&Expires=xxx&Signature=xxx",
    "https://your-bucket.oss-cn-hangzhou.aliyuncs.com/product_002.png?OSSAccessKeyId=xxx&Expires=xxx&Signature=xxx"
  ]
}
```

* 可直接传给你的视频 API 的 `images` 字段

---

## 6️⃣ 注意事项

1. **签名 URL 有效期**

   * 视频 API 调用前必须在有效期内
   * 超过有效期，需要重新生成

2. **安全性**

   * AccessKeyId/Secret 仅后端使用
   * 前端或外部不要暴露密钥

3. **文件命名规范**

   * 英文 + 数字，避免中文/空格/特殊符号
   * 与 OSS 对象名保持一致，保证签名 URL 可访问

4. **异常处理**

   * 上传失败或 URL 生成失败会打印报错
   * 可集成日志系统记录错误，后续自动重试

5. **批量处理**

   * 脚本默认扫描整个文件夹
   * 可拓展支持子文件夹递归

---

## 7️⃣ 接入现有视频 API 的方法

1. 执行本模块，生成 `signed_urls.json`
2. 读取 `images` 数组：

```python
import json
with open("signed_urls.json", "r", encoding="utf-8") as f:
    data = json.load(f)

images = data["images"]
```

3. 调用视频 API：

```python
payload = {
    "model": "sora-2-vip",
    "prompt": "生成美区带货视频",
    "duration": 15,
    "orientation": "portrait",
    "images": images
}
```

