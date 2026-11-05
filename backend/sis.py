"""SIS（三一服务信息系统 sis-global.sanygroup.com）：按物料号查配件图片。

接口同 SIS「配件查询 → 详情」里的图片：
  POST /api-gateway/bom-server/materialLink/queryGmpsPhotoByMateriaCode  {"materiaCode": 物料号}
  请求头 Authorization = 登录 token（网页登录后存在 Cookie「Admin-Token」里，JWT，12 小时有效），
        Authorization-userId = token 里的 userId；不需要 Cookie
  返回 data: [{filePath, fileThumbnailPath, ...}]，图片在 https://gmps-cos.sany.com.cn/<路径>，不用登录就能打开
  fileThumbnailPath 名为缩略图，实际和原图一样是 6016×4016（约 2 MB）。图片服务器是腾讯云 COS，
  地址后加 ?imageMogr2/thumbnail/<宽>x 就返回按宽度缩小的图（200 宽约 20 KB），列表和预览都用缩小的
token 缺失或过期时返回 code 10001。SIS 登录有验证码，所以由用户在软件的浏览器窗口里自己登录。
"""
from __future__ import annotations

import base64
import json
import time

from . import net
from .browser import NeedLogin

SIS_URL = "https://sis-global.sanygroup.com"
IMG_BASE = "https://gmps-cos.sany.com.cn/"
THUMB_WIDTH, PREVIEW_WIDTH = 200, 1200  # 列表、小图用 200 宽；弹窗里的大图用 1200 宽；全屏看原图
TOKEN_COOKIE = "Admin-Token"


def _claims(token: str) -> dict:
    try:
        part = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError):
        return {}


class Sis:
    def __init__(self, browser, get_settings):
        self.browser = browser
        self.get_settings = get_settings
        self._cache: dict[str, list[dict]] = {}

    @property
    def base(self) -> str:
        return (self.get_settings().get("sis_url") or SIS_URL).rstrip("/")

    @property
    def img_base(self) -> str:
        return (self.get_settings().get("sis_img_url") or IMG_BASE).rstrip("/") + "/"

    def _token(self) -> tuple[str, dict]:
        """返回还有效的 token 和它的内容（userId、exp），没有或快过期了就要重新登录。"""
        tok = self.browser.cookie(self.base, TOKEN_COOKIE)
        c = _claims(tok) if tok else {}
        if not c.get("userId") or c.get("exp", 0) < time.time() + 60:
            raise NeedLogin("SIS 未登录或登录已过期")
        return tok, c

    def status(self) -> dict:
        """实际请求一次 SIS（查一个不存在的物料号），token 时间上没过期但 SIS 已经不认时也能发现。"""
        try:
            _, c = self._token()
            self._call("/bom-server/materialLink/queryGmpsPhotoByMateriaCode", {"materiaCode": "-"})
            return {"logged_in": True, "expires": time.strftime("%Y-%m-%d %H:%M", time.localtime(c["exp"]))}
        except NeedLogin:
            return {"logged_in": False}

    def login(self) -> dict:
        # 旧 token 可能时间上没过期、但 SIS 已经不认了（如在别处重新登录），所以要等到出现新的 token
        old = self.browser.cookie(self.base, TOKEN_COOKIE)

        def done(ctx):
            tok = next((x["value"] for x in ctx.cookies(self.base) if x["name"] == TOKEN_COOKIE), "")
            c = _claims(tok) if tok and tok != old else {}
            return bool(c.get("userId")) and c.get("exp", 0) > time.time() + 60
        self.browser.login_window(self.base + "/login", done)
        return self.status()

    def _call(self, path: str, body: dict):
        tok, c = self._token()
        r = net.request("SIS", "POST", f"{self.base}/api-gateway{path}", json=body,
                        headers={"Content-Type": "application/json;charset=UTF-8", "Accept": "application/json",
                                 "Authorization": tok, "Authorization-userId": str(c["userId"])})
        j = net.json_of(net.check("SIS", r))
        code = str(j.get("code"))
        if code == "10001":
            raise NeedLogin("SIS 登录已过期")
        if code != "0":
            raise RuntimeError(j.get("message") or "SIS 查询失败")
        return j.get("data")

    def photos(self, part_no: str) -> list[dict]:
        """配件图片：[{thumb, preview, full, name}]，没有图片返回空列表。同一物料号只查一次。
        原图有 2400 万像素，几十张一起显示会让页面卡住（Mac 版窗口按原尺寸解码），所以 thumb / preview 是缩小的"""
        no = (part_no or "").strip()
        if not no:
            return []
        if no not in self._cache:
            rows = self._call("/bom-server/materialLink/queryGmpsPhotoByMateriaCode", {"materiaCode": no}) or []
            rows.sort(key=lambda r: r.get("filePath") or "")
            out = []
            for r in rows:
                full = self.img_base + (r.get("filePath") or r.get("fileThumbnailPath") or "")
                out.append({"thumb": f"{full}?imageMogr2/thumbnail/{THUMB_WIDTH}x",
                            "preview": f"{full}?imageMogr2/thumbnail/{PREVIEW_WIDTH}x",
                            "full": full, "name": (r.get("filePath") or "").rsplit("/", 1)[-1]})
            self._cache[no] = out
        return self._cache[no]
