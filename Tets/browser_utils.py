import webview

def open_url_in_app(url: str):
    """
    在内置浏览器中打开网页（带 JS，TikTok 视频可播放）
    """
    window = webview.create_window("浏览器", url, width=1000, height=700)
    webview.start()

def open_in_default_browser(url: str):
    """
    用系统默认浏览器打开
    """
    import webbrowser
    webbrowser.open(url, new=2)
