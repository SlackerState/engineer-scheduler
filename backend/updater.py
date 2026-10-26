"""在线更新：从 GitHub Releases 检查新版本、下载、替换程序并重启。

Windows：程序是一个文件夹（工程师排班管理\\工程师排班管理.exe），用 PowerShell 脚本等本程序退出后整体替换。
Mac：程序是 工程师排班管理.app，用 shell 脚本等本程序退出后替换整个 .app。
用户数据在系统的应用数据目录里，不受更新影响。
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.request
import zipfile
from pathlib import Path

from .version import __version__

REPO = "SlackerState/engineer-scheduler"
APP_NAME = "工程师排班管理"


def _ver(s: str) -> tuple:
    out = []
    for part in s.strip().lstrip("vV").split("."):
        num = "".join(ch for ch in part if ch.isdigit())
        out.append(int(num or 0))
    return tuple(out + [0] * (3 - len(out)))


def asset_name() -> str | None:
    if sys.platform == "win32":
        return "EngineerScheduler-windows.zip"
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return "EngineerScheduler-macos-arm64.zip"
    return None


def install_path() -> Path | None:
    """打包后的程序所在位置：Windows 是程序文件夹，Mac 是 .app。开发模式返回 None。"""
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable).resolve()
    if sys.platform == "darwin":
        return exe.parents[2]  # xxx.app/Contents/MacOS/xxx
    return exe.parent


class Updater:
    def __init__(self, get_settings):
        self.get_settings = get_settings
        self.state = {"current": __version__, "latest": "", "available": False, "notes": "", "url": "",
                      "size": 0, "phase": "idle", "progress": 0, "message": "", "can_apply": False}
        self._new_path: Path | None = None
        self._lock = threading.Lock()

    def _api(self) -> str:
        return self.get_settings().get("update_api_url") or f"https://api.github.com/repos/{REPO}/releases/latest"

    def check(self) -> dict:
        req = urllib.request.Request(self._api(), headers={"Accept": "application/vnd.github+json",
                                                           "User-Agent": "EngineerScheduler"})
        with urllib.request.urlopen(req, timeout=15) as r:
            rel = json.loads(r.read())
        latest = rel.get("tag_name") or ""
        want = asset_name()
        asset = next((a for a in rel.get("assets", []) if a.get("name") == want), None)
        self.state.update(
            latest=latest.lstrip("vV"), notes=rel.get("body") or "", url=(asset or {}).get("browser_download_url", ""),
            size=(asset or {}).get("size", 0), available=bool(asset) and _ver(latest) > _ver(__version__),
            can_apply=install_path() is not None,
        )
        if not want:
            self.state["message"] = "这台电脑的系统暂不支持在线更新（Mac 只支持 Apple 芯片）"
        elif not self.state["available"]:
            self.state["message"] = f"已是最新版本（{__version__}）"
        else:
            self.state["message"] = f"发现新版本 {self.state['latest']}"
        return dict(self.state)

    # ---------- 下载 ----------
    def start_download(self) -> bool:
        if not self.state["available"] or not self._lock.acquire(blocking=False):
            return False
        self.state.update(phase="downloading", progress=0, message="正在下载新版本…")
        threading.Thread(target=self._download, daemon=True).start()
        return True

    def _download(self):
        try:
            work = Path(tempfile.mkdtemp(prefix="es-update-"))
            zpath = work / "update.zip"
            req = urllib.request.Request(self.state["url"], headers={"User-Agent": "EngineerScheduler"})
            with urllib.request.urlopen(req, timeout=60) as r, open(zpath, "wb") as f:
                total = int(r.headers.get("Content-Length") or self.state["size"] or 0)
                done = 0
                while chunk := r.read(256 * 1024):
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        self.state["progress"] = int(done * 100 / total)
            self.state.update(phase="extracting", message="正在解压…")
            out = work / "new"
            if sys.platform == "darwin":
                # ditto 能保留 .app 里的符号链接和可执行权限
                subprocess.run(["ditto", "-x", "-k", str(zpath), str(out)], check=True)
                self._new_path = out / f"{APP_NAME}.app"
            else:
                with zipfile.ZipFile(zpath) as z:
                    z.extractall(out)
                self._new_path = out / APP_NAME
            if not self._new_path.exists():
                raise RuntimeError("安装包内容不对，没有找到程序")
            self.state.update(phase="ready", progress=100,
                              message="新版本已下载，点「立即重启更新」完成更新")
        except Exception as e:  # noqa: BLE001 - 显示给用户
            self.state.update(phase="error", message=f"下载失败：{e}")
        finally:
            self._lock.release()

    # ---------- 替换并重启 ----------
    def apply(self) -> None:
        """启动替换脚本；调用方随后必须退出本程序，脚本会等本程序退出后替换并重新打开。"""
        dst = install_path()
        if dst is None:
            raise RuntimeError("开发模式下不能在线更新，请用 git pull 更新代码")
        if self.state["phase"] != "ready" or not self._new_path:
            raise RuntimeError("新版本还没有下载完成")
        pid = os.getpid()
        work = self._new_path.parent.parent
        if sys.platform == "win32":
            exe = dst / f"{APP_NAME}.exe"
            script = work / "update.ps1"
            script.write_text(
                "$ErrorActionPreference = 'SilentlyContinue'\n"
                f"Wait-Process -Id {pid} -Timeout 120\n"
                "Start-Sleep -Seconds 1\n"
                f"robocopy \"{self._new_path}\" \"{dst}\" /MIR /R:10 /W:1 | Out-Null\n"
                f"Start-Process -FilePath \"{exe}\"\n"
                f"Remove-Item -Recurse -Force \"{work}\"\n",
                encoding="utf-8-sig")  # 带 BOM，Windows PowerShell 才能正确读中文路径
            flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
            subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                              "-File", str(script)], creationflags=flags, close_fds=True)
        else:
            script = work / "update.sh"
            q = lambda p: "'" + str(p).replace("'", "'\\''") + "'"  # noqa: E731
            script.write_text(
                "#!/bin/sh\n"
                f"while kill -0 {pid} 2>/dev/null; do sleep 0.5; done\n"
                f"rm -rf {q(str(dst) + '.old')}\n"
                f"mv {q(dst)} {q(str(dst) + '.old')} && mv {q(self._new_path)} {q(dst)} "
                f"&& rm -rf {q(str(dst) + '.old')} || mv {q(str(dst) + '.old')} {q(dst)}\n"
                f"xattr -dr com.apple.quarantine {q(dst)} 2>/dev/null\n"
                f"open {q(dst)}\n"
                f"rm -rf {q(work)}\n", encoding="utf-8")
            subprocess.Popen(["/bin/sh", str(script)], start_new_session=True, close_fds=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.state.update(phase="restarting", message="正在重启以完成更新…")

    def cleanup_old(self):
        """上次更新若异常中断留下的 .old 备份，启动时顺手清掉。"""
        dst = install_path()
        if dst and sys.platform == "darwin":
            old = Path(str(dst) + ".old")
            if old.exists():
                shutil.rmtree(old, ignore_errors=True)
