from yt_dlp import YoutubeDL

def get_tiktok_mp4_direct_url(page_url: str, proxy: str | None = None):
    """
    从 TikTok 页面 URL 解析出「带视频+音频的 mp4 直链」优先版本。

    :param page_url: 例如 https://www.tiktok.com/@user/video/7577783617544359175
    :param proxy: 例如 "socks5://127.0.0.1:10808" 或 "http://127.0.0.1:7890"，没有就传 None
    :return: (best_mp4_url, all_mp4_formats)
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        # 这里 format 写什么无所谓，因为我们手动从 formats 里挑
        "format": "best",
    }

    if proxy:
        ydl_opts["proxy"] = proxy

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(page_url, download=False)

    formats = info.get("formats", []) or []

    mp4_candidates = []
    for f in formats:
        url = f.get("url")
        if not url:
            continue

        ext = f.get("ext")
        vcodec = (f.get("vcodec") or "").lower()
        acodec = (f.get("acodec") or "").lower()
        protocol = f.get("protocol")

        # ✅ 过滤条件：
        # 1）mp4 容器
        # 2）既有视频又有音频（vcodec、acodec 都不是 none）
        # 3）排除 m3u8 等分片协议，只要真正的文件直链
        if ext == "mp4" and vcodec != "none" and acodec != "none" and protocol not in ("m3u8_native", "m3u8"):
            mp4_candidates.append(f)

    if not mp4_candidates:
        # 兜底：如果实在没有，就退一步随便给一个 best 的 URL
        return info.get("url"), []

    # 在候选 mp4 里，按分辨率 / 码率 / 文件大小综合选一个“最好”的
    def sort_key(f):
        height = f.get("height") or 0
        tbr = f.get("tbr") or 0
        filesize = f.get("filesize") or f.get("filesize_approx") or 0
        return (height, tbr, filesize)

    mp4_candidates.sort(key=sort_key, reverse=True)
    best_mp4 = mp4_candidates[0]
    best_mp4_url = best_mp4["url"]

    # 把所有候选 mp4 的信息也一并返回，方便你调试 / 显示
    simple_list = []
    for f in mp4_candidates:
        simple_list.append({
            "format_id": f.get("format_id"),
            "height": f.get("height"),
            "tbr": f.get("tbr"),
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "url": f.get("url"),
        })

    return best_mp4_url, simple_list


if __name__ == "__main__":
    url = "https://www.tiktok.com/@xiaowang626/video/7577783617544359175"

    # 如果你有代理就填，没有就 None
    proxy = None
    # proxy = "socks5://127.0.0.1:10808"

    best_url, candidates = get_tiktok_mp4_direct_url(url, proxy=proxy)

    print("✅ 选出来的 mp4 直链：")
    print(best_url)
    print()

    print("其它 mp4 清晰度候选：")
    for c in candidates:
        print(c["height"], "p", c["tbr"], "kbps", c["url"][:80] + "...")
