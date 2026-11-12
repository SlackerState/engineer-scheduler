"""GSC 指挥中心（数链全球 bop-gsc.sanyglobal.com，HZERO 平台）：维修三快（接单、到达、完工及时）。

数据同 GSC「调度中心 → 维修调度」页，只取直销和合资公司的单（订单模式 10 = 直销、30 = 合资公司；20 是服务经销商，不要）：
  POST {后台}/gsc-service-cmdcenter/v1/2/schedul-general-view/order-status-stat     四个节点各有几单
       {"owningBusinessUnit": "R06", "region": "N029", "country": "", "serviceOrderMode": "10,30",
        "regionIdList": [], "agentCodeList": []}        R06 = 欧洲大区事业部，N029 = 英国国区
       → {"totalCount", "acceptance" 待接收, "departure" 待出发, "arriving" 待到达, "completion" 待完工}
  POST {后台}/gsc-service-cmdcenter/v1/2/schedul-general-view/alarm-overtime-stat   各节点触发预警 / 已超时几单（同上请求体）
       → {"acceptanceMap": {"alarm", "overTime"}, "departureMap", "arrivingMap", "completionMap": {..., "pauseRepair"}}
  GET  {后台}/gsc-service-cmdcenter/v1/2/schedul-general-view/page-warn?owningBusinessUnit=&region=&serviceOrderMode=&page=&size=
       还在走流程的单，每单一行：currentNodeDesc 当前节点、isWarn / isOvertime、residueOverTimeStr
       「-1,剩余」没预警 /「0,剩余」已预警 /「1,超时」已超时，repairMaterialOrderNos 借料 / 领料单号等
  请求头 Authorization: Bearer <网页 sessionStorage 里的 access_token>，失效时返回 401
登录走三一统一登录，由用户在软件的浏览器窗口里自己登录。
"""
from __future__ import annotations

import time
import urllib.parse

from . import net
from .browser import NeedLogin

WEB_URL = "https://bop-gsc.sanyglobal.com"
API_URL = "https://bop-gsc-api.sanyglobal.com"
BOARD = "/gsc-service-cmdcenter/v1/2/schedul-general-view"
ORDER_MODES = "10,30"  # 直销、合资公司
BUSINESS_UNIT = "R06"  # 欧洲大区事业部
REGION = "N029"  # 英国国区
PAGE = 200
MAX_PAGES = 10
# 节点：统计接口里的键 → 卡片上的名字（顺序即流程顺序）。每单的节点按 currentNode 代码对应（DEPARTURE_NODE → departure），
# 不按名字：卡片叫「待完工」，单子上却是「待现场完工」
NODES = [("acceptance", "待接收"), ("departure", "待出发"), ("arriving", "待到达"), ("completion", "待完工")]


def _node_key(r: dict) -> str:
    """这单在哪个节点卡片下：按 currentNode 代码（DEPARTURE_NODE → departure）；代码对不上时按名字（「待现场完工」含「完工」）。"""
    key = (r.get("currentNode") or "").removesuffix("_NODE").lower()
    if key in dict(NODES):
        return key
    desc = r.get("currentNodeDesc") or ""
    return next((k for k, name in NODES if name[1:] in desc), key)


def _left(s: str) -> str:
    """residueOverTimeStr「-1,22时48分26秒」→「22时48分26秒」。"""
    return (s or "").split(",", 1)[-1] if "," in (s or "") else (s or "")


def _row(r: dict) -> dict:
    over = str(r.get("isOvertime")) == "1"
    secs = r.get("overTime") if over else r.get("residueTime")
    try:
        secs = int(secs)
    except (TypeError, ValueError):
        secs = None
    return {
        "order_no": r.get("orderNumber") or "",
        "d365_id": (r.get("serviceOrderId") or "").lower(),
        "mode": r.get("serviceOrderModeName") or "",
        "summon": r.get("summonTypeName") or "",
        "source": r.get("orderSourceName") or "",
        "channel": r.get("sourceChannelDesc") or "",
        "created": r.get("createDonStr") or "",
        "node": r.get("currentNodeDesc") or "",
        "node_key": _node_key(r),
        "status": r.get("orderStatusDesc") or "",
        "pause": r.get("repairPauseReasonDesc") or "",
        "warn": str(r.get("isWarn")) == "1",
        "over": over,
        "left": _left(r.get("residueOverTimeStr") or "") or (r.get("overTimeStr") if over else r.get("residueTimeStr")) or "",
        "secs": -secs if over and secs is not None else secs,  # 剩余为正、超时为负，排序用
        "engineer": r.get("engineerName") or "",
        "network": r.get("agentName") or "",
        "equipment": r.get("equipmentName") or "",
        "customer": r.get("newCustomerName") or "",
        "fault": r.get("faultPriorityDesc") or "",
        "materials": [x for x in (r.get("repairMaterialOrderNos") or "").split(",") if x],
        "explain": r.get("timeoutExplanation") or "",
    }


