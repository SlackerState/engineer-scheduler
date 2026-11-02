"""iWMS（三一物流平台 oswms-deu.sanygroup.com，HZERO 平台）：查配件在英国各仓的库存和库位。

接口同 iWMS「库存管理 → 库存查询」：
  POST {后台}/inv-console/v1/7/wms-inventorys/queryByAll?page=0&size=200
       {"itemCode": 物料号, "warehouseIdStr": "19,1"}   （7 = 租户 三一物流平台）
       返回每个库位、每个批次一行：warehouseName、locationCode 库位、locationType（ST 存储 / RV 收货 / SP 发货）、
       qtyBaseQty 数量、qtyPackageUnit 单位、lockStatus 等
  GET  {后台}/basedata/v1/lovs/sql/data?lovCode=MD.WAREHOSUE   仓库列表（英国英格兰配件库 = 2O01_8005）
  请求头 Authorization: bearer <网页 localStorage 里的 access_token>（约 24 小时有效），失效时返回 401
库存查询要用「海外iWMS查询」角色（hwpjk_query），当前角色记在 token 上：
  GET {后台}/iam/hzero/v1/users/self 看 currentRoleCode；GET /iam/hzero/v1/member-roles/self-roles 列角色；
  PUT {后台}/iam/v1/users/roles?roleId= 切换（同网页右上角切换角色）
登录走三一统一登录，由用户在软件的浏览器窗口里自己登录。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .browser import NeedLogin

WEB_URL = "https://oswms-deu.sanygroup.com"
API_URL = "https://oswms-deu-backstage.sanygroup.com"
TENANT = 7
QUERY_ROLE = "hwpjk_query"  # 海外iWMS查询
MAIN_WAREHOUSE = "2O01_8005"  # 英国英格兰配件库
LOCATION_TYPES = {"ST": "存储", "RV": "收货（待上架）", "SP": "发货（已拣货）"}


class Iwms:
    def __init__(self, browser, get_settings, set_setting):
        self.browser = browser
        self.get_settings = get_settings
        self.set_setting = set_setting
        self._warehouses: list[dict] | None = None

    @property
    def web(self) -> str:
        return (self.get_settings().get("iwms_url") or WEB_URL).rstrip("/")

    @property
    def api(self) -> str:
        return (self.get_settings().get("iwms_api_url") or API_URL).rstrip("/")

    def _call(self, method: str, path: str, body: dict | None = None, token: str | None = None):
        tok = token or self.get_settings().get("iwms_token") or ""
        if not tok:
            raise NeedLogin("iWMS 未登录")
        req = urllib.request.Request(
            self.api + path, method=method, data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"bearer {tok}", "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise NeedLogin("iWMS 登录已过期") from e
            try:
                msg = json.loads(e.read() or b"{}")
                msg = msg.get("message") or msg.get("requestMessage") or ""
            except ValueError:
                msg = ""
            raise RuntimeError(f"iWMS 返回错误 {e.code}{'：' + msg if msg else ''}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"连不上 iWMS（{e.reason}），请检查网络") from e
        data = json.loads(raw or b"{}")
        if isinstance(data, dict) and data.get("failed"):
            raise RuntimeError(data.get("message") or "iWMS 处理失败")
        return data

    # ---------- 登录 / 角色 ----------
    def _ensure_role(self, token: str | None = None):
        """库存查询要在「海外iWMS查询」角色下做；当前不是就切过去（同网页右上角切换角色）。"""
        me = self._call("GET", "/iam/hzero/v1/users/self", token=token)
        if me.get("currentRoleCode") == QUERY_ROLE:
            return
        roles = self._call("GET", "/iam/hzero/v1/member-roles/self-roles", token=token) or []
        role = next((r for r in roles if r.get("code") == QUERY_ROLE), None)
        if not role:
            raise RuntimeError("你的 iWMS 账号没有「海外iWMS查询」角色，查不了库存，请找 iWMS 管理员开通")
        self._call("PUT", f"/iam/v1/users/roles?roleId={role['id']}", token=token)

    def status(self) -> dict:
        try:
            self._call("GET", "/iam/hzero/v1/users/self")
            return {"logged_in": True}
        except NeedLogin:
            return {"logged_in": False}

    def login(self) -> dict:
        """弹出浏览器窗口打开 iWMS，用户登录后从网页取 access_token，校验有效后保存。"""
        host = urllib.parse.urlparse(self.web).hostname
        found = {}

        def done(ctx):
            for page in ctx.pages:
                if urllib.parse.urlparse(page.url).hostname != host:
                    continue
                try:
                    tok = page.evaluate("() => localStorage.getItem('access_token')")
                except Exception:
                    continue  # 页面还在跳转
                if tok and tok != found.get("bad"):
                    try:
                        self._call("GET", "/iam/hzero/v1/users/self", token=tok)
                        found["token"] = tok
                        return True
                    except NeedLogin:
                        found["bad"] = tok  # 上次留下的过期 token，等用户重新登录
            return False

        self.browser.login_window(self.web + "/", done)
        self.set_setting("iwms_token", found["token"])
        self._ensure_role()
        return {"logged_in": True}

    # ---------- 查询 ----------
    def warehouses(self) -> list[dict]:
        if self._warehouses is None:
            r = self._call("GET", "/basedata/v1/lovs/sql/data?lovCode=MD.WAREHOSUE&page=0&size=100")
            self._warehouses = [{"id": w["id"], "code": w.get("code") or "", "name": w.get("name") or ""}
                                for w in r.get("content") or []]
        return self._warehouses

    def _query(self, part_no: str, wh_ids: str) -> list[dict]:
        path = f"/inv-console/v1/{TENANT}/wms-inventorys/queryByAll?page=0&size=500"
        return self._call("POST", path, {"itemCode": part_no, "warehouseIdStr": wh_ids}).get("content") or []

    def stock(self, part_no: str) -> dict:
        """这个物料在各仓的库存，按 仓库 → 库位 合并（同一库位不同批次相加）。英格兰配件库排第一。"""
        no = (part_no or "").strip()
        if not no:
            return {"warehouses": []}
        main = self.get_settings().get("iwms_main_warehouse") or MAIN_WAREHOUSE
        whs = self.warehouses()
        ids = ",".join(str(w["id"]) for w in whs)
        try:
            rows = self._query(no, ids)
        except RuntimeError as e:
            if "403" not in str(e) and "权限" not in str(e):
                raise
            self._ensure_role()  # 角色被切走了（如在网页上切了别的角色）
            rows = self._query(no, ids)
        out: dict[int, dict] = {w["id"]: {**w, "main": w["code"] == main, "total": 0.0, "available": 0.0,
                                          "unit": "", "locations": {}} for w in whs}
        for r in rows:
            if (r.get("skuItemCode") or "").upper() != no.upper():
                continue  # 只要这个物料号本身
            w = out.setdefault(r.get("warehouseId"), {"id": r.get("warehouseId"), "code": "", "main": False,
                                                       "name": r.get("warehouseName") or "", "total": 0.0,
                                                       "available": 0.0, "unit": "", "locations": {}})
            q = float(r.get("qtyBaseQty") or 0)
            locked = "Y" in (r.get("lockStatus"), r.get("inOutLock"), r.get("countLock"))
            typ = r.get("locationType") or ""
            key = (r.get("locationCode") or "", typ, locked)
            loc = w["locations"].setdefault(key, {"location": key[0], "type": LOCATION_TYPES.get(typ, typ),
                                                  "store": typ == "ST", "locked": locked, "qty": 0.0,
                                                  "received": ""})
            loc["qty"] += q
            loc["received"] = max(loc["received"], (r.get("lotInfoReceivedDate") or "")[:10])
            w["total"] += q
            if typ == "ST" and not locked:
                w["available"] += q
            w["unit"] = w["unit"] or r.get("qtyPackageUnit") or ""
        res = []
        for w in out.values():
            w["locations"] = sorted(w["locations"].values(), key=lambda x: (not x["store"], x["location"]))
            res.append(w)
        res.sort(key=lambda w: (not w["main"], -w["total"], w["name"]))
        return {"part_no": no, "warehouses": res, "checked": time.strftime("%H:%M")}
