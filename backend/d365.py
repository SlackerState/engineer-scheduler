"""D365 服务订单（实体 mcs_serviceorder）查询与字段映射。

字段来自 sany.crm4.dynamics.com 的实体元数据：
  mcs_ordernumber 订单号 · mcs_referencenumber 外部系统工单号（Udesk 的 SVC-xxxxx）
  mcs_ordersource 订单来源（60 = Udesk）· mcs_orderstatus 订单状态 · _mcs_engineer_value 工程师
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import quote

from .browser import Browser

FV = "@OData.Community.Display.V1.FormattedValue"
ENTITY_SET = "mcs_serviceorders"

SELECT = [
    "mcs_serviceorderid", "mcs_ordernumber", "mcs_referencenumber", "mcs_ordersource",
    "mcs_orderstatus", "mcs_ordertype", "createdon", "modifiedon", "mcs_customerordertime",
    "_mcs_customer_value", "mcs_equipmentno", "mcs_equipmentmodel", "mcs_orderdescription",
    "_mcs_engineer_value", "mcs_appointmenttime", "mcs_dispatchdate", "mcs_setofftime",
    "mcs_actualarrivaldate", "mcs_onsitecompletiondate", "mcs_estimatedarrivaldate",
    "mcs_equipmentlocation", "mcs_equipmentcontact1", "mcs_equipmentcontacttel",
    "_mcs_networkid_value", "mcs_warrantystatus", "mcs_faultlevel",
]

# mcs_orderstatus 选项值 → 本地「订单清单」状态
STATUS_LOCAL = {10: "待处理", 15: "已取消"}
STATUS_LOCAL.update({v: "已完成" for v in (100, 105, 110, 130, 140)})
FINAL_CODES = {15, 100, 105, 110, 130, 140}


def local_status(code) -> str:
    if code is None:
        return ""
    return STATUS_LOCAL.get(int(code), "处理中")


def to_local(ts: str | None) -> str:
    """D365 返回 UTC 时间（...Z），转成本机时区 'YYYY-MM-DD HH:MM'。"""
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts


def _q(v: str) -> str:
    return v.replace("'", "''")


class D365:
    def __init__(self, browser: Browser, get_settings):
        self.browser = browser
        self.get_settings = get_settings

    @property
    def base(self) -> str:
        url = (self.get_settings().get("d365_url") or "").strip().rstrip("/")
        if not url:
            raise RuntimeError("请先在「设置」里填写 D365 网址")
        return url

    def _api(self, path: str) -> str:
        return f"{self.base}/api/data/{self.get_settings().get('d365_api_version', 'v9.2')}/{path}"

    def get_all(self, path: str, limit: int = 5000) -> list[dict]:
        url, rows = self._api(path), []
        while url and len(rows) < limit:
            data = self.browser.fetch_json(self.base, url)
            rows.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
        return rows

    def whoami(self) -> dict:
        return self.browser.fetch_json(self.base, self._api("WhoAmI"))

    def login(self):
        appid = self.get_settings().get("d365_appid") or ""
        self.browser.interactive_login(self.base, f"{self.base}/main.aspx" + (f"?appid={appid}" if appid else ""))

    def record_url(self, rid: str) -> str:
        appid = self.get_settings().get("d365_appid") or ""
        q = f"appid={appid}&" if appid else ""
        return f"{self.base}/main.aspx?{q}pagetype=entityrecord&etn=mcs_serviceorder&id={rid}"

    def _query(self, flt: str, order: str = "createdon desc") -> list[dict]:
        sel = ",".join(SELECT)
        return self.get_all(f"{ENTITY_SET}?$select={sel}&$filter={quote(flt)}&$orderby={quote(order)}")

    def list_orders(self, since: datetime, sources: list[int]) -> list[dict]:
        flt = f"createdon ge {since.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        if sources:
            flt += " and (" + " or ".join(f"mcs_ordersource eq {int(s)}" for s in sources) + ")"
        return self._query(flt)

    def by_ids(self, ids: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(ids), 15):
            chunk = ids[i:i + 15]
            out += self._query(" or ".join(f"mcs_serviceorderid eq {x}" for x in chunk))
        return out

    def find(self, number: str) -> list[dict]:
        n = _q(number.strip())
        return self._query(f"mcs_ordernumber eq '{n}' or mcs_referencenumber eq '{n}'")

    # ---------- 写入（规则与 D365 表单 mcs_serviceorder.js / dispatchuser.html 一致） ----------
    def _live(self, rid: str) -> dict:
        """写入前重新读一次订单的实时状态，避免用过期数据做判断。"""
        sel = "mcs_ordernumber,mcs_orderstatus,mcs_approvalstatus,_mcs_engineer_value,mcs_appointmenttime," \
              "mcs_appointmenttimechangenumber"
        return self.browser.fetch_json(self.base, self._api(f"{ENTITY_SET}({rid})?$select={sel}"))

    def _action(self, name: str, params: dict) -> dict:
        data = self.browser.fetch_json(self.base, self._api(name), method="POST", body=params)
        res = data.get("Result")
        if isinstance(res, str) and res.strip().startswith("{"):
            res = json.loads(res)
            if res.get("IsSuccess") is False:
                raise RuntimeError(res.get("Message") or res.get("ErrorMessage") or f"{name} 执行失败")
            return res
        return data

    def engineer_list(self, rid: str, keyword: str = "") -> list[dict]:
        """D365 派工对话框用的工程师列表（Service_GetEngineerListApi，只读）。"""
        res = self._action("Service_GetEngineerListApi", {"OrderId": rid, "FilterName": keyword, "AllowSelf": True})
        return [{"id": e.get("id"), "name": " ".join(str(e.get("name") or "").replace("#", " ").split()),
                 "jobcard": e.get("jobcard") or "", "network": e.get("Network") or "",
                 "skill": e.get("personnelskill") or ""} for e in res.get("EngineerData") or []]

    def assign_engineer(self, rid: str, person_id: str) -> str:
        """已创建 → 派工（Service_DispatchEngineerApi）；已派工/已接收 → 转派（Service_TransferEngineerApi）。"""
        cur = self._live(rid)
        code, no = cur.get("mcs_orderstatus"), cur.get("mcs_ordernumber")
        if cur.get("mcs_approvalstatus") in (20, 81, 90, 110, 130):
            raise RuntimeError(f"{no} 正在审批中，D365 不允许转派")
        if code == 10:
            action, verb = "Service_DispatchEngineerApi", "派工"
        elif code in (20, 70):
            if (cur.get("_mcs_engineer_value") or "").lower() == person_id.lower():
                raise RuntimeError(f"{no} 已经派给这位工程师了")
            action, verb = "Service_TransferEngineerApi", "转派"
        else:
            raise RuntimeError(f"{no} 当前状态为「{cur.get('mcs_orderstatus' + FV)}」，D365 只允许在已创建/已派工/已接收时派工或转派")
        self._action(action, {"DispatchParame": json.dumps({"personid": person_id, "orderid": rid})})
        return verb

    def set_appointment(self, rid: str, local_dt: datetime) -> None:
        """改预约上门时间：D365 表单只允许「已创建」状态修改（CC 角色除外），并累加修改次数。"""
        cur = self._live(rid)
        if cur.get("mcs_orderstatus") != 10:
            raise RuntimeError(f"{cur.get('mcs_ordernumber')} 当前状态为「{cur.get('mcs_orderstatus' + FV)}」，"
                               "D365 只允许在「已创建」时修改预约上门时间，请在 D365 里由 CC 修改")
        body = {"mcs_appointmenttime": local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        if cur.get("mcs_appointmenttime"):
            body["mcs_appointmenttimechangenumber"] = int(cur.get("mcs_appointmenttimechangenumber") or 0) + 1
        self.browser.fetch_json(self.base, self._api(f"{ENTITY_SET}({rid})"), method="PATCH", body=body,
                                headers={"If-Match": "*"})


def normalize(rec: dict) -> dict:
    """D365 记录 → 本地 orders 表字段。"""
    f = lambda k: rec.get(k + FV) or (rec.get(k) if rec.get(k) is not None else "")  # noqa: E731
    contact = " ".join(x for x in (rec.get("mcs_equipmentcontact1"), rec.get("mcs_equipmentcontacttel")) if x)
    created = to_local(rec.get("mcs_customerordertime") or rec.get("createdon"))
    code = rec.get("mcs_orderstatus")
    return {
        "d365_id": rec["mcs_serviceorderid"],
        "service_order_no": rec.get("mcs_ordernumber") or "",
        "udesk_no": rec.get("mcs_referencenumber") or "",
        "order_type": f("mcs_ordertype"),
        "day": created[:10],
        "customer": f("_mcs_customer_value"),
        "serial": rec.get("mcs_equipmentno") or "",
        "model": rec.get("mcs_equipmentmodel") or "",
        "problem": rec.get("mcs_orderdescription") or "",
        "engineer": " ".join(str(f("_mcs_engineer_value")).replace("#", " ").split()),
        "d365_status": f("mcs_orderstatus"),
        "d365_status_code": code,
        "status": local_status(code),
        "appointment": to_local(rec.get("mcs_appointmenttime")),
        "dispatch_time": to_local(rec.get("mcs_dispatchdate")),
        "setoff_time": to_local(rec.get("mcs_setofftime")),
        "arrival_time": to_local(rec.get("mcs_actualarrivaldate")),
        "complete_time": to_local(rec.get("mcs_onsitecompletiondate")),
        "address": rec.get("mcs_equipmentlocation") or "",
        "contact": contact,
        "network": f("_mcs_networkid_value"),
        "warranty": f("mcs_warrantystatus"),
        "source": f("mcs_ordersource"),
        "d365_json": json.dumps(rec, ensure_ascii=False),
    }
