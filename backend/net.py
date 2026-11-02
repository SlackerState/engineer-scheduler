"""对外的 HTTP 请求（SIS、iWMS、Udesk 中间件、GitHub 检查更新）：共用 httpx 客户端。

同一个网站的连接会复用（不用每次重新握手，连续查库存、图片更快），连接没建立起来时自动重试；
请求已经发出去的不重试，不会重复提交。代理和证书照旧跟系统走（同以前的 urllib）：
代理读系统设置 / 环境变量，证书用系统默认的证书库（公司网络装了自己的根证书也能用）。
"""
from __future__ import annotations

import contextlib
import ssl
import threading
import urllib.parse
import urllib.request
from typing import Any, Iterator

import httpx

RETRIES = 2
TIMEOUT = 30
USER_AGENT = "EngineerScheduler"

_lock = threading.Lock()
_clients: dict[str, httpx.Client] = {}  # 代理地址（直连为 ""）→ 客户端


def _proxy_for(url: str) -> str:
    """这个地址要走的代理，每次都读系统设置，改了代理不用重启软件。"""
    u = urllib.parse.urlsplit(url)
    p = urllib.request.getproxies().get(u.scheme) or ""
    if not p.startswith(("http://", "https://")) or urllib.request.proxy_bypass(u.hostname or ""):
        return ""
    return p


def _client(url: str) -> httpx.Client:
    proxy = _proxy_for(url)
    with _lock:
        if proxy not in _clients:
            _clients[proxy] = httpx.Client(
                transport=httpx.HTTPTransport(verify=ssl.create_default_context(), proxy=proxy or None,
                                              retries=RETRIES, trust_env=False),
                timeout=TIMEOUT, follow_redirects=True, trust_env=False, headers={"User-Agent": USER_AGENT})
        return _clients[proxy]


@contextlib.contextmanager
def _errors(system: str) -> Iterator[None]:
    """连不上、超时转成给用户看的中文提示。"""
    try:
        yield
    except httpx.TimeoutException as e:
        raise RuntimeError(f"{system} 响应超时，请稍后再试") from e
    except httpx.TransportError as e:
        raise RuntimeError(f"连不上 {system}（{e}），请检查网络") from e


def request(system: str, method: str, url: str, **kw) -> httpx.Response:
    """发请求，不检查状态码（调用方先处理 401 等，再用 check）。system 是出错时提示里的系统名。"""
    with _errors(system):
        return _client(url).request(method, url, **kw)


@contextlib.contextmanager
def stream(system: str, url: str, **kw) -> Iterator[httpx.Response]:
    """边下边写的 GET（下载安装包用），状态码不是 2xx 时报错。"""
    with _errors(system), _client(url).stream("GET", url, **kw) as r:
        yield check(system, r)


def check(system: str, r: httpx.Response) -> httpx.Response:
    """状态码不是 2xx 时报错，带上接口返回的错误说明。"""
    if r.is_success:
        return r
    r.read()
    try:
        j = r.json()
        msg = (j.get("message") or j.get("requestMessage") or "") if isinstance(j, dict) else ""
    except ValueError:
        msg = ""
    raise RuntimeError(f"{system} 返回错误 {r.status_code}：{msg or r.reason_phrase}")


def json_of(r: httpx.Response) -> Any:
    return r.json() if r.content else {}
