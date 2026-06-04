import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import threading
import requests
import time
from bs4 import BeautifulSoup
import json
import pandas as pd
import os
import subprocess
import socket
from datetime import datetime
import re
import webbrowser
import tkinter.font as tkfont
import platform
import sys
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
import chromedriver_autoinstaller
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import yt_dlp
from concurrent.futures import ThreadPoolExecutor, as_completed

# 自动安装匹配的 ChromeDriver
chromedriver_autoinstaller.install()

# 可选：隐藏浏览器界面（无头模式）
options = Options()
options.add_argument('--headless')  # 如果你想可视化浏览器就注释掉这一行
options.add_argument('--disable-gpu')
options.add_argument('--no-sandbox')

os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)


def is_v2ray_running(host="127.0.0.1", port=10808):
    """检查本地的 V2Ray 代理端口是否开启"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)  # 最多等待1秒
        result = sock.connect_ex((host, port))
        return result == 0


# 在初始化的时候检测
if is_v2ray_running():
    proxies = {
        "http": "socks5h://127.0.0.1:10808",
        "https": "socks5h://127.0.0.1:10808",
    }
else:
    proxies = None  # 不用代理，直连

# 定义缩写和中文对应表
statuscode_mapping = {
    0: "正常",
    10221: "找不到此账号"
}

# 定义缩写和中文对应表
region_mapping = {
    "AD": "安道尔",
    "AE": "阿联酋",
    "AF": "阿富汗",
    "AG": "安提瓜和巴布达",
    "AI": "安圭拉",
    "AL": "阿尔巴尼亚",
    "AM": "亚美尼亚",
    "AO": "安哥拉",
    "AR": "阿根廷",
    "AS": "美属萨摩亚",
    "AT": "奥地利",
    "AU": "澳大利亚",
    "AW": "阿鲁巴",
    "AX": "奥兰群岛",
    "AZ": "阿塞拜疆",
    "BA": "波黑",
    "BB": "巴巴多斯",
    "BD": "孟加拉国",
    "BE": "比利时",
    "BF": "布基纳法索",
    "BG": "保加利亚",
    "BH": "巴林",
    "BI": "布隆迪",
    "BJ": "贝宁",
    "BL": "圣巴泰勒米岛",
    "BM": "百慕大",
    "BN": "文莱",
    "BO": "玻利维亚",
    "BQ": "荷属加勒比区",
    "BR": "巴西",
    "BS": "巴哈马",
    "BT": "不丹",
    "BV": "布韦岛",
    "BW": "博茨瓦纳",
    "BY": "白俄罗斯",
    "BZ": "伯利兹",
    "CA": "加拿大",
    "CC": "科科斯群岛",
    "CD": "刚果（金）",
    "CF": "中非共和国",
    "CG": "刚果（布）",
    "CH": "瑞士",
    "CI": "科特迪瓦",
    "CK": "库克群岛",
    "CL": "智利",
    "CM": "喀麦隆",
    "CN": "中国",
    "CO": "哥伦比亚",
    "CR": "哥斯达黎加",
    "CU": "古巴",
    "CV": "佛得角",
    "CW": "库拉索",
    "CX": "圣诞岛",
    "CY": "塞浦路斯",
    "CZ": "捷克",
    "DE": "德国",
    "DJ": "吉布提",
    "DK": "丹麦",
    "DM": "多米尼克",
    "DO": "多米尼加共和国",
    "DZ": "阿尔及利亚",
    "EC": "厄瓜多尔",
    "EE": "爱沙尼亚",
    "EG": "埃及",
    "EH": "西撒哈拉",
    "ER": "厄立特里亚",
    "ES": "西班牙",
    "ET": "埃塞俄比亚",
    "FI": "芬兰",
    "FJ": "斐济",
    "FM": "密克罗尼西亚",
    "FO": "法罗群岛",
    "FR": "法国",
    "GA": "加蓬",
    "GB": "英国",
    "GD": "格林纳达",
    "GE": "格鲁吉亚",
    "GF": "法属圭亚那",
    "GG": "根西岛",
    "GH": "加纳",
    "GI": "直布罗陀",
    "GL": "格陵兰",
    "GM": "冈比亚",
    "GN": "几内亚",
    "GP": "瓜德罗普",
    "GQ": "赤道几内亚",
    "GR": "希腊",
    "GT": "危地马拉",
    "GU": "关岛",
    "GW": "几内亚比绍",
    "GY": "圭亚那",
    "HK": "中国香港",
    "HM": "赫德岛和麦克唐纳群岛",
    "HN": "洪都拉斯",
    "HR": "克罗地亚",
    "HT": "海地",
    "HU": "匈牙利",
    "ID": "印度尼西亚",
    "IE": "爱尔兰",
    "IL": "以色列",
    "IM": "马恩岛",
    "IN": "印度",
    "IO": "英属印度洋领地",
    "IQ": "伊拉克",
    "IR": "伊朗",
    "IS": "冰岛",
    "IT": "意大利",
    "JE": "泽西岛",
    "JM": "牙买加",
    "JO": "约旦",
    "JP": "日本",
    "KE": "肯尼亚",
    "KG": "吉尔吉斯斯坦",
    "KH": "柬埔寨",
    "KI": "基里巴斯",
    "KM": "科摩罗",
    "KN": "圣基茨和尼维斯",
    "KP": "朝鲜",
    "KR": "韩国",
    "KW": "科威特",
    "KY": "开曼群岛",
    "KZ": "哈萨克斯坦",
    "LA": "老挝",
    "LB": "黎巴嫩",
    "LC": "圣卢西亚",
    "LI": "列支敦士登",
    "LK": "斯里兰卡",
    "LR": "利比里亚",
    "LS": "莱索托",
    "LT": "立陶宛",
    "LU": "卢森堡",
    "LV": "拉脱维亚",
    "LY": "利比亚",
    "MA": "摩洛哥",
    "MC": "摩纳哥",
    "MD": "摩尔多瓦",
    "ME": "黑山",
    "MF": "法属圣马丁",
    "MG": "马达加斯加",
    "MH": "马绍尔群岛",
    "MK": "北马其顿",
    "ML": "马里",
    "MM": "缅甸",
    "MN": "蒙古",
    "MO": "中国澳门",
    "MP": "北马里亚纳群岛",
    "MQ": "马提尼克",
    "MR": "毛里塔尼亚",
    "MS": "蒙特塞拉特",
    "MT": "马耳他",
    "MU": "毛里求斯",
    "MV": "马尔代夫",
    "MW": "马拉维",
    "MX": "墨西哥",
    "MY": "马来西亚",
    "MZ": "莫桑比克",
    "NA": "纳米比亚",
    "NC": "新喀里多尼亚",
    "NE": "尼日尔",
    "NF": "诺福克岛",
    "NG": "尼日利亚",
    "NI": "尼加拉瓜",
    "NL": "荷兰",
    "NO": "挪威",
    "NP": "尼泊尔",
    "NR": "瑙鲁",
    "NU": "纽埃",
    "NZ": "新西兰",
    "OM": "阿曼",
    "PA": "巴拿马",
    "PE": "秘鲁",
    "PF": "法属波利尼西亚",
    "PG": "巴布亚新几内亚",
    "PH": "菲律宾",
    "PK": "巴基斯坦",
    "PL": "波兰",
    "PM": "圣皮埃尔和密克隆",
    "PN": "皮特凯恩群岛",
    "PR": "波多黎各",
    "PT": "葡萄牙",
    "PW": "帕劳",
    "PY": "巴拉圭",
    "QA": "卡塔尔",
    "RE": "留尼旺",
    "RO": "罗马尼亚",
    "RS": "塞尔维亚",
    "RU": "俄罗斯",
    "RW": "卢旺达",
    "SA": "沙特阿拉伯",
    "SB": "所罗门群岛",
    "SC": "塞舌尔",
    "SD": "苏丹",
    "SE": "瑞典",
    "SG": "新加坡",
    "SH": "圣赫勒拿",
    "SI": "斯洛文尼亚",
    "SJ": "斯瓦尔巴和扬马延",
    "SK": "斯洛伐克",
    "SL": "塞拉利昂",
    "SM": "圣马力诺",
    "SN": "塞内加尔",
    "SO": "索马里",
    "SR": "苏里南",
    "SS": "南苏丹",
    "ST": "圣多美和普林西比",
    "SV": "萨尔瓦多",
    "SX": "荷属圣马丁",
    "SY": "叙利亚",
    "SZ": "斯威士兰",
    "TC": "特克斯和凯科斯群岛",
    "TD": "乍得",
    "TF": "法属南部领地",
    "TG": "多哥",
    "TH": "泰国",
    "TJ": "塔吉克斯坦",
    "TK": "托克劳",
    "TL": "东帝汶",
    "TM": "土库曼斯坦",
    "TN": "突尼斯",
    "TO": "汤加",
    "TR": "土耳其",
    "TT": "特立尼达和多巴哥",
    "TV": "图瓦卢",
    "TZ": "坦桑尼亚",
    "UA": "乌克兰",
    "UG": "乌干达",
    "UM": "美国本土外小岛屿",
    "US": "美国",
    "UY": "乌拉圭",
    "UZ": "乌兹别克斯坦",
    "VA": "梵蒂冈",
    "VC": "圣文森特和格林纳丁斯",
    "VE": "委内瑞拉",
    "VG": "英属维京群岛",
    "VI": "美属维京群岛",
    "VN": "越南",
    "VU": "瓦努阿图",
    "WF": "瓦利斯和富图纳",
    "WS": "萨摩亚",
    "YE": "也门",
    "YT": "马约特",
    "ZA": "南非",
    "ZM": "赞比亚",
    "ZW": "津巴布韦"
}


def open_url(url):
    """使用系统默认浏览器打开链接"""
    try:
        webbrowser.open(url, new=2)  # new=2 表示在新标签页中打开
    except Exception as e:
        import traceback
        traceback.print_exc()
        messagebox.showerror("跳转失败", f"无法打开网页。\n错误详情：{e}")


def open_pid_link(event=None):
    pid = pid_entry.get().strip()
    if not pid.isdigit() or len(pid) < 19:
        messagebox.showerror("错误", "请输入正确的PID")
        return
    url = f"https://shop.tiktok.com/view/product/{pid}"
    try:
        open_url(url)
    except Exception as e:
        messagebox.showerror("跳转失败", f"无法打开网页。\n错误详情：{e}")


def open_fastmoss_link(event=None):
    pid = pid_entry.get().strip()
    if not pid.isdigit() or len(pid) < 19:
        messagebox.showerror("错误", "请输入正确的PID")
        return
    url = f"https://www.fastmoss.com/zh/e-commerce/detail/{pid}"
    try:
        open_url(url)
    except Exception as e:
        messagebox.showerror("跳转失败", f"无法打开网页。\n错误详情：{e}")


def open_handle_link(event=None):
    handle = handle_entry.get().strip()
    if not handle:
        messagebox.showerror("错误", "请输入正确的用户handle")
        return
    url = f"https://www.tiktok.com/@{handle}"
    try:
        open_url(url)
    except Exception as e:
        messagebox.showerror("跳转失败", f"无法打开网页。\n错误详情：{e}")


def clear_pid(event=None):
    pid_entry.delete(0, tk.END)


def get_region_name(abbreviation):
    abbreviation = abbreviation.upper()
    return region_mapping.get(abbreviation, "未知地区")


def extract_username(url):
    match = re.search(r'@([^/?]+)', url)
    if match:
        return match.group(1)
    else:
        return None


class TikTokScraperApp:
    def __init__(self, parent):
        self.parent = parent

        font_style = tkfont.Font(family="微软雅黑", size=14, weight="bold")
        tk.Label(parent, text="网页自动跳转功能\n************************************************************************", font=font_style).pack()

        # PID 跳转部分
        pid_frame = tk.Frame(parent)
        pid_frame.pack(pady=5)
        tk.Label(pid_frame, text="输入PID:").pack(side="left")
        global pid_entry
        pid_entry = tk.Entry(pid_frame, width=30)
        pid_entry.pack(side="left", padx=5)

        tiktok_button = tk.Button(pid_frame, text="打开TikTok商品页", command=open_pid_link)
        tiktok_button.pack(side=tk.LEFT, padx=5)

        fastmoss_button = tk.Button(pid_frame, text="打开Fastmoss商品页", command=open_fastmoss_link)
        fastmoss_button.pack(side=tk.LEFT, padx=5)

        clearpid_button = tk.Button(pid_frame, text="清除PID", command=clear_pid)
        clearpid_button.pack(side=tk.LEFT, padx=5)

        # Handle 跳转部分
        handle_frame = tk.Frame(parent)
        handle_frame.pack(pady=5)
        tk.Label(handle_frame, text="输入Handle:").pack(side="left")
        global handle_entry
        handle_entry = tk.Entry(handle_frame, width=30)
        handle_entry.pack(side="left", padx=5)
        handle_entry.bind("<Return>", open_handle_link)
        tk.Button(handle_frame, text="打开TikTok账号主页", command=open_handle_link).pack(side="left")

        # 继续原有功能组件构建
        tk.Label(parent, text="\n\n读取TikTok账号信息功能\n************************************************************************", font=font_style).pack()
        tk.Label(parent, text="请 输入TikTok用户主页链接 或 输入TikTok产品链接 （每行一个）:").pack()
        self.text_urls = scrolledtext.ScrolledText(parent, height=10)
        self.text_urls.pack(fill="x", padx=10, pady=5)

        # 文件选择按钮（可选）
        self.file_path = None

        button_frame = tk.Frame(parent)
        button_frame.pack(pady=10)

        self.btn_account = tk.Button(button_frame, text="开始采集TikTok账号信息", command=self.start_scraping)
        self.btn_account.pack(side="left", padx=5)

        self.btn_product = tk.Button(button_frame, text="开始采集产品信息", command=self.start_scraping_product)
        self.btn_product.pack(side="left", padx=5)

        self.open_button = tk.Button(parent, text="打开结果文件", command=self.open_result_file, state="disabled")
        self.open_button.pack(pady=5)

        self.status_text = tk.StringVar()
        self.status_text.set("空闲中")
        tk.Label(parent, textvariable=self.status_text).pack(pady=5)

        tk.Label(parent, text="采集结果:").pack()
        self.output_box = scrolledtext.ScrolledText(parent, height=10, state="normal")
        self.output_box.pack(fill="both", expand=True, padx=10, pady=5)

        self.save_path = ""

    # 封装两个按钮都禁用的操作
    def disable_buttons(self):
        self.btn_account.config(state="disabled")
        self.btn_product.config(state="disabled")

    # 封装两个按钮都启用的操作
    def enable_buttons(self):
        self.btn_account.config(state="normal")
        self.btn_product.config(state="normal")

    def start_scraping(self):
        self.disable_buttons()
        thread = threading.Thread(target=self.scrape)
        thread.start()

    def start_scraping_product(self):
        self.disable_buttons()
        thread = threading.Thread(target=self.scrape_product)
        thread.start()

    def scrape(self):
        self.set_status("正在采集...")
        self.output_box.config(state="normal")
        self.output_box.delete("1.0", tk.END)
        self.output_box.config(state="disabled")

        # 显示列标题
        self.append_output({
            "链接": "链接",
            "UID": "UID",
            "handle": "handle",
            "昵称": "昵称",
            "状态": "状态",
            "国家地区": "国家地区",
            "粉丝": "粉丝",
            "关注": "关注",
            "视频数量": "视频数量",
            "是否有电商权限": "是否有电商权限",
            "电商账号分类": "电商账号分类"
        })

        urls = []

        manual_input = self.text_urls.get("1.0", tk.END).strip().splitlines()
        urls.extend([u.strip() for u in manual_input if u.strip()])

        if self.file_path:
            with open(self.file_path, 'r', encoding='utf-8') as f:
                file_urls = [line.strip() for line in f if line.strip()]
                urls.extend(file_urls)

        results = []

        for idx, url in enumerate(urls, 1):
            self.set_status(f"正在处理 {idx}/{len(urls)}")
            info = self.extract_user_info_from_url(url)
            if info:
                results.append(info)
                self.append_output(info)

        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        filename = f"user_info_results_{timestamp}.xlsx"

        if self.file_path:
            save_dir = os.path.dirname(self.file_path)
        else:
            save_dir = os.getcwd()

        self.save_path = os.path.join(save_dir, filename)
        df = pd.DataFrame(results)
        df.to_excel(self.save_path, index=False)

        self.set_status(f"✅ 完成！已保存到 {self.save_path}")
        self.open_button.config(state="normal")
        self.enable_buttons()

    def extract_user_info_from_url(self, url):
        try:
            username = extract_username(url)
            if not username:
                return {
                    "链接": url,
                    "UID": "",
                    "handle": "无效的TikTok链接",
                    "昵称": "",
                    "状态": "",
                    "国家地区": "",
                    "粉丝": "",
                    "关注": "",
                    "视频数量": "",
                    "是否有电商权限": "",
                    "电商账号分类": ""
                }

            # 使用 Selenium 获取页面内容
            driver = webdriver.Chrome(options=options)
            driver.get(url)

            wait = WebDriverWait(driver, 15)
            try:
                script_element = wait.until(
                    EC.presence_of_element_located((By.ID, "__UNIVERSAL_DATA_FOR_REHYDRATION__"))
                )
                wait.until(lambda d: "webapp.user-detail" in d.find_element(
                    By.ID, "__UNIVERSAL_DATA_FOR_REHYDRATION__").get_attribute("innerHTML"))
                print("页面加载完成，准备获取信息")
            except Exception:
                print("等待超时，页面未能正常加载")

            html = driver.page_source
            driver.quit()

            soup = BeautifulSoup(html, 'html.parser')

            script_tag = soup.find("script", id="__UNIVERSAL_DATA_FOR_REHYDRATION__")
            if not script_tag:
                return {
                    "链接": url,
                    "UID": "",
                    "handle": "找不到指定的 script 标签 '__UNIVERSAL_DATA_FOR_REHYDRATION__'",
                    "昵称": "",
                    "状态": "",
                    "国家地区": "",
                    "粉丝": "",
                    "关注": "",
                    "视频数量": "",
                    "是否有电商权限": "",
                    "电商账号分类": ""
                }

            json_data = json.loads(script_tag.string)

            scope = json_data.get("__DEFAULT_SCOPE__", {}).get("webapp.user-detail", {})
            statusCode = scope.get("statusCode", {})
            user = scope.get("userInfo", {}).get("user", {})
            stats = scope.get("userInfo", {}).get("stats", {})

            return {
                "链接": url,
                "UID": str(user.get("id", "")),
                "handle": user.get("uniqueId", ""),
                "昵称": user.get("nickname", ""),
                "状态": statuscode_mapping.get(statusCode, str(statusCode)),
                "国家地区": get_region_name(user.get("region", "")),
                "粉丝": stats.get("followerCount", ""),
                "关注": stats.get("followingCount", ""),
                "视频数量": stats.get("videoCount", ""),
                "是否有电商权限": "",
                "电商账号分类": ""
            }

        except Exception as e:
            messagebox.showerror("采集出错", f"采集 URL 时出错：\n{url}\n\n错误详情：\n{str(e)}")
            return None

    def set_status(self, message):
        self.status_text.set(message)

    def append_output(self, info):
        try:
            self.output_box.config(state="normal")
            line = (
                f"{info['链接']}\t"
                f"{info['UID']}\t"
                f"{info['handle']}\t"
                f"{info['昵称']}\t"
                f"{info['状态']}\t"
                f"{info['国家地区']}\t"
                f"{info['粉丝']}\t"
                f"{info['关注']}\t"
                f"{info['视频数量']}\n"
            )
            self.output_box.insert(tk.END, line)
            self.output_box.see(tk.END)
            self.output_box.config(state="disabled")
        except Exception as e:
            messagebox.showerror("追加结果出错", f"追加结果时出错：\n{info.get('handle','')}\n\n错误详情：\n{str(e)}")

    def append_output_pid(self, info):
        try:
            self.output_box.config(state="normal")
            line = (
                f"{info['链接']}\t"
                f"{info['PID']}\t"
                f"{info['店铺名']}\t"
                f"{info['售价']}\t"
                f"{info['评分']}\t"
                f"{info['销量']}\t"
                f"{info['商品描述']}\n"
            )

            self.output_box.insert(tk.END, line)
            self.output_box.see(tk.END)
            self.output_box.config(state="disabled")
        except Exception as e:
            messagebox.showerror("追加结果出错", f"追加结果时出错：\n{info.get('PID','')}\n\n错误详情：\n{str(e)}")

    def open_result_file(self):
        if self.save_path and os.path.exists(self.save_path):
            try:
                os.startfile(self.save_path)
            except Exception as e:
                messagebox.showerror("错误", f"无法打开文件: {e}")

    def extract_pid(self, url):
        match = re.search(r'/view/product/(\d+)', url)
        if match:
            pid = match.group(1)
            return pid
        else:
            return ""

    def extract_product_info_from_url(self, url):
        try:
            if not url.startswith("https://shop.tiktok.com/view/product/"):
                return {
                    "链接": url,
                    "PID": "无效的产品链接",
                    "店铺名": "",
                    "售价": "",
                    "评分": "",
                    "销量": "",
                    "商品描述": ""
                }

            pid = self.extract_pid(url)
            if not pid:
                return {
                    "链接": url,
                    "PID": "",
                    "店铺名": "",
                    "售价": "",
                    "评分": "",
                    "销量": "",
                    "商品描述": ""
                }

            driver = webdriver.Chrome(options=options)
            driver.get(url)

            wait = WebDriverWait(driver, 15)
            try:
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".seller-c27aRQ")))
                wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, ".price__integer-wjkT3b")))
                wait.until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, ".price__decimal-Ta1LoJ")))
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".info__sold-ZdTfzQ")))
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, ".infoRatingScore-jSs6kd")))
                print("页面加载完成，准备获取信息")
            except Exception:
                print("等待超时，页面未能正常加载")

            html = driver.page_source
            driver.quit()
            soup = BeautifulSoup(html, 'html.parser')

            # 店铺名
            try:
                seller_text = soup.select_one('.seller-c27aRQ').text.strip()
                match = re.search(r'由\s+(.*?)\s+销售', seller_text)
                store_name = match.group(1) if match else ""
            except Exception:
                store_name = ""

            # 售价
            try:
                integers = soup.select('.price__integer-wjkT3b')
                decimals = soup.select('.price__decimal-Ta1LoJ')
                if len(integers) >= 2 and len(decimals) >= 2:
                    price1 = f"{integers[0].text}{decimals[0].text}"
                    price2 = f"{integers[1].text}{decimals[1].text}"
                else:
                    price1 = price2 = ""
                price_range = f"{price1}-{price2}" if price1 and price2 else price1 or price2
            except Exception:
                price_range = ""

            # 销量
            try:
                sold_element = soup.select_one('.info__sold-ZdTfzQ')
                if sold_element:
                    sold_text = sold_element.text.strip()
                    match = re.search(r'\d+', sold_text)
                    sold_count = int(match.group()) if match else None
                else:
                    sold_count = None
            except Exception:
                sold_count = None

            # 评分
            try:
                rating_text = soup.select_one('.infoRatingScore-jSs6kd').text.strip()
                rating = float(rating_text)
            except Exception:
                rating = None

            return {
                "链接": url,
                "PID": pid,
                "店铺名": store_name,
                "售价": price_range,
                "评分": rating,
                "销量": sold_count,
                "商品描述": ""
            }

        except Exception as e:
            messagebox.showerror("采集出错", f"采集 URL 时出错：\n{url}\n\n错误详情：\n{str(e)}")
            return None

    def scrape_product(self):
        self.set_status("正在采集...")
        self.output_box.config(state="normal")
        self.output_box.delete("1.0", tk.END)
        self.output_box.config(state="disabled")

        self.append_output_pid({
            "链接": "链接",
            "PID": "PID",
            "店铺名": "店铺名",
            "售价": "售价",
            "评分": "评分",
            "销量": "销量",
            "商品描述": "商品描述"
        })

        urls = []

        manual_input = self.text_urls.get("1.0", tk.END).strip().splitlines()
        urls.extend([u.strip() for u in manual_input if u.strip()])

        if self.file_path:
            with open(self.file_path, 'r', encoding='utf-8') as f:
                file_urls = [line.strip() for line in f if line.strip()]
                urls.extend(file_urls)

        results = []

        for idx, url in enumerate(urls, 1):
            self.set_status(f"正在处理 {idx}/{len(urls)}")
            info = self.extract_product_info_from_url(url)
            if info:
                results.append(info)
                self.append_output_pid(info)

        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        filename = f"user_info_results_{timestamp}.xlsx"

        if self.file_path:
            save_dir = os.path.dirname(self.file_path)
        else:
            save_dir = os.getcwd()

        self.save_path = os.path.join(save_dir, filename)
        df = pd.DataFrame(results)
        df.to_excel(self.save_path, index=False)

        self.set_status(f"✅ 完成！已保存到 {self.save_path}")
        self.open_button.config(state="normal")
        self.enable_buttons()


# ==================== 新功能：视频批量下载 ========================
class TikTokDownloaderApp:
    def __init__(self, parent):
        self.parent = parent
        self.build_ui()

    def build_ui(self):
        label = tk.Label(self.parent, text="请输入多个 TikTok 视频链接（每行一个）:")
        label.pack(pady=5)

        self.text_area = scrolledtext.ScrolledText(self.parent, height=12)
        self.text_area.pack(fill="both", padx=10, pady=5, expand=True)

        # 链接统计
        self.link_stats_label = tk.Label(self.parent, text="共输入：0 条，去重后：0 条", fg="blue")
        self.link_stats_label.pack(pady=2)

        self.count_label = tk.Label(self.parent, text="准备下载视频数: 0 / 已完成: 0", fg="green")
        self.count_label.pack(pady=2)

        self.dir_var = tk.StringVar()
        dir_label = tk.Label(self.parent, text="保存路径：")
        dir_label.pack()

        dir_entry = tk.Entry(self.parent, textvariable=self.dir_var, width=50)
        dir_entry.pack(pady=2)

        button_frame = tk.Frame(self.parent)
        button_frame.pack(pady=5)

        self.browse_button = tk.Button(button_frame, text="选择文件夹", command=self.select_directory)
        self.browse_button.pack(side=tk.LEFT, padx=5)

        self.open_button = tk.Button(button_frame, text="打开文件夹", command=self.open_directory)
        self.open_button.pack(side=tk.LEFT, padx=5)

        self.download_button = tk.Button(self.parent, text="开始下载", command=self.start_download_thread)
        self.download_button.pack(pady=10)

    def select_directory(self):
        folder_selected = filedialog.askdirectory()
        if folder_selected:
            self.dir_var.set(folder_selected)

    def open_directory(self):
        path = self.dir_var.get()
        if path and os.path.isdir(path):
            try:
                if os.name == 'nt':
                    os.startfile(path)
                elif sys.platform == 'darwin':
                    subprocess.Popen(['open', path])
                else:
                    subprocess.Popen(['xdg-open', path])
            except Exception as e:
                messagebox.showerror("错误", f"无法打开文件夹：{e}")
        else:
            messagebox.showwarning("提示", "请先选择一个有效的文件夹。")

    # 封装两个按钮都禁用的操作
    def disable_buttons(self):
        self.download_button.config(state="disabled")
        self.browse_button.config(state="disabled")

    # 封装两个按钮都启用的操作
    def enable_buttons(self):
        self.download_button.config(state="normal")
        self.browse_button.config(state="normal")

    def start_download_thread(self):
        self.disable_buttons()
        thread = threading.Thread(target=self.download_videos)
        thread.start()

    def update_count_label(self):
        self.count_label.config(text=f"准备下载视频数: {self.total_links} / 已完成: {self.completed_count}")

    def sanitize_filename(self, name):
        return re.sub(r'[\\/:*?"<>|]', "_", name).strip()

    def resolve_short_link(self, url):
        try:
            response = requests.head(url, allow_redirects=True, timeout=10, proxies=proxies)
            final_url = response.url
            if 'tiktok.com/@' in final_url and '/video/' in final_url:
                return final_url
            else:
                return url
        except Exception as e:
            print(f"短链解析失败：{url} 错误：{e}")
            return url

    def resolve_short_links_async(self, links):
        resolved_links = []
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(self.resolve_short_link, link): link for link in links}
            for future in as_completed(futures):
                try:
                    resolved_links.append(future.result())
                except Exception as e:
                    link = futures[future]
                    print(f"短链解析失败：{link} 错误：{e}")
                    resolved_links.append(link)
        return resolved_links

    def generate_unique_filename(self, save_path):
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        base = f"tiktok_{timestamp}"
        filename = base
        counter = 1
        while os.path.exists(os.path.join(save_path, filename + ".mp4")):
            filename = f"{base}_{counter}"
            counter += 1
        return filename + ".%(ext)s"

    def download_single_video(self, url, save_path):
        filename = self.generate_unique_filename(save_path)
        ydl_opts = {
            'outtmpl': os.path.join(save_path, filename),
            'quiet': True,
            'noplaylist': True,
            'format': 'mp4',
            'retries': 3,
            'concurrent_fragment_downloads': 3,
            'fragment_retries': 3,
            'nocheckcertificate': True,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

    def download_videos(self):
        raw_links = self.text_area.get("1.0", "end").strip().splitlines()
        links = [link.strip() for link in raw_links if link.strip()]

        # TikTok 短链：tiktok.com/t/xxxx
        short_links = [l for l in links if "tiktok.com/t/" in l]

        # 普通视频链接：只要是 tiktok.com 域名且包含 /video/
        normal_links = [
            l for l in links
            if ("tiktok.com" in l.lower()) and ("/video/" in l.lower())
               and "tiktok.com/t/" not in l.lower()  # 排除短链
        ]

        print("正在解析短链，请稍候...")
        resolved_short_links = self.resolve_short_links_async(short_links)
        all_links = normal_links + resolved_short_links

        seen = set()
        unique_links = []
        for link in all_links:
            if link not in seen:
                seen.add(link)
                unique_links.append(link)

        self.link_stats_label.config(text=f"共输入：{len(links)} 条，去重后：{len(unique_links)} 条")

        save_path = self.dir_var.get()
        if not unique_links or not save_path:
            messagebox.showwarning("提示", "请填写链接并选择保存路径")
            self.enable_buttons()
            return

        self.total_links = len(unique_links)
        self.completed_count = 0
        self.update_count_label()

        for i, url in enumerate(unique_links, 1):
            try:
                self.download_single_video(url, save_path)
                self.completed_count += 1
                self.update_count_label()
                print(f"[{i}] 下载完成：{url}")
            except Exception as e:
                print(f"[{i}] 下载失败：{url} 错误：{e}")

        messagebox.showinfo("完成", f"共处理 {self.total_links} 个链接，成功下载 {self.completed_count} 个视频。")
        self.enable_buttons()


class TikTokScraper1688App:
    def __init__(self, parent):
        self.parent = parent

        font_style = tkfont.Font(family="微软雅黑", size=14, weight="bold")
        tk.Label(parent, text="\n\n读取1688 SKU信息功能\n************************************************************************", font=font_style).pack()
        tk.Label(parent, text="请 输入1688产品页面链接（每行一个）:").pack()
        self.text_urls = scrolledtext.ScrolledText(parent, height=10)
        self.text_urls.pack(fill="x", padx=10, pady=5)

        self.file_path = None

        button_frame = tk.Frame(parent)
        button_frame.pack(pady=10)

        self.btn_account = tk.Button(button_frame, text="开始采集1688 SKU信息", command=self.start_scraping)
        self.btn_account.pack(side="left", padx=5)

        self.open_button = tk.Button(parent, text="打开结果文件", command=self.open_result_file, state="disabled")
        self.open_button.pack(pady=5)

        self.status_text = tk.StringVar()
        self.status_text.set("空闲中")
        tk.Label(parent, textvariable=self.status_text).pack(pady=5)

        tk.Label(parent, text="采集结果:").pack()
        self.output_box = scrolledtext.ScrolledText(parent, height=10, state="normal")
        self.output_box.pack(fill="both", expand=True, padx=10, pady=5)

        self.save_path = ""

    def disable_buttons(self):
        self.btn_account.config(state="disabled")

    def enable_buttons(self):
        self.btn_account.config(state="normal")

    def start_scraping(self):
        self.disable_buttons()
        thread = threading.Thread(target=self.scrape)
        thread.start()

    def scrape(self):
        self.set_status("正在采集...")
        self.output_box.config(state="normal")
        self.output_box.delete("1.0", tk.END)
        self.output_box.config(state="disabled")

        self.append_output({
            "链接": "链接",
            "SKU": "SKU",
            "图片URL": "图片URL",
        })

        urls = []

        manual_input = self.text_urls.get("1.0", tk.END).strip().splitlines()
        urls.extend([u.strip() for u in manual_input if u.strip()])

        if self.file_path:
            with open(self.file_path, 'r', encoding='utf-8') as f:
                file_urls = [line.strip() for line in f if line.strip()]
                urls.extend(file_urls)

        results = []

        for idx, url in enumerate(urls, 1):
            self.set_status(f"正在处理 {idx}/{len(urls)}")
            info_list = self.extract_user_info_from_url(url)
            if info_list:
                for item in info_list:
                    record = {
                        "链接": url,
                        "SKU": f"{item.get('属性名', '')}-{item.get('选项名', '')}",
                        "图片URL": item.get("图片", "")
                    }
                    results.append(record)
                    self.append_output(record)

        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        filename = f"user_info_results_{timestamp}.xlsx"

        if self.file_path:
            save_dir = os.path.dirname(self.file_path)
        else:
            save_dir = os.getcwd()

        self.save_path = os.path.join(save_dir, filename)
        df = pd.DataFrame(results)
        df.to_excel(self.save_path, index=False)

        self.set_status(f"✅ 完成！已保存到 {self.save_path}")
        self.open_button.config(state="normal")
        self.enable_buttons()

    def extract_user_info_from_url(self, url):
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            resp = requests.get(url, headers=headers)
            soup = BeautifulSoup(resp.text, "html.parser")

            for script in soup.find_all("script"):
                if script.string and "window.__INIT_DATA" in script.string:
                    match = re.search(r"window\.__INIT_DATA\s*=\s*(\{.*?\})\s*;", script.string, re.DOTALL)
                    if match:
                        json_str = match.group(1)
                        try:
                            data = json.loads(json_str)
                            sku_props = data.get("globalData", {}).get("skuModel", {}).get("skuProps", [])
                            result = []
                            for prop in sku_props:
                                prop_name = prop.get("prop")
                                for item in prop.get("value", []):
                                    result.append({
                                        "属性名": prop_name,
                                        "选项名": item.get("name"),
                                        "图片": item.get("imageUrl")
                                    })
                            return result
                        except Exception as e:
                            raise ValueError(f"JSON 解析失败: {e}")

            raise ValueError("找不到 window.__INIT_DATA 或格式不正确")
        except Exception as e:
            messagebox.showerror("采集出错", f"采集 URL 时出错：\n{url}\n\n错误详情：\n{str(e)}")
            return None

    def set_status(self, message):
        self.status_text.set(message)

    def append_output(self, info):
        try:
            self.output_box.config(state="normal")
            line = (
                f"{info['链接']}\t"
                f"{info['SKU']}\t"
                f"{info['图片URL']}\n"
            )
            self.output_box.insert(tk.END, line)
            self.output_box.see(tk.END)
            self.output_box.config(state="disabled")
        except Exception as e:
            messagebox.showerror("追加结果出错", f"追加结果时出错：\n{info.get('SKU','')}\n\n错误详情：\n{str(e)}")

    def open_result_file(self):
        if self.save_path and os.path.exists(self.save_path):
            try:
                os.startfile(self.save_path)
            except Exception as e:
                messagebox.showerror("错误", f"无法打开文件: {e}")


# ======================== 主程序窗口 =============================
class MainApp:
    def __init__(self, root):
        self.root = root
        self.root.title("多功能 TikTok 工具")
        self.root.geometry("1000x700")

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True)

        self.init_tabs()

    def init_tabs(self):
        tab_scraper = ttk.Frame(self.notebook)
        self.notebook.add(tab_scraper, text="数据采集")
        TikTokScraperApp(tab_scraper)

        tab_downloader = ttk.Frame(self.notebook)
        self.notebook.add(tab_downloader, text="视频下载")
        TikTokDownloaderApp(tab_downloader)

        tab_scraper1688 = ttk.Frame(self.notebook)
        self.notebook.add(tab_scraper1688, text="采集1688SKU信息")
        TikTokScraper1688App(tab_scraper1688)


if __name__ == "__main__":
    root = tk.Tk()
    app = MainApp(root)
    root.mainloop()
