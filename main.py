"""工程师排班管理 —— 入口。

  python main.py            打开桌面窗口（Windows 用 Edge 应用窗口，Mac 用 WKWebView）
  python main.py --web      网页模式，浏览器访问 http://127.0.0.1:8765
  python main.py --selftest 结果.json   打包自检（CI 用）
  工程师排班管理.exe --apply-update <旧进程号> <安装目录>   Windows 在线更新时由新版本自动执行
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import webbrowser
from pathlib import Path


def resource_dir() -> Path:
    # PyInstaller 打包后资源解压到 sys._MEIPASS
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_edge() -> str | None:
    """Windows 10/11 自带 Edge。"""
    if sys.platform != "win32":
        return None
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            p = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if p.exists():
                return str(p)
    return shutil.which("msedge")


def ui_log(msg: str):
    """Windows 窗口模式的启动/退出记录（数据目录下 ui.log，只留最近 300 行），排查打不开时看。"""
    try:
        from backend.db import app_dir
        f = app_dir() / "ui.log"
        lines = f.read_text(encoding="utf-8").splitlines()[-299:] if f.exists() else []
        lines.append(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{os.getpid()}] {msg}")
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


# 页面每 5 秒发一次心跳；Edge 进程退出后，超过这么久没心跳才算窗口真的关了
UI_IDLE_EXIT = 20


def wait_ui_closed(proc, httpd, api):
    """等 Edge 窗口关闭。Edge 有时会把窗口交给另一个进程后自己先退出（例如后台有 Edge 更新待安装），
    窗口其实还开着；这时如果直接关掉本地服务，页面就变成「127.0.0.1 无法访问」。所以进程退出后
    再看页面心跳：还有心跳就继续服务，直到页面关闭（收到 /__bye 且之后没有新请求）或长时间没心跳。"""
    code = proc.wait()
    ui_log(f"Edge 进程已退出（退出码 {code}，距上次页面请求 {time.time() - httpd.last_seen:.0f} 秒）")
    while not getattr(api, "_quitting", False):
        now = time.time()
        if httpd.bye_at and httpd.last_seen < httpd.bye_at and now - httpd.bye_at > 3:
            ui_log("页面已关闭，退出")
            return
        if now - httpd.last_seen > UI_IDLE_EXIT:
            ui_log(f"{UI_IDLE_EXIT} 秒没有页面心跳，退出")
            return
        time.sleep(1)


def run_edge_window(api, ui: Path) -> bool:
    """Windows：本地服务 + Edge 应用模式窗口（无地址栏），不依赖 .NET / pythonnet。窗口关掉即退出。"""
    edge = os.environ.get("ES_EDGE") or find_edge()  # ES_EDGE：CI 用来模拟 Edge 交接进程
    if not edge:
        return False
    from backend.server import serve
    port = int(os.environ.get("ES_PORT") or 0) or free_port()
    httpd = serve(api, ui, "127.0.0.1", port, background=True)
    # 每次用独立的临时配置目录，保证 Edge 窗口是我们自己的进程，关闭时能感知到
    profile = tempfile.mkdtemp(prefix="es-ui-")
    proc = subprocess.Popen([
        edge, f"--app=http://127.0.0.1:{port}/", f"--user-data-dir={profile}", "--window-size=1400,880",
        "--no-first-run", "--no-default-browser-check", "--disable-sync", "--disable-extensions",
    ])
    ui_log(f"启动 v{_version()}：端口 {port}，Edge {edge}（进程 {proc.pid}）")
    api._ui_proc = proc
    try:
        wait_ui_closed(proc, httpd, api)
    finally:
        httpd.shutdown()
        api.shutdown()
        shutil.rmtree(profile, ignore_errors=True)
    return True


def _version() -> str:
    try:
        from backend.version import __version__
        return __version__
    except Exception:
        return "?"


def run_webview(api, ui: Path):
    import webview
    window = webview.create_window("工程师排班管理", str(ui / "index.html"), js_api=api,
                                   width=1400, height=880, min_size=(1000, 640))
    api._window = window
    try:
        webview.start()
    finally:
        api.shutdown()


def selftest(out: str):
    """检查打包后的程序各部分能否加载，结果写到 JSON 文件（Windows 无控制台）。"""
    res = {"python": sys.version, "frozen": bool(getattr(sys, "frozen", False)), "exe": sys.executable}
    try:
        from backend.version import __version__
        res["version"] = __version__
        res["ui"] = (resource_dir() / "ui" / "index.html").exists()
        res["edge"] = find_edge()
        import playwright  # noqa: F401
        res["playwright"] = "ok"
    except Exception:
        res["error"] = traceback.format_exc()
    if sys.platform == "win32":
        try:
            import clr  # noqa: F401  pywebview 在 Windows 上依赖它（现在仅作后备）
            res["pythonnet"] = "ok"
        except Exception:
            res["pythonnet"] = traceback.format_exc()[-800:]
    Path(out).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--web", action="store_true", help="以网页模式运行")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--selftest", metavar="OUT_JSON")
    ap.add_argument("--apply-update", nargs=2, metavar=("OLD_PID", "TARGET_DIR"),
                    help="Windows 在线更新：由新版本执行，替换安装目录后重新打开")
    args = ap.parse_args()

    if args.selftest:
        selftest(args.selftest)
        return
    if args.apply_update:
        from backend.updater import finish_windows_update
        finish_windows_update(int(args.apply_update[0]), args.apply_update[1])
        return

    from backend.api import Api
    api = Api()
    ui = resource_dir() / "ui"

    if args.web:
        from backend.server import serve
        serve(api, ui, args.host, args.port)
        return

    if sys.platform == "win32" and run_edge_window(api, ui):
        return
    try:
        run_webview(api, ui)
    except Exception:
        # 最后的后备：用默认浏览器打开网页模式
        from backend.server import serve
        port = free_port()
        webbrowser.open(f"http://127.0.0.1:{port}/")
        serve(api, ui, "127.0.0.1", port)


if __name__ == "__main__":
    main()
