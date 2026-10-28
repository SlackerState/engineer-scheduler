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

    def record_url(self, rid: str, entity: str = "mcs_serviceorder") -> str:
        appid = self.get_settings().get("d365_appid") or ""
        q = f"appid={appid}&" if appid else ""
        return f"{self.base}/main.aspx?{q}pagetype=entityrecord&etn={entity}&id={rid}"

    # ---------- 配件（只读）：服务配件 + 领料/借料/归还单 ----------
    def _by_orders(self, entity_set: str, select: list[str], lookup: str, order_ids: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(order_ids), 15):
            flt = " or ".join(f"{lookup} eq {x}" for x in order_ids[i:i + 15])
            out += self.get_all(f"{entity_set}?$select={','.join(select)}&$filter={quote(flt)}")
        return out

    def parts_for(self, order_ids: list[str]) -> list[dict]:
        return [normalize_part(r) for r in self._by_orders("mcs_serviceparts", PART_SELECT, "_mcs_serviceorder_value", order_ids)]

    def set_consumption(self, part_id: str, qty: float) -> dict:
        """改服务配件的消耗数量。D365 的插件会据此重算剩余数量和金额（剩余 = 确认数量 − 消耗数量）。
        规则同 D365 表单：0 ≤ 消耗 ≤ 确认数量；另外已核销 / 已在归还单上 / 工单已取消或已完工审批的不能改。"""
        sel = ("mcs_partfullname,mcs_confirmquantity,mcs_consumptionquantity,mcs_remainingquantity,"
               "_mcs_partswriteoffdocid_value,_mcs_retunorderid_value,_mcs_serviceorder_value")
        p = self.browser.fetch_json(self.base, self._api(f"mcs_serviceparts({part_id})?$select={sel}"))
        name = p.get("mcs_partfullname") or "配件"
        confirm = float(p.get("mcs_confirmquantity") or 0)
        if qty < 0 or qty > confirm:
            raise RuntimeError(f"{name} 的消耗数量只能在 0 到确认数量 {confirm:g} 之间")
        if p.get("_mcs_partswriteoffdocid_value"):
            raise RuntimeError(f"{name} 已核销（{p.get('_mcs_partswriteoffdocid_value' + FV) or '核销单'}），不能再改消耗")
        if p.get("_mcs_retunorderid_value"):
            raise RuntimeError(f"{name} 已在归还单 {p.get('_mcs_retunorderid_value' + FV) or ''} 上，不能再改消耗")
        so_id = p.get("_mcs_serviceorder_value")
        so = self.browser.fetch_json(self.base, self._api(f"{ENTITY_SET}({so_id})?$select=mcs_orderstatus,mcs_ordernumber"))
        if so.get("mcs_orderstatus") in CONSUMPTION_LOCKED:
            raise RuntimeError(f"工单 {so.get('mcs_ordernumber')} 当前状态为「{so.get('mcs_orderstatus' + FV)}」，不能再改配件消耗")
        old = float(p.get("mcs_consumptionquantity") or 0)
        self.browser.fetch_json(self.base, self._api(f"mcs_serviceparts({part_id})"), method="PATCH",
                                body={"mcs_consumptionquantity": qty}, headers={"If-Match": "*"})
        return {"order_d365_id": so_id, "name": name, "old": old, "new": qty, "confirm": confirm}

    # ---------- 加配件 / 申请借料 / 提交审批 ----------
    # 规则照 D365：工单表单 mcs_serviceorder.js 的「申请备件」（ApplyForParts + ApplyForPartsEnableRule +
    # WriteOffInspection），借料单表单 mcs_repairmaterialorder.js 的「提交审批」（SubmitApproval）
    def search_parts(self, keyword: str) -> list[dict]:
        """按物料号（开头匹配）或英文名称（包含）查 D365 物料主数据 mcs_part。"""
        kw = _q(keyword.strip())
        if len(kw) < 2:
            return []
        by_no = any(c.isdigit() for c in kw) and " " not in kw
        flt = f"startswith(mcs_name,'{kw}')" if by_no else f"contains(mcs_partname,'{kw}')"
        rows = self.browser.fetch_json(self.base, self._api(
            f"mcs_parts?$select=mcs_partid,mcs_name,mcs_partname,mcs_deleteflag"
            f"&$filter={quote(flt + ' and statecode eq 0')}&$top=30&$orderby=mcs_name")).get("value", [])
        return [{"id": r["mcs_partid"], "no": r.get("mcs_name") or "", "name": r.get("mcs_partname") or ""}
                for r in rows if not r.get("mcs_deleteflag")]

    def _order_live(self, rid: str) -> dict:
        sel = ("mcs_ordernumber,mcs_orderstatus,mcs_ordertype,mcs_existfault,mcs_serviceordermode,mcs_offlineoperation,"
               "_mcs_dealerid_value,mcs_fullmaintenance,_mcs_fullmaintenancecontractid_value,_mcs_country_value,"
               "_mcs_region_value,mcs_receiver,mcs_receiverphone,mcs_receiveraddress")
        return self.browser.fetch_json(self.base, self._api(f"{ENTITY_SET}({rid})?$select={sel}"))

    def add_part(self, rid: str, part: dict, qty: float) -> dict:
        """给工单新增一条服务配件（同 D365 快速创建表单：配件、数量、所属工单），
        行号、确认数量、价格等由 D365 插件生成。"""
        if not qty or qty <= 0:
            raise RuntimeError("数量要大于 0")
        o = self._order_live(rid)
        if o.get("mcs_orderstatus") in FINAL_CODES:
            raise RuntimeError(f"工单 {o.get('mcs_ordernumber')} 当前状态为「{o.get('mcs_orderstatus' + FV)}」，不能再加配件")
        body = {"mcs_serviceorder@odata.bind": f"/{ENTITY_SET}({rid})", "mcs_part@odata.bind": f"/mcs_parts({part['id']})",
                "mcs_quantity": qty, "mcs_dealerstock": False}
        if part.get("name"):
            body["mcs_partfullname"] = part["name"]
        r = self.browser.fetch_json(self.base, self._api("mcs_serviceparts?$select=mcs_servicepartid,mcs_name"),
                                    method="POST", body=body, headers={"Prefer": "return=representation"})
        return {"id": r.get("mcs_servicepartid"), "line": r.get("mcs_name") or ""}

    def delete_part(self, part_id: str) -> str:
        """删掉还没申请（不在借料单上）、没核销、没消耗的服务配件。"""
        sel = ("mcs_partfullname,mcs_consumptionquantity,_mcs_repairmaterialorderid_value,"
               "_mcs_partswriteoffdocid_value,_mcs_retunorderid_value")
        p = self.browser.fetch_json(self.base, self._api(f"mcs_serviceparts({part_id})?$select={sel}"))
        name = p.get("mcs_partfullname") or "配件"
        if p.get("_mcs_repairmaterialorderid_value"):
            raise RuntimeError(f"{name} 已经在借料单 {p.get('_mcs_repairmaterialorderid_value' + FV) or ''} 上，不能删除")
        if p.get("_mcs_partswriteoffdocid_value") or p.get("_mcs_retunorderid_value") or (p.get("mcs_consumptionquantity") or 0) > 0:
            raise RuntimeError(f"{name} 已有消耗、核销或归还记录，不能删除")
        self.browser.fetch_json(self.base, self._api(f"mcs_serviceparts({part_id})"), method="DELETE")
        return name

    def set_receiver(self, rid: str, receiver: str, phone: str, address: str):
        """申请备件前要填的收货人 / 电话 / 地址（写在工单上）。"""
        self.browser.fetch_json(self.base, self._api(f"{ENTITY_SET}({rid})"), method="PATCH", headers={"If-Match": "*"},
                                body={"mcs_receiver": receiver, "mcs_receiverphone": phone, "mcs_receiveraddress": address})

    def _apply_rule(self, o: dict) -> str:
        """同 ApplyForPartsEnableRule（非印度）：返回不能申请的原因，空 = 可以。"""
        st, typ, mode = o.get("mcs_orderstatus"), o.get("mcs_ordertype"), o.get("mcs_serviceordermode")
        stf = o.get("mcs_orderstatus" + FV) or st
        if typ and typ != 80 and not o.get("mcs_existfault"):
            return "工单「是否存在故障」为否，D365 不允许申请备件"
        if o.get("_mcs_fullmaintenancecontractid_value") and o.get("mcs_fullmaintenance"):
            c = self.browser.fetch_json(self.base, self._api(
                f"mcs_fullmaintenancecontracts({o['_mcs_fullmaintenancecontractid_value']})?$select=mcs_contractentity"))
            if c.get("mcs_contractentity") == 2:
                return "全保合同工单不在这里申请备件"
        if typ == 40 and o.get("_mcs_dealerid_value"):
            return "" if st is not None and 70 <= st < 100 else f"工单状态为「{stf}」，只有已接收到现场完工之前可以申请备件"
        if mode != 20 and o.get("mcs_offlineoperation") and st == 100:
            return ""
        if typ in (30, 40, 110) and st == 96:
            return "工单处于「暂停维修」，D365 不允许申请备件"
        if mode == 20:
            return "服务代理商模式的工单不能在这里申请备件"
        return "" if st is not None and 70 <= st < 100 else f"工单状态为「{stf}」，只有已接收到现场完工之前可以申请备件"

    def _writeoff_block(self, o: dict) -> str:
        """同 WriteOffInspection：当前用户在本大区有超期未核销的借料、且催办次数已到上限时，不允许再申请。"""
        me = self.whoami().get("UserId")
        pers = self.browser.fetch_json(self.base, self._api(
            f"mcs_personnels?$select=mcs_domainaccount&$filter={quote(f'statecode eq 0 and _mcs_systemuserid_value eq {me}')}"
        )).get("value", [])
        dom = (pers[0].get("mcs_domainaccount") if pers else "") or ""
        region = o.get("_mcs_region_value")
        if o.get("_mcs_country_value"):
            c = self.browser.fetch_json(self.base, self._api(
                f"mcs_countries({o['_mcs_country_value']})?$select=mcs_isenable,_mcs_region_value"))
            if c.get("mcs_isenable"):
                region = c.get("_mcs_region_value")
        if not region:
            return ""
        cfg = self.browser.fetch_json(self.base, self._api(
            "mcs_writeoffconfigurationtables?$select=mcs_unwrittendays,mcs_remindersnumber,mcs_accountwhitelist"
            f"&$filter={quote(f'statecode eq 0 and _mcs_region_value eq {region}')}&$top=1")).get("value", [])
        if not cfg:
            return ""
        cfg = cfg[0]
        if dom and dom in (cfg.get("mcs_accountwhitelist") or "").split(","):
            return ""
        flt = f"statecode eq 0 and _mcs_region_value eq {region} and mcs_name eq '{_q(dom)}' and mcs_writeoffstatus eq 1"
        det = self.browser.fetch_json(self.base, self._api(
            f"mcs_unwrittenborrowingdetailses?$select=mcs_unwrittendays,mcs_remindersnumber&$filter={quote(flt)}"
            "&$orderby=mcs_unwrittendays desc&$top=1")).get("value", [])
        if det and (det[0].get("mcs_unwrittendays") or 0) > (cfg.get("mcs_unwrittendays") or 0) \
                and (det[0].get("mcs_remindersnumber") or 0) >= (cfg.get("mcs_remindersnumber") or 0):
            return "你有超期未核销的借料单。按总部规定，要先完成核销才能再申请备件（D365 同样会拦下）"
        return ""

    def apply_check(self, rid: str) -> dict:
        """申请借料前的检查：不能申请的原因、收货信息、这次会申请的配件（还没在借料单上的）。"""
        o = self._order_live(rid)
        parts = [normalize_part(r) for r in self._by_orders("mcs_serviceparts", PART_SELECT, "_mcs_serviceorder_value", [rid])]
        pending = [p for p in parts if not p["material_id"]]
        blockers = [x for x in (self._apply_rule(o),) if x]
        if not pending:
            blockers.append("没有待申请的配件：请先添加配件（已在借料单上的不会重复申请）")
        if not blockers:
            w = self._writeoff_block(o)
            if w:
                blockers.append(w)
        return {"order_no": o.get("mcs_ordernumber"), "blockers": blockers, "pending": pending,
                "receiver": o.get("mcs_receiver") or "", "phone": o.get("mcs_receiverphone") or "",
                "address": o.get("mcs_receiveraddress") or ""}

    def apply_parts(self, rid: str) -> str:
        """申请备件（Service_ApplyForPartsApi），生成借料单，返回借料单 id。"""
        chk = self.apply_check(rid)
        if chk["blockers"]:
            raise RuntimeError("；".join(chk["blockers"]))
        missing = [n for n, k in (("收货人", "receiver"), ("收货电话", "phone"), ("收货地址", "address")) if not chk[k].strip()]
        if missing:
            raise RuntimeError("请先填写" + "、".join(missing))
        data = self.browser.fetch_json(self.base, self._api("Service_ApplyForPartsApi"), method="POST", body={"OrderId": rid})
        res = data.get("Result")
        if isinstance(res, str) and res.strip().startswith("{"):
            j = json.loads(res)
            if j.get("IsSuccess") is False:
                raise RuntimeError(j.get("Message") or "申请备件失败")
            res = j.get("Data") or j.get("Id") or ""
        return str(res or "")

    def _material_live(self, mid: str) -> dict:
        sel = "mcs_code,mcs_type,mcs_orderstatus,_mcs_seller_value,_mcs_customer_value,_mcs_serviceorderid_value"
        return self.browser.fetch_json(self.base, self._api(f"mcs_repairmaterialorders({mid})?$select={sel}"))

    def material_check(self, mid: str) -> dict:
        """同 SubmitApproval：先检查能不能提交，再取 D365 的配件替换提醒（只提醒、不拦）。"""
        m = self._material_live(mid)
        code, typ, st = m.get("mcs_code"), m.get("mcs_type"), m.get("mcs_orderstatus")
        blockers = []
        if st not in (10, 40):
            blockers.append(f"{code} 当前状态为「{m.get('mcs_orderstatus' + FV)}」，只有已创建或已拒绝的单可以提交审批")
        if typ == MAT_RETURN:
            blockers.append("归还单请在 D365 里提交")
        so = m.get("_mcs_serviceorderid_value")
        in_site = True
        if so:
            t = self.browser.fetch_json(self.base, self._api(f"{ENTITY_SET}({so})?$select=mcs_ordertype")).get("mcs_ordertype")
            in_site = t in (10, 50, 70, 100, 40)
        if not in_site and typ in (20, 40):
            if m.get("_mcs_seller_value") and m.get("_mcs_seller_value") == m.get("_mcs_customer_value"):
                blockers.append("借料单的销售方和客户相同，D365 不允许提交")
            if m.get("_mcs_customer_value"):
                a = self.browser.fetch_json(self.base, self._api(f"accounts({m['_mcs_customer_value']})?$select=mcs_accountstage"))
                if a.get("mcs_accountstage") == 1:
                    blockers.append("客户还是潜在客户，D365 不允许提交审批")
        n = len(self.browser.fetch_json(self.base, self._api(
            f"mcs_serviceparts?$select=mcs_servicepartid&$filter={quote(f'_mcs_repairmaterialorderid_value eq {mid}')}")).get("value", []))
        if not n:
            blockers.append("借料单上没有配件")
        warnings = []
        if not blockers:
            res = self._action("Service_CheckPartsReplacementInfoApi", {"OrderId": mid})
            if isinstance(res, dict) and res.get("Result") and res.get("Description"):
                try:
                    warnings = [str(x) for x in json.loads(res["Description"]) or []]
                except (TypeError, ValueError):
                    warnings = [str(res["Description"])]
        return {"code": code, "blockers": blockers, "warnings": warnings, "parts": n}

    def submit_material(self, mid: str) -> str:
        chk = self.material_check(mid)
        if chk["blockers"]:
            raise RuntimeError("；".join(chk["blockers"]))
        self.browser.fetch_json(self.base, self._api(f"mcs_repairmaterialorders({mid})"), method="PATCH",
                                body={"mcs_orderstatus": 20}, headers={"If-Match": "*"})
        return chk["code"]

    def materials_for(self, order_ids: list[str]) -> list[dict]:
        return [normalize_material(r) for r in
                self._by_orders("mcs_repairmaterialorders", MATERIAL_SELECT, "_mcs_serviceorderid_value", order_ids)]

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


