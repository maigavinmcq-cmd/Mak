import json
from pathlib import Path

LICENSE_SECRET = "CHANGE_ME_TO_A_LONG_RANDOM_SECRET"  # 这里要和主程序一致

def canonical(core: dict) -> str:
    return json.dumps(core, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

def sign(payload: str) -> str:
    import hmac, hashlib
    return hmac.new(LICENSE_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()

# ✅ 1) 尝试在脚本同目录找 license.json
p = Path(__file__).resolve().parent / "license.json"

print("Looking for:", p)
if not p.exists():
    raise SystemExit("❌ license.json 不存在。请在脚本同目录创建/放置 license.json")

raw = p.read_text(encoding="utf-8").strip()
print("license.json bytes:", len(raw))

if not raw:
    raise SystemExit("❌ license.json 是空文件。请填入 JSON 内容后再运行。")

try:
    lic = json.loads(raw)
except Exception as e:
    print("----- license.json content start -----")
    print(raw)
    print("----- license.json content end -----")
    raise SystemExit(f"❌ license.json 不是合法 JSON：{e}")

core = {
    "app": lic.get("app", ""),
    "expires_utc": lic.get("expires_utc", ""),
    "allowed_machine_ids": lic.get("allowed_machine_ids", []),
    "allowed_users": lic.get("allowed_users", []),
}

payload = canonical(core)
lic["signature"] = sign(payload)

p.write_text(json.dumps(lic, ensure_ascii=False, indent=2), encoding="utf-8")
print("✅ signature updated:", lic["signature"])
