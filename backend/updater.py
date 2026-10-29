"""在线更新：从 GitHub Releases 检查新版本、下载、替换程序并重启。

Windows：程序是一个文件夹（工程师排班管理\\工程师排班管理.exe）。新版本下载到本机应用数据目录后，
直接运行新版本的 exe（--apply-update 模式）等本程序退出、把文件复制到安装目录并重新打开，不依赖 PowerShell。
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

from .db import app_dir
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
            if sys.platform == "win32":
                work = stage_dir()
                shutil.rmtree(work, ignore_errors=True)
                work.mkdir(parents=True, exist_ok=True)
            else:
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
                self._new_path = _find_program(out, ".app")
            else:
                with zipfile.ZipFile(zpath) as z:
                    z.extractall(out)
                self._new_path = _find_program(out, "")
            if not self._new_path or not self._new_path.exists():
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
            # 不用 PowerShell 脚本（公司电脑常被组策略禁止运行脚本）：
            # 直接启动新版本自己的 exe，进入 --apply-update 模式，由它等本程序退出后把自己复制过去
            new_exe = _exe_in(self._new_path)
            _log(f"启动新版本完成替换：{new_exe} -> {dst}（旧进程 {pid}）")
            flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            subprocess.Popen([str(new_exe), "--apply-update", str(pid), str(dst)], cwd=str(self._new_path),
                             creationflags=flags, close_fds=True)
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
        """清理上次更新留下的东西：Mac 的 .old 备份，Windows 的下载暂存目录。"""
        dst = install_path()
        if dst and sys.platform == "darwin":
            old = Path(str(dst) + ".old")
            if old.exists():
                shutil.rmtree(old, ignore_errors=True)
        if dst and sys.platform == "win32" and stage_dir() not in dst.parents:
            shutil.rmtree(stage_dir(), ignore_errors=True)

    @staticmethod
    def pop_result() -> dict | None:
        """上一次在线更新的结果（启动后提示一次）。"""
        f = app_dir() / "update-result.json"
        if not f.exists():
            return None
        try:
            return json.loads(f.read_text("utf-8"))
        except Exception:
            return None
        finally:
            f.unlink(missing_ok=True)


# ---------- Windows：新版本 exe 以 --apply-update 模式运行，完成替换 ----------
def _exe_in(folder: Path) -> Path:
    """程序文件夹里的主程序：优先 APP_NAME.exe，找不到就用文件夹里唯一的 exe（以后改了程序名也能更新）。"""
    exe = folder / f"{APP_NAME}.exe"
    if exe.exists():
        return exe
    exes = sorted(folder.glob("*.exe"))
    return exes[0] if exes else exe


def _find_program(out: Path, suffix: str) -> Path | None:
    """解压出来的新版本：Mac 是 *.app，Windows 是含 exe 的文件夹；优先叫 APP_NAME 的。"""
    first = out / (APP_NAME + suffix)
    if first.exists():
        return first
    for p in sorted(out.iterdir()) if out.exists() else []:
        if p.is_dir() and (p.name.endswith(".app") if suffix else any(p.glob("*.exe"))):
            return p
    return None


def stage_dir() -> Path:
    """Windows 下载解压新版本的位置（本机目录，不放漫游配置里）。"""
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(base) / "EngineerScheduler" / "update"


def _log(msg: str):
    try:
        from datetime import datetime
        with open(app_dir() / "update.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except Exception:
        pass


def _wait_pid(pid: int, timeout_ms: int = 120000):
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if h:
        k32.WaitForSingleObject(h, timeout_ms)
        k32.CloseHandle(h)


def _copy_with_retry(src: Path, dst: Path, seconds: float = 30) -> None:
    import time
    deadline = time.time() + seconds
    while True:
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            return
        except PermissionError:
            if time.time() > deadline:
                raise
            time.sleep(0.5)


def finish_windows_update(pid: int, target: str) -> None:
    """由新版本 exe 执行：等旧程序退出 → 把自己所在的程序文件夹复制到安装位置 → 打开新版本。"""
    src = Path(sys.executable).resolve().parent
    dst = Path(target)
    ok, msg = False, ""
    try:
        _log(f"开始替换：{src} -> {dst}，等待旧进程 {pid} 退出")
        _wait_pid(pid)
        import time
        time.sleep(1)
        files = [p for p in src.rglob("*") if p.is_file()]
        for p in files:
            _copy_with_retry(p, dst / p.relative_to(src))
        # _internal 里旧版本多出来的文件删掉，避免新旧混用
        keep = {p.relative_to(src) for p in files}
        for p in (dst / "_internal").rglob("*"):
            if p.is_file() and p.relative_to(dst) not in keep:
                p.unlink(missing_ok=True)
        ok, msg = True, f"已更新到 {__version__}"
        _log(f"替换完成，共 {len(files)} 个文件")
    except Exception as e:  # noqa: BLE001
        msg = f"在线更新没有完成：{e}。请关闭软件后，手动下载新版本覆盖安装。"
        _log(f"替换失败：{e!r}")
    try:
        (app_dir() / "update-result.json").write_text(
            json.dumps({"ok": ok, "message": msg, "version": __version__}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    exe = _exe_in(dst)
    try:
        subprocess.Popen([str(exe)], cwd=str(dst), creationflags=0x00000008 | 0x00000200, close_fds=True)
        _log(f"已重新打开：{exe}")
    except Exception as e:  # noqa: BLE001
        _log(f"重新打开失败：{e!r}")