PART_SELECT = [
    "mcs_servicepartid", "_mcs_serviceorder_value", "_mcs_part_value", "mcs_partfullname", "mcs_quantity",
    "mcs_confirmquantity", "mcs_remainingquantity", "mcs_consumptionquantity", "mcs_ischarge", "mcs_price",
    "mcs_discount", "mcs_totalamount", "_mcs_repairmaterialorderid_value", "_mcs_partswriteoffdocid_value",
    "_mcs_retunorderid_value", "createdon", "modifiedon",
]
MATERIAL_SELECT = [
    "mcs_repairmaterialorderid", "_mcs_serviceorderid_value", "mcs_code", "mcs_type", "mcs_orderstatus",
    "mcs_returnmaterialstatus", "mcs_shipmentstatus", "_mcs_borroworder_value", "mcs_orderreason",
    "createdon", "modifiedon",
]
# mcs_repairmaterialorder.mcs_type：10 向事业部领料 · 20 向子公司借料 · 30 归还单 · 40 向子公司领料 · 50 向寄售库领料
MAT_BORROW, MAT_RETURN = 20, 30
# 这些工单状态下不允许改配件消耗：已取消、提交审批、服务完工、远程完工、异常完工
CONSUMPTION_LOCKED = {15, 105, 110, 130, 140}


