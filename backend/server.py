"""网页模式：用标准库起一个本地 HTTP 服务，界面通过 POST /api/<方法名> 调用 Api。"""
from __future__ import annotations

import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .api import Api, call

BLOCKED = {"shutdown"}


def serve(api: Api, ui_dir: Path, host: str = "127.0.0.1", port: int = 8765, background: bool = False):
    """background=True 时在后台线程运行并返回 server 对象（Edge 应用窗口模式用）。"""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            rel = self.path.split("?")[0].lstrip("/") or "index.html"
            f = (ui_dir / rel).resolve()
            if ui_dir.resolve() not in f.parents or not f.is_file():
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            body = f.read_bytes()
            if f.name == "index.html":
                body = body.replace(b"<head>", b'<head>\n<meta name="es-mode" content="web">', 1)
            self._send(200, body, ctype)

        def do_POST(self):
            if not self.path.startswith("/api/"):
                return self._send(404, b"not found", "text/plain")
            method = self.path[5:]
            try:
                if method in BLOCKED:
                    raise AttributeError(method)
                n = int(self.headers.get("Content-Length") or 0)
                args = json.loads(self.rfile.read(n) or b"[]")
                body = {"ok": True, "result": call(api, method, args)}
            except Exception as e:  # noqa: BLE001 - 错误交给界面显示
                body = {"ok": False, "error": str(e)}
            self._send(200, json.dumps(body, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    httpd = ThreadingHTTPServer((host, port), Handler)
    if background:
        threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
        return httpd
    try:  # 打包后的 Windows 程序没有控制台，或控制台编码不支持中文
        print(f"网页模式已启动：http://{host}:{port}", flush=True)
    except Exception:
        pass
    try:
        httpd.serve_forever()
    finally:
        api.shutdown()
