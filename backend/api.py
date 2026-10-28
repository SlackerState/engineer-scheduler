"""界面调用的接口。桌面窗口里经 pywebview 的 js_api 调用，网页模式下经 server.py 的 /api/<方法名> 调用。"""
from __future__ import annotations

import base64
import json
import threading
import webbrowser
from datetime import date, datetime, timedelta

from . import excel_io, logic, parts, workload
from .browser import Browser, NeedLogin
from .d365 import D365, normalize
from .db import DB, ORDER_EDITABLE, ORDER_STATUSES, SLOTS, STATUSES, app_dir
from .sync import Syncer
from .updater import Updater
from .version import __version__

RISK_OPEN = ("overdue", "soon", "plan_late")


class Api:
    def __init__(self, db: DB | None = None):
        self._db = db or DB()
        self._browser = Browser(app_dir(), self._db.settings)
        self._d365 = D365(self._browser, self._db.settings)
        self._sync = Syncer(self._db, self._d365)
        self._updater = Updater(self._db.settings)
        self._updater.cleanup_old()
        self._update_result = self._updater.pop_result()  # 上次在线更新的结果，界面提示一次
        self._sync.rebuild_dispatches()  # 排班规则可能随版本变化，启动时按当前规则重排一次
        self._window = None  # 桌面模式下由 main.py 注入，用于文件对话框
        self._ui_proc = None  # Windows 的 Edge 应用窗口进程，由 main.py 注入
        self._quitting = False  # 在线更新时置为 True，main.py 不再等页面心跳

    # ---------- 基础 ----------
    def bootstrap(self):
        noon = int(self._db.settings().get("noon_hour") or 12)
        return {
            "settings": self._db.settings(), "engineers": self._db.engineers(),
            "statuses": STATUSES, "order_statuses": ORDER_STATUSES, "slots": SLOTS,
            "today": date.today().isoformat(),
            "slot_now": "上午" if datetime.now().hour < noon else "下午",
            "desktop": self._window is not None, "data_dir": str(app_dir()), "version": __version__,
            "update_result": self._update_result,
        }

    # ---------- 在线更新 ----------
    def update_check(self):
        try:
            return self._updater.check()
        except Exception as e:  # noqa: BLE001 - 网络问题不影响使用
            return {**self._updater.state, "available": False, "message": f"检查更新失败：{e}"}

    def update_download(self):
        return self._updater.start_download()

    def update_state(self):
        return self._updater.state

    def update_apply(self):
        """启动替换脚本后退出本程序，脚本会替换文件并重新打开新版本。"""
        self._updater.apply()

        def quit_soon():
            import os
            import time
            time.sleep(0.8)
            if self._window is not None:
                self._window.destroy()  # main.py 随后关闭浏览器并退出
            elif self._ui_proc is not None:
                self._quitting = True  # main.py 不再等页面心跳
                self._ui_proc.terminate()  # 关掉 Edge 应用窗口（页面自己也会关窗），main.py 随后退出
            else:
                self.shutdown()
                os._exit(0)
            # 兜底：正常退出流程卡住时也要让出程序文件，否则更新没法替换
            time.sleep(8)
            os._exit(0)
        threading.Thread(target=quit_soon, daemon=True).start()
        return {"ok": True}

    def save_settings(self, data):
        return self._db.save_settings(data)

    # ---------- 人员 ----------
    def list_engineers(self, include_inactive=False):
        return self._db.engineers(bool(include_inactive))

    def save_engineer(self, data):
        eid = self._db.save_engineer(data)
        self._sync.rebuild_dispatches()  # 姓名/别名变了，工单对应的工程师可能变
        return eid

    def delete_engineer(self, eid):
        res = self._db.delete_engineer(int(eid))
        self._sync.rebuild_dispatches()
        return res

    def restore_engineer(self, eid):
        self._db.restore_engineer(int(eid))
        self._sync.rebuild_dispatches()

    def move_engineer(self, eid, delta):
        self._db.move_engineer(int(eid), int(delta))

    # ---------- 排班 ----------
    def schedule(self, start, days):
        return logic.schedule_grid(self._db, start, days)

    def set_cell(self, eid, day, slot, status):
        if status and status not in STATUSES:
            raise ValueError(f"未知状态：{status}")
        self._db.set_cell(int(eid), day, slot, status)

    def set_day(self, eid, day, status):
        """排班表按整天改：上午下午一起设。"""
        for slot in SLOTS:
            self.set_cell(eid, day, slot, status)

    def day_status(self, day, slot):
        return logic.day_status(self._db, day, slot)

    # ---------- 派单 ----------
    def check_dispatch(self, form):
        return logic.check_dispatch(self._db, form)

    def submit_dispatch(self, form, force=False):
        return logic.submit_dispatch(self._db, form, bool(force))

    def list_dispatches(self, day_from="", day_to=""):
        return logic.list_dispatches(self._db, day_from, day_to)

    def delete_dispatch(self, did):
        d = self._db.one("SELECT * FROM dispatches WHERE id=?", (int(did),))
        self._db.x("DELETE FROM dispatches WHERE id=?", (int(did),))
        if d:
            logic.unmark_out(self._db, d["engineer_id"], d["day"], d["slot"])

    # ---------- 订单 ----------
    def _enrich(self, rows: list[dict]) -> list[dict]:
        s = self._db.settings()
        for r in rows:
            r.pop("d365_json", None)
            logic.enrich(r, s)
        return rows

    def list_orders(self, status="", keyword="", engineer=""):
        sql, p = "SELECT * FROM orders WHERE 1=1", []
        if status in ("未完成", "超期风险", "待排期"):
            sql += " AND status NOT IN ('已完成','已取消')"
        elif status and status != "待归还配件":
            sql += " AND status=?"
            p.append(status)
        if engineer:
            sql += " AND engineer LIKE ?"
            p.append(f"%{engineer}%")
        if keyword:
            sql += (" AND (service_order_no LIKE ? OR udesk_no LIKE ? OR customer LIKE ? OR serial LIKE ? "
                    "OR problem LIKE ? OR progress LIKE ? OR loan_no LIKE ?)")
            p += [f"%{keyword}%"] * 7
        sql += " ORDER BY day DESC, id DESC"
        rows = self._enrich(self._db.q(sql, p))
        todo = parts.to_return_by_order(self._db)
        for r in rows:
            r["to_return"] = todo.get(r["d365_id"], 0)
        if status == "待归还配件":
            rows = [r for r in rows if r["to_return"]]
        if status == "超期风险":
            rows = [r for r in rows if r["risk"]["level"] in RISK_OPEN]
            rows.sort(key=lambda r: r["appointment"])
        elif status == "待排期":
            rows = [r for r in rows if r["d365_id"] and not r["visit"]]
            rows.sort(key=lambda r: r["appointment"] or "9999")
        return rows

    def unscheduled(self):
        """待排期：还没出发/到达、也没定本地计划的未完结 D365 工单，按预约截止时间排序。"""
        return self.list_orders("待排期")

    def set_plan(self, oid, day, slot=""):
        """设置本地计划上门时间（day 为空 = 清除），并同步派单和排班。只存软件里，不写 D365。"""
        if day and slot not in SLOTS:
            slot = "上午"
        self._db.update_order(int(oid), {"planned_day": day or "", "planned_slot": slot if day else ""})
        o = self._db.one("SELECT * FROM orders WHERE id=?", (int(oid),))
        self._sync.place_order(o)
        return self.get_order(oid)

    def risk_summary(self):
        rows = self.list_orders("未完成")
        count = lambda lv: sum(1 for r in rows if r["risk"]["level"] == lv)  # noqa: E731
        return {"overdue": count("overdue"), "soon": count("soon"), "plan_late": count("plan_late"),
                "unscheduled": sum(1 for r in rows if r["d365_id"] and not r["visit"]),
                "to_return": sum(parts.to_return_by_order(self._db).values())}

    # ---------- 配件（只读，来自 D365） ----------
    def _mat_link(self, rows: list[dict], key: str = "id"):
        for r in rows:
            if r.get(key):
                r["d365_link"] = self._d365.record_url(r[key], "mcs_repairmaterialorder")
        return rows

    def parts_borrows(self, state=""):
        rows = parts.borrows(self._db)
        counts: dict = {}
        for r in rows:
            counts[r["state"]] = counts.get(r["state"], 0) + 1
        if state == "需处理":
            rows = [r for r in rows if r["state"] in parts.TODO]
        elif state:
            rows = [r for r in rows if r["state"] == state]
        order = {"归还被拒": 0, "待归还": 1, "审批通过待退": 2, "归还审批中": 3, "借用中": 4, "已用完": 5, "已归还": 6}
        rows.sort(key=lambda r: (order.get(r["state"], 9), r["created"]))
        return {"rows": self._mat_link(rows), "counts": counts}

    def parts_charges(self, day_from="", day_to="", engineer="", only_charge=True):
        return parts.charges(self._db, day_from, day_to, engineer, bool(only_charge))

    def order_parts(self, oid):
        o = self._db.one("SELECT d365_id FROM orders WHERE id=?", (int(oid),))
        if not o or not o["d365_id"]:
            return {"parts": [], "materials": [], "borrows": [], "charge_total": 0, "currency": ""}
        res = parts.order_parts(self._db, o["d365_id"])
        self._mat_link(res["materials"])
        return res

    def set_part_consumption(self, oid, part_id, qty):
        """把配件消耗数量写回 D365，然后重新读取这一单的配件（剩余数量由 D365 重算）。"""
        try:
            r = self._d365.set_consumption(part_id, float(qty))
        except NeedLogin as e:
            raise RuntimeError(f"{e}，请先点「同步 D365」登录") from e
        self._sync.sync_parts([r["order_d365_id"]])
        p = self._db.one("SELECT remaining_qty FROM parts WHERE id=?", (part_id,))
        r["remaining"] = p["remaining_qty"] if p else None
        return {**self.order_parts(oid), "changed": r}

    def refresh_order_parts(self, oid, quiet=False):
        """从 D365 重新读取这一单的配件和借料/归还单。quiet：打开详情时的自动刷新，失败就用本地数据。"""
        o = self._d365_order(oid)
        try:
            self._sync.sync_parts([o["d365_id"]])
        except Exception as e:  # noqa: BLE001
            if not quiet:
                if isinstance(e, NeedLogin):
                    raise RuntimeError(f"{e}，请先点「同步 D365」登录") from e
                raise
            res = self.order_parts(oid)
            res["stale"] = ("没登录 D365" if isinstance(e, NeedLogin) else "没连上 D365") + "，显示的是上次同步的数据"
            res["stale_detail"] = str(e).splitlines()[0][:200] if str(e) else ""
            return res
        return self.order_parts(oid)

    def orders_since(self, day, status=""):
        """创建日期在 day 及之后的工单，按创建时间倒序。"""
        sql, p = "SELECT * FROM orders WHERE day >= ?", [day]
        if status == "未完成":
            sql += " AND status NOT IN ('已完成','已取消')"
        elif status:
            sql += " AND status=?"
            p.append(status)
        rows = self._enrich(self._db.q(sql + " ORDER BY day DESC, id DESC", p))
        lookback = int(self._db.settings().get("lookback_days") or 30)
        earliest = (date.today() - timedelta(days=lookback)).isoformat()
        return {"rows": rows, "synced_from": earliest, "beyond_sync": day < earliest}

    def sync_since(self, day):
        """从 D365 拉取 day 及之后创建的工单（超出平时同步范围时用）。"""
        return self._sync.start(since=day)

    def get_order(self, oid):
        o = self._db.one("SELECT * FROM orders WHERE id=?", (int(oid),))
        if o:
            self._enrich([o])
            if o.get("d365_id"):
                o["d365_link"] = self._d365.record_url(o["d365_id"])
        return o

    def save_order(self, data):
        fields = {k: (data.get(k) or "") for k in ORDER_EDITABLE if k in data}
        if data.get("id"):
            self._db.update_order(int(data["id"]), fields)
            return int(data["id"])
        cols = list(fields)
        return self._db.x(f"INSERT INTO orders({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                          list(fields.values()))

    def delete_order(self, oid):
        o = self._db.one("SELECT d365_id FROM orders WHERE id=?", (int(oid),))
        if o and o["d365_id"]:
            self._db.x("DELETE FROM dispatches WHERE booking_id=?", (f"so:{o['d365_id']}",))
        self._db.x("DELETE FROM orders WHERE id=?", (int(oid),))

    def open_url(self, url):
        webbrowser.open(url)

    # ---------- D365 ----------
    def login_d365(self):
        """弹出登录窗口，登录成功后自动关窗并同步一次。"""
        return self._sync.start(login_first=True)

    def check_login(self):
        try:
            me = self._d365.whoami()
            self._sync.state["need_login"] = False
            return {"ok": True, "user_id": me.get("UserId", "")}
        except NeedLogin as e:
            self._sync.state["need_login"] = True
            return {"ok": False, "message": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "message": str(e)}

    def find_d365(self, number):
        try:
            rows = [normalize(r) for r in self._d365.find(number)]
        except NeedLogin as e:
            return {"ok": False, "need_login": True, "message": str(e)}
        for r in rows:
            r.pop("d365_json", None)
            r["d365_link"] = self._d365.record_url(r["d365_id"])
            r["exists"] = bool(self._db.one("SELECT id FROM orders WHERE d365_id=?", (r["d365_id"],)))
        return {"ok": True, "rows": rows}

    def add_from_d365(self, number):
        recs = self._d365.find(number)
        engineers = self._db.engineers(include_inactive=True)
        s = self._db.settings()
        for rec in recs:
            row = normalize(rec)
            self._sync.upsert(row, s)
            if s.get("d365_to_dispatch", True):
                self._sync.sync_dispatch(row, engineers)
        return len(recs)

    # ---------- 写回 D365 ----------
    def _d365_order(self, oid) -> dict:
        o = self._db.one("SELECT id, d365_id, service_order_no FROM orders WHERE id=?", (int(oid),))
        if not o or not o["d365_id"]:
            raise ValueError("这条订单不是从 D365 同步来的，无法写回 D365")
        return o

    def _refresh_from_d365(self, d365_ids: list[str]):
        """写入后立即从 D365 读回最新数据，并同步派单记录和排班表。"""
        s, engineers = self._db.settings(), self._db.engineers(include_inactive=True)
        for rec in self._d365.by_ids(d365_ids):
            row = normalize(rec)
            self._sync.upsert(row, s)
            if s.get("d365_to_dispatch", True):
                self._sync.sync_dispatch(row, engineers)

    def d365_engineers(self, oid, keyword=""):
        o = self._d365_order(oid)
        rows = self._d365.engineer_list(o["d365_id"], keyword or "")
        engineers = self._db.engineers()
        for r in rows:  # 人员表里的人排在前面，并标出本地姓名
            r["local"] = (logic.match_engineer(engineers, r["name"]) or {}).get("name", "")
        rows.sort(key=lambda r: (not r["local"], r["name"].lower()))
        return rows

    def d365_assign(self, oids, person_id, person_name=""):
        """把一单或多单派给 / 转派给 D365 里的某位工程师，逐单返回结果。"""
        results, done = [], []
        for oid in oids if isinstance(oids, list) else [oids]:
            o = self._d365_order(oid)
            try:
                verb = self._d365.assign_engineer(o["d365_id"], person_id)
                results.append({"id": o["id"], "no": o["service_order_no"], "ok": True, "message": f"已{verb}给 {person_name}"})
                done.append(o["d365_id"])
            except NeedLogin:
                raise
            except Exception as e:  # noqa: BLE001 - 单张失败不影响其它
                results.append({"id": o["id"], "no": o["service_order_no"], "ok": False, "message": str(e)})
        if done:
            self._refresh_from_d365(done)
        return results

    def d365_set_appointment(self, oid, value):
        """value 形如 '2026-10-28T09:30'（本机时间）。"""
        o = self._d365_order(oid)
        self._d365.set_appointment(o["d365_id"], datetime.fromisoformat(value).astimezone())
        self._refresh_from_d365([o["d365_id"]])
        return self.get_order(oid)

    def open_udesk(self):
        webbrowser.open(self._db.settings().get("udesk_url") or "https://sany.s5.udesk.cn/entry/")

    def start_sync(self):
        return self._sync.start()

    def sync_state(self):
        return self._sync.state

    # ---------- Excel ----------
    def import_excel(self, b64):
        return excel_io.import_workbook(self._db, base64.b64decode(b64))

    def export_excel(self, start, days):
        return self._save_xlsx(excel_io.export_workbook(self._db, start, int(days)),
                               f"工程师排班表_{date.today():%Y%m%d}.xlsx")

    def _save_xlsx(self, data: bytes, name: str):
        """桌面模式弹保存对话框；网页模式把内容交给前端下载。"""
        if self._window is not None:
            import webview
            path = self._window.create_file_dialog(webview.SAVE_DIALOG, save_filename=name,
                                                   file_types=("Excel 文件 (*.xlsx)",))
            if not path:
                return {"saved": False}
            path = path if isinstance(path, str) else path[0]
            with open(path, "wb") as f:
                f.write(data)
            return {"saved": True, "path": path}
        return {"saved": False, "name": name, "b64": base64.b64encode(data).decode()}

    # ---------- 工作量 ----------
    def workload(self, day_from, day_to):
        return workload.summary(self._db, day_from, day_to)

    def workload_detail(self, key, day_from, day_to):
        return workload.detail(self._db, key, day_from, day_to)

    def export_workload(self, day_from, day_to):
        return self._save_xlsx(excel_io.export_workload(self._db, day_from, day_to),
                               f"工程师工作量_{day_from}_{day_to}.xlsx")

    def open_data_dir(self):
        webbrowser.open(app_dir().as_uri())

    def shutdown(self):
        self._browser.shutdown()


def call(api: Api, method: str, args: list):
    """网页模式的分发入口：只允许调用公开方法。"""
    if method.startswith("_") or not callable(getattr(api, method, None)):
        raise AttributeError(f"没有这个接口：{method}")
    return json.loads(json.dumps(getattr(api, method)(*args), ensure_ascii=False, default=str))
