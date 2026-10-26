"""D365 服务订单（实体 mcs_serviceorder）查询与字段映射。

字段来自 sany.crm4.dynamics.com 的实体元数据：
  mcs_ordernumber 订单号 · mcs_referencenumber 外部系统工单号（Udesk 的 SVC-xxxxx）
  mcs_ordersource 订单来源（60 = Udesk）· mcs_orderstatus 订单状态 · _mcs_engineer_value 工程师
"""
from __future__ import annotations

import json
from datetime import datetime
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