class Gsc:
    def __init__(self, browser, get_settings, set_setting):
        self.browser = browser
        self.get_settings = get_settings
        self.set_setting = set_setting

    @property
    def web(self) -> str:
        return (self.get_settings().get("gsc_url") or WEB_URL).rstrip("/")

    @property
    def api(self) -> str:
        return (self.get_settings().get("gsc_api_url") or API_URL).rstrip("/")

    def _call(self, method: str, path: str, body: dict | None = None, token: str | None = None):
        tok = token or self.get_settings().get("gsc_token") or ""
        if not tok:
            raise NeedLogin("GSC 未登录")
        r = net.request("GSC", method, self.api + path, json=body,
                        headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"})
        if r.status_code == 401:
            raise NeedLogin("GSC 登录已过期")
        data = net.json_of(net.check("GSC", r))
        if isinstance(data, dict) and data.get("failed"):
            raise RuntimeError(data.get("message") or "GSC 处理失败")
        return data

    # ---------- 登录 ----------
    def status(self) -> dict:
        try:
            self._call("GET", "/iam/hzero/v1/users/self")
            return {"logged_in": True}
        except NeedLogin:
            return {"logged_in": False}

    def login(self) -> dict:
        """弹出浏览器窗口打开 GSC，用户登录后从网页的 sessionStorage 取 access_token，校验有效后保存。"""
        host = urllib.parse.urlparse(self.web).hostname
        found = {}

        def done(ctx):
            for page in ctx.pages:
                if urllib.parse.urlparse(page.url).hostname != host:
                    continue
                try:
                    tok = page.evaluate("() => sessionStorage.getItem('access_token')")
                except Exception:
                    continue  # 页面还在跳转
                if tok and tok != found.get("bad"):
                    try:
                        self._call("GET", "/iam/hzero/v1/users/self", token=tok)
                        found["token"] = tok
                        return True
                    except NeedLogin:
                        found["bad"] = tok
            return False

        self.browser.login_window(self.web + "/dispath-center/scheduling-overview", done)
        self.set_setting("gsc_token", found["token"])
        return {"logged_in": True}

    # ---------- 维修调度 ----------
    def _scope(self) -> dict:
        s = self.get_settings()
        return {"owningBusinessUnit": s.get("gsc_business_unit") or BUSINESS_UNIT,
                "region": s.get("gsc_region") or REGION, "serviceOrderMode": ORDER_MODES}

    def board(self) -> dict:
        """四个节点的单数、预警数、超时数，和每单的节点 / 预警 / 超时 / 剩余或超时时间。"""
        scope = self._scope()
        body = {**scope, "country": "", "regionIdList": [], "agentCodeList": []}
        counts = self._call("POST", BOARD + "/order-status-stat", body) or {}
        alarms = self._call("POST", BOARD + "/alarm-overtime-stat", body) or {}
        rows: list[dict] = []
        for page in range(MAX_PAGES):
            q = urllib.parse.urlencode({**scope, "page": page, "size": PAGE})
            data = self._call("GET", f"{BOARD}/page-warn?{q}") or {}
            rows += data.get("content") or []
            if len(rows) >= int(data.get("totalElements") or 0) or not data.get("content"):
                break
        nodes = []
        for key, name in NODES:
            a = alarms.get(key + "Map") or {}
            nodes.append({"key": key, "name": name, "count": int(counts.get(key) or 0),
                          "warn": int(a.get("alarm") or 0), "over": int(a.get("overTime") or 0),
                          "pause": int(a.get("pauseRepair") or 0)})
        out = [_row(r) for r in rows]
        out.sort(key=lambda r: (not r["over"], not r["warn"], r["secs"] if r["secs"] is not None else 10**12))
        return {"nodes": nodes, "total": int(counts.get("totalCount") or len(out)), "rows": out,
                "checked": time.strftime("%H:%M")}
