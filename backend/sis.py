"""SIS（三一服务信息系统 sis-global.sanygroup.com）：按物料号查配件图片。

接口同 SIS「配件查询 → 详情」里的图片：
  POST /api-gateway/bom-server/materialLink/queryGmpsPhotoByMateriaCode  {"materiaCode": 物料号}
  请求头 Authorization = 登录 token（网页登录后存在 Cookie「Admin-Token」里，JWT，12 小时有效），
        Authorization-userId = token 里的 userId；不需要 Cookie
  返回 data: [{filePath, fileThumbnailPath, ...}]，图片在 https://gmps-cos.sany.com.cn/<路径>，不用登录就能打开
  fileThumbnailPath 名为缩略图，实际和原图一样是 6016×4016（约 2 MB）。图片服务器是腾讯云 COS，
  地址后加 ?imageMogr2/thumbnail/<宽>x 就返回按宽度缩小的图（200 宽约 20 KB），列表和预览都用缩小的
token 缺失或过期时返回 code 10001。SIS 登录有验证码，所以由用户在软件的浏览器窗口里自己登录。

替代件同 SIS「配件查询」里的「替代件」弹窗：
  POST /api-gateway/customer/equipmentInfo/listReplaceByMaterialCode，表单（multipart）materialCode、pageNum（从 0 开始）、
  pageSize、langType；除了上面两个请求头，还要带 SIS 网站的 Cookie「JSESSIONID」，没有时网关返回 500
  「authHystrixInterface failed」。返回 data: {data: [...], totalElements}，每行是一个适用的主机或产品组：
  newPartChildrenLst（替代后的物料号 / 名称 / 数量）、relationType（1对1替换…）、replaceDirection（单向 / 双向）、
  isForce（是否强制）、replaceType（基于主机 / 基于产品组）、groupMachineName。名称只有中文（langType 不影响）
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
SESSION_COOKIE = "JSESSIONID"
REPLACE_PAGE, REPLACE_MAX_PAGES = 100, 5


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
        self._repl_cache: dict[str, list[dict]] = {}

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

    def _form(self, path: str, fields: dict):
        """表单方式调用（替代件接口只认 multipart 表单，还要带 SIS 网站的会话 Cookie）。"""
        tok, c = self._token()
        sess = self.browser.cookie(self.base, SESSION_COOKIE)
        if not sess:
            raise NeedLogin("SIS 未登录或登录已过期")
        r = net.request("SIS", "POST", f"{self.base}/api-gateway{path}",
                        files={k: (None, str(v)) for k, v in fields.items()},
                        headers={"Accept": "application/json", "Authorization": tok, "Authorization-userId": str(c["userId"]),
                                 "Cookie": f"{SESSION_COOKIE}={sess}"})  # httpx 不建议按请求传 cookies=，直接放请求头
        if r.status_code == 500 and "authHystrix" in r.text:
            raise NeedLogin("SIS 登录已过期")
        j = net.json_of(net.check("SIS", r))
        code = str(j.get("code", "0"))
        if code == "10001":
            raise NeedLogin("SIS 登录已过期")
        if code != "0":
            raise RuntimeError(j.get("message") or "SIS 查询失败")
        return j.get("data") or {}

    def replacements(self, part_no: str) -> list[dict]:
        """配件的替代件。SIS 按适用的主机 / 产品组一行行列出，同一组替代件合并成一条：
        [{parts: [{no, name, qty}], relation, direction, forced, scope, machines: [...], reasons}]，按适用范围多的排前面"""
        no = (part_no or "").strip()
        if not no:
            return []
        if no in self._repl_cache:
            return self._repl_cache[no]
        rows: list[dict] = []
        for page in range(REPLACE_MAX_PAGES):
            d = self._form("/customer/equipmentInfo/listReplaceByMaterialCode",
                           {"materialCode": no, "pageNum": page, "pageSize": REPLACE_PAGE, "langType": "zh_CN"})
            got = d.get("data") or []
            rows += got
            if len(got) < REPLACE_PAGE or len(rows) >= (d.get("totalElements") or 0):
                break
        groups: dict[tuple, dict] = {}
        for r in rows:
            parts = [{"no": (c.get("newChildrenPart") or "").strip(), "name": c.get("newChildrenName") or "",
                      "qty": c.get("newChildrenQty") or ""} for c in (r.get("newPartChildrenLst") or [])]
            parts = [x for x in parts if x["no"]]
            if not parts:
                continue
            key = (tuple((x["no"], x["qty"]) for x in parts), r.get("relationType") or "", r.get("replaceDirection") or "",
                   r.get("isForce") or "")
            g = groups.setdefault(key, {"parts": parts, "relation": key[1], "direction": key[2], "forced": key[3] == "是",
                                        "scope": [], "machines": [], "reasons": []})
            for k, v in (("scope", r.get("replaceType")), ("machines", r.get("groupMachineName")), ("reasons", r.get("replaceReason"))):
                if v and v not in g[k]:
                    g[k].append(v)
        out = sorted(groups.values(), key=lambda g: -len(g["machines"]))
        self._repl_cache[no] = out
        return out
