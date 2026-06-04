import base64
import json
import yaml

def decode_vmess_link(link):
    if not link.startswith("vmess://"):
        return None
    base64_data = link[8:]
    try:
        padding = '=' * (-len(base64_data) % 4)  # 修正 Base64 长度
        json_data = base64.b64decode(base64_data + padding).decode('utf-8')
        vmess = json.loads(json_data)

        return {
            "name": vmess.get("ps", vmess["add"] + ":" + vmess["port"]),
            "type": "vmess",
            "server": vmess["add"],
            "port": int(vmess["port"]),
            "uuid": vmess["id"],
            "alterId": int(vmess.get("aid", 0)),
            "cipher": vmess.get("scy", "auto"),
            "tls": True if vmess.get("tls", "").lower() == "tls" else False,
            "network": vmess.get("net", "tcp"),
        }
    except Exception as e:
        print(f"解析失败：{e}")
        return None

def batch_convert_vmess_to_yaml(input_file, output_file="output_clash.yaml"):
    proxies = []

    with open(input_file, "r", encoding="utf-8") as f:
        lines = f.readlines()

    for line in lines:
        link = line.strip()
        if link:
            proxy = decode_vmess_link(link)
            if proxy:
                proxies.append(proxy)

    config = {
        "proxies": proxies,
        "proxy-groups": [
            {
                "name": "🚀 节点选择",
                "type": "select",
                "proxies": [p["name"] for p in proxies] + ["DIRECT"]
            }
        ],
        "rules": [
            "MATCH,🚀 节点选择"
        ]
    }

    with open(output_file, "w", encoding="utf-8") as f:
        yaml.dump(config, f, sort_keys=False, allow_unicode=True)

    print(f"✅ 已生成 Clash 配置文件：{output_file}")

# 示例用法
# 在当前目录创建一个 vmess_links.txt，把链接粘进去，每行一个
# 然后运行：
if __name__ == "__main__":
    batch_convert_vmess_to_yaml("C:\\Users\\22892\\Desktop\\生成文件\\vmess_links.txt")
