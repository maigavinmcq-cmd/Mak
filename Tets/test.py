import re
import json
import requests

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
    "Mobile/15E148 Safari/604.1"
)

HEADERS = {
    "User-Agent": MOBILE_UA,
    "Referer": "https://www.tiktok.com/",
    # 如果你后面要加登录 Cookie，就直接在这里塞：
    # "Cookie": "msToken=xxx; tt_webid_v2=xxx; ...",
}


def decode_url(u: str | None) -> str | None:
    """把 JSON 里的 \\u002F 这种转义恢复成正常 URL"""
    if not u:
        return None
    # 先把 \\u002F 转成 /
    u = u.replace("\\u002F", "/")
    # 再把多余的反斜杠去掉
    u = u.replace("\\/", "/")
    return u


def find_first_value_by_keys(data, target_keys):
    """
    在任意深度的 dict/list 里，递归查找第一个 key 属于 target_keys 的值
    target_keys 是一个 set，例如 {"playAddr", "downloadAddr"}
    """
    if isinstance(data, dict):
        for k, v in data.items():
            if k in target_keys and isinstance(v, str) and v.startswith("http"):
                return decode_url(v)
            # 继续向下递归
            sub = find_first_value_by_keys(v, target_keys)
            if sub:
                return sub
    elif isinstance(data, list):
        for item in data:
            sub = find_first_value_by_keys(item, target_keys)
            if sub:
                return sub
    return None


def parse_universal_data(data: dict):
    """
    通用解析：不依赖具体路径，直接在整份 JSON 中找想要的 key
    """
    # 1）优先在整个 JSON 里找无水印 / 播放地址
    play_keys = {"playAddr", "playApi", "play_url", "playUrl", "originVideoDownloadAddr"}
    download_keys = {"downloadAddr", "download_url", "downloadUrl"}

    play_url = find_first_value_by_keys(data, play_keys)
    download_url = find_first_value_by_keys(data, download_keys)

    # 2）顺便找一找封面
    cover_keys = {"cover", "originCover", "dynamicCover"}
    cover_url = find_first_value_by_keys(data, cover_keys)

    # 3）尝试找视频 ID（有就用，没有也不致命）
    video_id = None

    def find_video_id(d):
        nonlocal video_id
        if video_id:
            return
        if isinstance(d, dict):
            # 常见字段名：id / videoId / awemeId 等
            if "id" in d and isinstance(d["id"], str) and d["id"].isdigit():
                video_id = d["id"]
            elif "videoId" in d and isinstance(d["videoId"], str):
                video_id = d["videoId"]
            for v in d.values():
                find_video_id(v)
        elif isinstance(d, list):
            for item in d:
                find_video_id(item)

    find_video_id(data)

    return {
        "video_id": video_id,
        "play_url": play_url,
        "download_url": download_url,
        "cover_url": cover_url,
    }


def parse_tiktok_html(html: str):
    """
    优先试 SIGI_STATE，失败再试 UNIVERSAL_DATA，哪个里有就用哪个
    """

    # ① SIGI_STATE
    m = re.search(r'<script id="SIGI_STATE"[^>]*>(.*?)</script>', html)
    if m:
        try:
            js = json.loads(m.group(1))
            print("✅ 找到 SIGI_STATE，尝试从中提取…")
            info = parse_universal_data(js)
            if info.get("play_url") or info.get("download_url"):
                print("✅ 使用 SIGI_STATE 提取成功")
                return info
            else:
                print("SIGI_STATE 里没找到有效直链，继续尝试 UNIVERSAL_DATA")
        except Exception as e:
            print("SIGI_STATE 解析失败：", e)

    # ② __UNIVERSAL_DATA_FOR_REHYDRATION__
    m2 = re.search(
        r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
        html
    )
    if m2:
        try:
            js = json.loads(m2.group(1))
            print("✅ 找到 __UNIVERSAL_DATA_FOR_REHYDRATION__，尝试从中提取…")
            info = parse_universal_data(js)
            if info.get("play_url") or info.get("download_url"):
                print("✅ 使用 UNIVERSAL_DATA 提取成功")
                return info
            else:
                print("UNIVERSAL_DATA 里没找到有效直链")
        except Exception as e:
            print("UNIVERSAL_DATA 解析失败：", e)

    return None


def get_tiktok_video(url: str):
    print("请求页面中…")
    resp = requests.get(url, headers=HEADERS, allow_redirects=True)
    if resp.status_code != 200:
        print(f"❌ 请求失败，HTTP {resp.status_code}")
        return None

    html = resp.text
    info = parse_tiktok_html(html)
    if not info:
        print("❌ 仍然没能从页面 JSON 里解析到视频直链。")
        # 你可以把 html 保存下来手动检查：
        with open("debug_tiktok.html", "w", encoding="utf-8") as f:
             f.write(html)
        return None

    print("\n=== 视频解析结果 ===")
    print("视频 ID:", info.get("video_id"))
    print("封面 URL:", info.get("cover_url"))
    print("\n▶ 优先无水印播放地址（如有）：", info.get("play_url"))
    print("\n▶ 备用下载地址（可能有水印）：", info.get("download_url"))

    return info


if __name__ == "__main__":
    test_url = "https://www.tiktok.com/@xiaowang626/video/7577783617544359175"
    get_tiktok_video(test_url)
