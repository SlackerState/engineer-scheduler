"""同步：从 D365 拉取 Udesk 创建的服务订单 → 写入订单清单 → 按预约时间同步派单记录和排班表。"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from . import logic
from .browser import NeedLogin
from .d365 import D365, FINAL_CODES, normalize
from .db import DB

D365_COLUMNS = ["udesk_no", "order_type", "d365_status", "d365_status_code", "appointment",
                "dispatch_time", "setoff_time", "arrival_time", "complete_time", "address",
                "contact", "network", "model", "warranty", "d365_json"]
FILL_IF_EMPTY = ["day", "service_order_no", "customer", "serial", "problem"]


class Syncer:
    def __init__(self, db: DB, d365: D365):
        self.db, self.d365 = db, d365
        self.lock = threading.Lock()
        self.state = {"running": False, "last_run": "", "ok": None, "message": "尚未同步",
                      "need_login": False, "log": []}
        threading.Thread(target=self._auto_loop, name="auto-sync", daemon=True).start()

    def _log(self, msg: str):
        self.state["log"] = (self.state["log"] + [f"{datetime.now():%H:%M:%S} {msg}"])[-200:]

    def start(self, interactive: bool = True, login_first: bool = False, since: str | None = None) -> bool:
        """interactive：没登录时弹出登录窗口（自动同步时不弹）；login_first：先登录再同步；
        since：'YYYY-MM-DD'，拉取这天及之后创建的订单（默认按设置里的回看天数）。"""
        if not self.lock.acquire(blocking=False):
            return False
        self.state.update(running=True, message="同步中…")
        threading.Thread(target=self._run_locked, args=(interactive, login_first, since),
                         name="sync", daemon=True).start()
        return True

    def _login(self):
        self.state["message"] = "请在弹出的浏览器窗口里登录 D365（5 分钟内），登录后会自动继续…"
        self._log("等待登录 D365…")
        self.d365.login()
        self.state["need_login"] = False
        self._log("D365 登录成功")
        self.state["message"] = "登录成功，同步中…"

    def _run_locked(self, interactive: bool, login_first: bool, since: str | None = None):
        try:
            if login_first:
                self._login()
            try:
                summary = self.run(since)
            except NeedLogin:
                if not interactive or login_first:
                    raise
                self._login()
                summary = self.run(since)
            self.state.update(ok=True, need_login=False, message=summary)
        except NeedLogin as e:
            self.state.update(ok=False, need_login=True, message=f"{e}，请点「同步 D365」重新登录")
        except Exception as e:  # noqa: BLE001 - 错误显示给用户
            self.state.update(ok=False, message=f"同步失败：{e}")
        finally:
            self._log(self.state["message"])
            self.state.update(running=False, last_run=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            self.lock.release()

    def _auto_loop(self):
        while True:
            time.sleep(30)
            s = self.db.settings()
            if not s.get("auto_sync") or self.state["running"] or self.state["need_login"]:
                continue
            last = self.state["last_run"]
            due = not last or datetime.now() - datetime.strptime(last, "%Y-%m-%d %H:%M:%S") >= \
                timedelta(minutes=max(1, int(s.get("sync_interval_min") or 15)))
            if due:
                self.start(interactive=False)

    # ---------- 主流程 ----------
    def run(self, since_day: str | None = None) -> str:
        s = self.db.settings()
        sources = [int(x) for x in str(s.get("order_sources") or "").replace("，", ",").split(",") if x.strip()]
        network = (s.get("network_filter") or "").strip()
        if since_day:  # 本机当天 0 点
            since = datetime.fromisoformat(since_day).astimezone().astimezone(timezone.utc)
        else:
            since = datetime.now(timezone.utc) - timedelta(days=int(s.get("lookback_days") or 30))
        self._log(f"从 D365 拉取 {since.astimezone():%Y-%m-%d} 之后创建的订单…")
        recs = self.d365.list_orders(since, sources)
        seen = {r["mcs_serviceorderid"] for r in recs}
        # 回看期之外、但本地还没完结的订单也刷新一下状态
        pending = [r["d365_id"] for r in self.db.q(
            "SELECT d365_id FROM orders WHERE d365_id IS NOT NULL AND status NOT IN ('已完成','已取消')")
            if r["d365_id"] not in seen]
        if pending:
            recs += self.d365.by_ids(pending)
        new = upd = skipped = 0
        engineers = self.db.engineers()
        for rec in recs:
            row = normalize(rec)
            if network and row["network"] and row["network"] != network:
                skipped += 1
                continue
            created = self.upsert(row, s)
            new += created
            upd += not created
            if s.get("d365_to_dispatch", True):
                self.sync_dispatch(row, engineers)
        self.d365.browser.save_state()
        msg = f"同步完成：新增 {new} 单，更新 {upd} 单"
        if skipped:
            msg += f"，跳过其它网点 {skipped} 单"
        return msg

    def upsert(self, row: dict, s: dict) -> bool:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        cur = self.db.one("SELECT * FROM orders WHERE d365_id=?", (row["d365_id"],))
        if not cur and row["service_order_no"]:
            # 从 Excel 导入的旧记录只有服务订单号，这里把它和 D365 关联起来
            cur = self.db.one("SELECT * FROM orders WHERE d365_id IS NULL AND service_order_no=?",
                              (row["service_order_no"],))
        if not cur:
            cols = ["d365_id", *FILL_IF_EMPTY, "engineer", "status", *D365_COLUMNS, "synced_at"]
            vals = [row["d365_id"], *(row[c] for c in FILL_IF_EMPTY), row["engineer"],
                    row["status"] or "待处理", *(row[c] for c in D365_COLUMNS), now]
            self.db.x(f"INSERT INTO orders({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)
            return True
        fields = {c: row[c] for c in D365_COLUMNS}
        fields.update(d365_id=row["d365_id"], synced_at=now)
        for c in FILL_IF_EMPTY:
            if not cur.get(c) and row[c]:
                fields[c] = row[c]
        if row["engineer"]:
            fields["engineer"] = row["engineer"]
        if s.get("d365_status_overrides_local", True) and row["status"]:
            fields["status"] = row["status"]
        self.db.update_order(cur["id"], fields)
        return False

    def sync_dispatch(self, row: dict, engineers: list[dict]):
        o = self.db.one("SELECT * FROM orders WHERE d365_id=?", (row["d365_id"],))
        if o:
            self.place_order(o, engineers)

    def place_order(self, o: dict, engineers: list[dict] | None = None):
        """工单 → 派单记录（每单一条）。上门时间按 实际到达 > 出发 > 本地计划；
        D365 的预约上门时间是考核截止，往往比实际晚很多天，不用来排班。时间变了会自动挪动。"""
        if not o.get("d365_id"):
            return
        engineers = engineers if engineers is not None else self.db.engineers()
        key = f"so:{o['d365_id']}"
        old = self.db.q("SELECT * FROM dispatches WHERE booking_id=?", (key,))
        eng = logic.match_engineer(engineers, o["engineer"])
        v = logic.visit_of(o, int(self.db.settings().get("noon_hour") or 12))
        want = (v["day"], eng["id"], v["slot"]) if eng and v else None
        keep = None
        for d in old:
            if want and (d["day"], d["engineer_id"], d["slot"]) == want and keep is None:
                keep = d
            else:
                self.db.x("DELETE FROM dispatches WHERE id=?", (d["id"],))
                logic.unmark_out(self.db, d["engineer_id"], d["day"], d["slot"])
        if not want:
            return
        info = (o["customer"], o["address"], o["service_order_no"])
        if keep:
            self.db.x("UPDATE dispatches SET customer=?, address=?, order_no=? WHERE id=?", (*info, keep["id"]))
            return
        day, eid, slot = want
        self.db.x("INSERT INTO dispatches(day, engineer_id, slot, customer, address, order_no, source, booking_id) "
                  "VALUES(?,?,?,?,?,?, 'd365', ?)", (day, eid, slot, *info, key))
        warn = logic.mark_out(self.db, eid, day, slot)
        if warn:
            self._log(f"{o['service_order_no']} {eng['name']} {day} {slot}：{warn}")

    def rebuild_dispatches(self) -> int:
        """按当前规则重排所有 D365 工单的派单（升级后、改了人员别名后用）。"""
        engineers = self.db.engineers()
        rows = self.db.q("SELECT * FROM orders WHERE d365_id IS NOT NULL")
        for o in rows:
            self.place_order(o, engineers)
        return len(rows)