def _fv(rec: dict, k: str):
    return rec.get(k + FV) or (rec.get(k) if rec.get(k) is not None else "")


def normalize_part(r: dict) -> dict:
    return {
        "id": r["mcs_servicepartid"], "order_d365_id": r.get("_mcs_serviceorder_value") or "",
        "part_no": _fv(r, "_mcs_part_value"), "name": r.get("mcs_partfullname") or "",
        "quantity": r.get("mcs_quantity") or 0, "confirm_qty": r.get("mcs_confirmquantity") or 0,
        "remaining_qty": r.get("mcs_remainingquantity") or 0, "consumption_qty": r.get("mcs_consumptionquantity") or 0,
        "writeoff_doc": str(_fv(r, "_mcs_partswriteoffdocid_value") or ""),
        "return_order": str(_fv(r, "_mcs_retunorderid_value") or ""),
        "is_charge": int(bool(r.get("mcs_ischarge"))),
        "price": r.get("mcs_price") or 0, "discount": r.get("mcs_discount") or 0,
        "total": r.get("mcs_totalamount") or 0, "total_text": str(_fv(r, "mcs_totalamount")),
        "material_id": r.get("_mcs_repairmaterialorderid_value") or "",
        "material_code": _fv(r, "_mcs_repairmaterialorderid_value"),
        "created": to_local(r.get("createdon")), "modified": to_local(r.get("modifiedon")),
    }


def normalize_material(r: dict) -> dict:
    return {
        "id": r["mcs_repairmaterialorderid"], "order_d365_id": r.get("_mcs_serviceorderid_value") or "",
        "code": r.get("mcs_code") or "", "type_code": r.get("mcs_type"), "type": _fv(r, "mcs_type"),
        "status_code": r.get("mcs_orderstatus"), "status": _fv(r, "mcs_orderstatus"),
        "return_code": r.get("mcs_returnmaterialstatus"), "return_status": _fv(r, "mcs_returnmaterialstatus"),
        "shipment": _fv(r, "mcs_shipmentstatus"), "borrow_id": r.get("_mcs_borroworder_value") or "",
        "borrow_code": _fv(r, "_mcs_borroworder_value"), "reason": _fv(r, "mcs_orderreason"),
        "created": to_local(r.get("createdon")), "modified": to_local(r.get("modifiedon")),
    }


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
