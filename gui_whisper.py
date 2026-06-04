import whisper
import os
import shutil

# 模型名称，可选值："tiny", "base", "small", "medium", "large"
model_name = "medium"

# 下载模型到默认缓存目录（~/.cache/whisper）
print(f"正在下载 Whisper 模型：{model_name} ...")
model = whisper.load_model(model_name)
print("下载完成。")

# 获取默认缓存路径
cache_dir = os.path.expanduser("~/.cache/whisper")
model_filename = f"{model_name}.pt"
model_src_path = os.path.join(cache_dir, model_filename)

# 目标路径（你打包项目中的 models 文件夹）
target_dir = os.path.join(os.path.dirname(__file__), "models")
os.makedirs(target_dir, exist_ok=True)
model_dst_path = os.path.join(target_dir, model_filename)

# 复制模型文件
shutil.copy2(model_src_path, model_dst_path)
print(f"模型已保存到：{model_dst_path}")
