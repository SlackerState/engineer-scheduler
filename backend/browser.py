"""用 Playwright 驱动本机的 Chrome / Edge 保持 D365 登录，并在页面里调用 D365 Web API。

Playwright 的同步 API 只能在创建它的线程里用，所以所有浏览器操作都放进一个专用工作线程。
"""
from __future__ import annotations

import json
import queue
import sys
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from urllib.parse import urlparse


class NeedLogin(Exception):
    pass


ACCEPT_HEADERS = {
    "Accept": "application/json",
    "OData-MaxVersion": "4.0",
    "OData-Version": "4.0",
    "Prefer": 'odata.include-annotations="OData.Community.Display.V1.FormattedValue",odata.maxpagesize=500',
}

FETCH_JS = """
async ([url, headers]) => {
  const r = await fetch(url, {headers, credentials: 'include'});
  return {status: r.status, ct: r.headers.get('content-type') || '', text: await r.text()};
}
"""


def _channels(pref: str) -> list:
    if pref and pref != "auto":
        return [None if pref == "chromium" else pref]
    if sys.platform == "win32":
        return ["msedge", "chrome", None]
    return ["chrome", "msedge", None]


class Browser:
    def __init__(self, data_dir: Path, get_settings):
        self.profile = data_dir / "browser-profile"
        self.state_file = data_dir / "d365-session.json"
        self.get_settings = get_settings
        self._q: queue.Queue = queue.Queue()
        self._pw = None
        self._ctx = None
        self._headed = False
        self._api_page = None
        threading.Thread(target=self._loop, name="browser", daemon=True).start()

    # ---------- 线程调度 ----------
    def _loop(self):
        while True:
            fn, fut = self._q.get()
            if fn is None:
                self._close()
                if self._pw:
                    self._pw.stop()
                fut.set_result(None)
                return
            try:
                fut.set_result(fn())
            except BaseException as e:  # noqa: BLE001 - 原样交回调用方
                fut.set_exception(e)

    def _run(self, fn, timeout: float = 300):
        fut: Future = Future()
        self._q.put((fn, fut))
        return fut.result(timeout)

    def shutdown(self):
        try:
            self._run(None, timeout=15)
        except Exception:
            pass

    # ---------- 浏览器上下文（仅在工作线程调用） ----------
    def _close(self):
        if self._ctx:
            try:
                self._ctx.storage_state(path=str(self.state_file))
            except Exception:
                pass
            try:
                self._ctx.close()
            except Exception:
                pass
        self._ctx = None
        self._api_page = None

    def _ensure(self, headed: bool):
        if self._ctx and (self._headed or not headed):
            return self._ctx
        self._close()
        if not self._pw:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
        s = self.get_settings()
        last = None
        for ch in _channels(s.get("browser_channel", "auto")):
            kw = dict(user_data_dir=str(self.profile), headless=not headed,
                      args=["--disable-blink-features=AutomationControlled"])
            if headed:
                kw["no_viewport"] = True
            if ch:
                kw["channel"] = ch
            try:
                self._ctx = self._pw.chromium.launch_persistent_context(**kw)
                break
            except Exception as e:  # 该浏览器没装，换下一个
                last = e
        if not self._ctx:
            raise RuntimeError(f"没有找到可用的浏览器（Chrome 或 Edge），请先安装其中一个。\n{last}")
        self._headed = headed
        # 持久化目录不保存会话 Cookie，这里把上次保存的补回去
        if self.state_file.exists():
            try:
                cookies = json.loads(self.state_file.read_text("utf-8")).get("cookies", [])
                if cookies:
                    self._ctx.add_cookies(cookies)
            except Exception:
                pass
        return self._ctx

    # ---------- 对外接口（任意线程） ----------
    def interactive_login(self, base: str, url: str, timeout: int = 300):
        """弹出可见浏览器让用户登录；检测到 D365 API 可用后自动保存登录状态并关窗。"""
        def fn():
            self._close()
            ctx = self._ensure(headed=True)
            ver = self.get_settings().get("d365_api_version", "v9.2")
            host = urlparse(base).netloc
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.bring_to_front()
                deadline = time.time() + timeout
                while time.time() < deadline:
                    if urlparse(page.url).netloc == host:
                        try:
                            r = page.evaluate(FETCH_JS, [f"{base}/api/data/{ver}/WhoAmI", ACCEPT_HEADERS])
                            if r["status"] == 200:
                                self._close()  # 保存 Cookie 并关窗，之后在后台运行
                                return True
                        except Exception:
                            pass  # 页面还在跳转
                    page.wait_for_timeout(2000)
            except Exception as e:
                self._close()
                raise NeedLogin(f"登录窗口被关闭或出错（{str(e).splitlines()[0]}）") from e
            self._close()
            raise NeedLogin(f"{timeout // 60} 分钟内没有完成登录")
        return self._run(fn, timeout=timeout + 90)

    def _open_api_page(self, ctx, base: str):
        """先打开 D365 首页：会话过期时微软账号会自动单点登录续期；然后停在同源的 WhoAmI 上调 API。

        直接打开 API 地址在未登录时只会得到 401，不会触发登录跳转。
        """
        host = urlparse(base).netloc
        ver = self.get_settings().get("d365_api_version", "v9.2")
        page = ctx.new_page()
        try:
            page.goto(base + "/", wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass  # 跳转过程中的中断不要紧，下面看最终落在哪
        deadline = time.time() + 25
        while urlparse(page.url).netloc != host and time.time() < deadline:
            # 停在微软登录页、出现了账号/密码输入框 = 无法静默登录，不必再等
            try:
                if page.locator("input[type=email]:visible, input[type=password]:visible").count():
                    break
            except Exception:
                pass
            page.wait_for_timeout(500)
        if urlparse(page.url).netloc != host:
            page.close()
            raise NeedLogin("D365 未登录或登录已过期")
        try:
            page.goto(f"{base}/api/data/{ver}/WhoAmI", wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            page.close()
            raise NeedLogin("D365 未登录或登录已过期") from e
        return page

    def fetch_json(self, base: str, url: str) -> dict:
        """在 D365 页面上下文里 fetch，Cookie 认证与正常打开 D365 完全一样。"""
        def fn():
            s = self.get_settings()
            ctx = self._ensure(headed=not s.get("headless", True))
            page = self._api_page
            if page is None or page.is_closed() or urlparse(page.url).netloc != urlparse(base).netloc:
                page = self._api_page = self._open_api_page(ctx, base)
            r = page.evaluate(FETCH_JS, [url, ACCEPT_HEADERS])
            if r["status"] in (401, 403) or "json" not in r["ct"]:
                self._api_page = None
                raise NeedLogin(f"D365 拒绝访问（HTTP {r['status']}），请重新登录")
            data = json.loads(r["text"] or "{}")
            if r["status"] >= 400:
                msg = (data.get("error") or {}).get("message") or r["text"][:300]
                raise RuntimeError(f"D365 返回错误 {r['status']}：{msg}")
            return data
        return self._run(fn)

    def save_state(self):
        def fn():
            if self._ctx:
                self._ctx.storage_state(path=str(self.state_file))
        try:
            self._run(fn, timeout=30)
        except Exception:
            pass
