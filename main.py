"""工程师排班管理 —— 入口。

  python main.py            打开桌面窗口（Windows 用 WebView2，Mac 用 WKWebView）
  python main.py --web      网页模式，浏览器访问 http://127.0.0.1:8765
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def resource_dir() -> Path:
    # PyInstaller 打包后资源解压到 sys._MEIPASS
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--web", action="store_true", help="以网页模式运行")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    from backend.api import Api
    api = Api()
    ui = resource_dir() / "ui"

    if args.web:
        from backend.server import serve
        serve(api, ui, args.host, args.port)
        return

    import webview
    window = webview.create_window("工程师排班管理", str(ui / "index.html"), js_api=api,
                                   width=1400, height=880, min_size=(1000, 640))
    api._window = window
    try:
        webview.start()
    finally:
        api.shutdown()


if __name__ == "__main__":
    main()
