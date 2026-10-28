"""在 Udesk 建服务订单：调用 Udesk 里「建单」插件背后的三一中间件（sanyi-mid）接口。

插件页面 sanyi-mid.s5.udesk.cn/sanyi/web 的接口（不需要登录，提交时带坐席 ID 标明是谁建的）：
  GET  /customer?equipmentNo=      按设备编号查客户（客户名、联系人、电话、地址、国家）
  GET  /equipment?equipmentNo=&countryCode=   设备信息（设备所在国家）
  GET  /dicts/code?code=process_type          订单类型：YV06 召请 · YV02 保养 · YV04 开机
  GET  /engineer/search?keyword=&countryCode= 服务工程师
  POST /serviceOrder                          建服务订单（生成 SVC-xxxxx，中间件再推送到 D365）
  GET  /myServiceOrderRecord/search?agentId=  我创建的服务订单（SVC 号 ↔ D365 订单号）
表单规则照插件（ServeOrder 组件）：必填 设备、订单类型、联系人、联系电话（≤20）、服务目的地（≤50）、来源渠道；
召请必须填订单描述（≤500），开机必须选服务工程师；预约时间可不填，填了要在现在到 30 天之内。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

# 来源渠道（插件里的「来源渠道」，字段 isInitByEng）
SOURCE_CHANNELS = [{"value": 0, "label": "本地热线"}, {"value": 2, "label": "即时消息"}, {"value": 1, "label": "工程师转接"}]
PROCESS_DESC_REQUIRED = "YV06"   # 召请
PROCESS_ENGINEER_REQUIRED = "YV04"  # 开机
LIMITS = {"zzPerTel": 20, "zzeqpLocation": 50, "textZ013": 500}


class Udesk:
    def __init__(self, get_settings):
        self.get_settings = get_settings

    # ---------- HTTP ----------
    @property
    def base(self) -> str:
        return (self.get_settings().get("udesk_mid_url") or "https://sanyi-mid.s5.udesk.cn/sanyi").rstrip("/")

    def _q(self, params: dict | None = None) -> str:
        s = self.get_settings()
        p = dict(params or {}, language="zh-cn", timeZone=s.get("udesk_timezone") or "Europe/London")
        return urllib.parse.urlencode({k: ("" if v is None else v) for k, v in p.items()})

    def _call(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        url = f"{self.base}{path}?{self._q(params)}"
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json;charset=UTF-8", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                j = json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read() or b"{}").get("message")
            except ValueError:
                msg = ""
            raise RuntimeError(f"Udesk 中间件返回错误 {e.code}：{msg or e.reason}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"连不上 Udesk 中间件（{e.reason}），请检查网络") from e
        if isinstance(j, dict) and j.get("succeed") is False:
            raise RuntimeError(j.get("message") or "Udesk 中间件处理失败")
        return j.get("data") if isinstance(j, dict) else j

    # ---------- 查询 ----------
    def process_types(self) -> list[dict]:
        return [{"value": x.get("staticKey"), "label": x.get("staticValue")}
                for x in self._call("GET", "/dicts/code", {"code": "process_type"}) or []]

    def lookup(self, equipment_no: str) -> dict:
        """按设备编号查客户和设备，用来预填联系人、电话、服务目的地和国家。"""
        no = equipment_no.strip()
        if not no:
            raise ValueError("请输入设备编号")
        cust = self._call("GET", "/customer", {"equipmentNo": no}) or {}
        if not cust:
            raise RuntimeError(f"Udesk 里没有找到设备 {no}")
        cc = cust.get("accountCountryCode") or ""
        eq = (self._call("GET", "/equipment", {"equipmentNo": no, "countryCode": cc}) or {}) if cc else {}
        return {
            "equipment_no": no, "country": eq.get("countryCode") or cc,
            "customer": cust.get("accountName") or cust.get("englishName") or "",
            "account_number": cust.get("accountNumber") or "",
            "contact": cust.get("contactName") or eq.get("equipmentContact") or "",
            "phone": cust.get("mobilePhone") or eq.get("equipmentContactTel") or "",
            "address": cust.get("address") or "",
            "model": eq.get("modelMachineType") or eq.get("name") or "",
            "warranty": eq.get("warrantyStatus") or "", "network": eq.get("serviceNetwork") or "",
            "dealer": eq.get("serviceDealer") or eq.get("dealer") or "",
        }

    def engineers(self, keyword: str, country: str) -> list[dict]:
        if not keyword.strip():
            return []
        rows = self._call("GET", "/engineer/search", {"keyword": keyword.strip(), "countryCode": country}) or []
        return [{"id": r.get("engineerId"), "name": r.get("engineerName")} for r in rows]

    def my_records(self, agent_id: str, size: int = 10) -> list[dict]:
        rows = self._call("GET", "/myServiceOrderRecord/search",
                          {"agentId": agent_id, "telephoneNo": "", "pageNum": 1, "pageSize": size}) or []
        return [{"id": r.get("id"), "svc": r.get("preOrderNumber") or "", "crm": r.get("crmOrderNumber") or "",
                 "process_type": r.get("processType") or "", "status": r.get("status")} for r in rows]

    # ---------- 建单 ----------
    def check(self, f: dict) -> list[str]:
        """同插件表单校验，返回问题列表（空 = 可以提交）。"""
        errs = []
        for key, label in (("equipmentId", "设备编号"), ("processType", "订单类型"), ("zzSrvPer", "联系人"),
                           ("zzPerTel", "联系电话"), ("zzeqpLocation", "服务目的地")):
            if not str(f.get(key) or "").strip():
                errs.append(f"请填写{label}")
        if str(f.get("isInitByEng", "")).strip() == "":
            errs.append("请选择来源渠道")
        for key, label in (("zzPerTel", "联系电话"), ("zzeqpLocation", "服务目的地"), ("textZ013", "订单描述")):
            if len(str(f.get(key) or "")) > LIMITS[key]:
                errs.append(f"{label}最多 {LIMITS[key]} 个字符")
        if f.get("processType") == PROCESS_DESC_REQUIRED and not str(f.get("textZ013") or "").strip():
            errs.append("召请单必须填写订单描述")
        if f.get("processType") == PROCESS_ENGINEER_REQUIRED and not f.get("engineerId"):
            errs.append("开机单必须选择服务工程师")
        if f.get("appointmentTime"):
            try:
                t = datetime.strptime(f["appointmentTime"], "%Y-%m-%d %H:%M")
                if t < datetime.now() or t > datetime.now() + timedelta(days=30):
                    errs.append("预约时间只能在现在到 30 天之内")
            except ValueError:
                errs.append("预约时间格式不对")
        return errs

    def create(self, f: dict) -> dict:
        """提交服务订单，返回新单的 SVC 号和 D365 订单号（中间件处理完才有，可能为空）。"""
        agent = str(self.get_settings().get("udesk_agent_id") or "").strip()
        if not agent:
            raise RuntimeError("请先在「设置」里填写你的 Udesk 坐席 ID（建单时用来标明是谁建的）")
        errs = self.check(f)
        if errs:
            raise ValueError("；".join(errs))
        before = {r["id"] for r in self.my_records(agent, 5)}
        body = {
            "processType": f["processType"], "equipmentId": f["equipmentId"].strip(),
            "zzSrvPer": f["zzSrvPer"].strip(), "zzPerTel": f["zzPerTel"].strip(),
            "zzeqpLocation": f["zzeqpLocation"].strip(), "textZ013": (f.get("textZ013") or "").strip(),
            "appointmentTime": f.get("appointmentTime") or "", "country": f.get("country") or "",
            "engineerId": f.get("engineerId") or "", "engineerName": f.get("engineerName") or "",
            "isInitByEng": int(f["isInitByEng"]),
            # 插件在来电时带来电号码和通话 ID；软件里建单没有通话，来电号码用联系电话
            "mobile": f["zzPerTel"].strip(), "agentId": agent, "countryCode": f.get("country") or "", "callId": "",
        }
        self._call("POST", "/serviceOrder", body=body)
        # 找出刚建的那条（我创建的列表里新出现的），D365 订单号可能要等中间件推送后才有
        for _ in range(6):
            new = [r for r in self.my_records(agent, 5) if r["id"] not in before]
            if new and new[0]["crm"]:
                return new[0]
            time.sleep(2)
        return new[0] if new else {"svc": "", "crm": ""}
