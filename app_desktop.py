"""
==============================================================================
PolyArbitrage Pro - 原生独立客户端启动器 (Microsoft Edge PWA Standalone Mode)
==============================================================================
特点:
1. 采用 Windows 官方自带的原生 Chromium 引擎独立运行
2. 彻底移除地址栏、书签栏、菜单栏与多标签页，呈现纯净专业的彭博终端独立窗口
3. 拥有独立的硬件加速进程与 GPU 独立渲染管道，零浏览器沙盒掉帧
4. 自动守护检测并拉起本地量化引擎
==============================================================================
"""

import os
import sys
import time
import subprocess
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)

SERVER_URL = "http://127.0.0.1:8888/?v=3.0"
PROFILE_DIR = os.path.join(BASE_DIR, "data", "app_profile")
os.makedirs(PROFILE_DIR, exist_ok=True)

EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe"),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

def find_browser():
    for p in EDGE_PATHS:
        if os.path.exists(p):
            return p
    return None

def ensure_server():
    try:
        urllib.request.urlopen(f"{SERVER_URL}/api/status", timeout=1.5)
        return
    except Exception:
        pass

    import threading
    def start_backend():
        try:
            import uvicorn
            from server import app
            uvicorn.run(app, host="0.0.0.0", port=8888, log_level="warning")
        except Exception:
            pass

    t = threading.Thread(target=start_backend, daemon=True)
    t.start()

    for _ in range(30):
        time.sleep(0.3)
        try:
            urllib.request.urlopen(f"{SERVER_URL}/api/status", timeout=1.0)
            break
        except Exception:
            pass

def kill_old_instances():
    """彻底终止残留的同 profile 浏览器后台进程，防止复用旧缓存窗口"""
    try:
        cmd = [
            "powershell", "-NoProfile", "-Command",
            "Get-CimInstance Win32_Process -Filter \"name = 'msedge.exe'\" | "
            "Where-Object { $_.CommandLine -match 'app_profile' } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    except Exception:
        pass

def clear_cache():
    """清理本地 Profile 缓存目录"""
    for sub in ["Cache", "Code Cache", "Service Worker"]:
        p = os.path.join(PROFILE_DIR, "Default", sub)
        if os.path.exists(p):
            try:
                import shutil
                shutil.rmtree(p, ignore_errors=True)
            except Exception:
                pass

def main():
    kill_old_instances()
    clear_cache()
    ensure_server()

    browser = find_browser()
    if not browser:
        # 回退默认打开
        import webbrowser
        webbrowser.open(SERVER_URL)
        return

    cmd = [
        browser,
        f"--user-data-dir={PROFILE_DIR}",
        f"--app={SERVER_URL}",
        "--window-size=1440,920",
        "--window-position=80,40",
        "--enable-gpu-rasterization",
        "--enable-zero-copy",
        "--disk-cache-size=1",
        "--media-cache-size=1",
        "--disable-application-cache",
        "--disable-features=Translate",
        "--no-first-run",
        "--no-default-browser-check"
    ]

    subprocess.Popen(cmd)

if __name__ == "__main__":
    main()
