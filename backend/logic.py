"""排班表 / 今日查询 / 派单录入 的业务规则（对应原 Excel 公式和「提交派单」宏）。"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .db import DB, SLOTS

BLOCKING = ("休假", "培训", "休息", "驻点")
# 排班表、工作量按整天显示：上午下午状态不同时，整天算哪个（靠前的优先）
DAY_PRIORITY = ("外出", "驻点", "培训", "休假", "休息", "在岗", "空闲")


def pick_day(statuses: list[str]) -> str:
    """上午/下午两个状态 → 整天的状态；都为空返回空。"""
    vals = [x for x in statuses if x]
    return next((x for x in DAY_PRIORITY if x in vals), vals[0] if vals else "")


def base_status(raw: str, eng_type: str) -> str:
    """排班格子留空时：外勤 = 空闲，驻点 = 在岗。"""
    if raw:
        return raw
    return "在岗" if eng_type == "驻点" else "空闲"


def effective_status(raw: str, eng_type: str, dispatch_count: int) -> str:
    b = base_status(raw, eng_type)
    if dispatch_count > 0:
        return f"⚠ {b}但已派单" if b in BLOCKING else "外出（已派单）"
    return b


def is_available(raw: str, eng_type: str, dispatch_count: int) -> bool:
    """外勤可派 / 驻点在岗 统计口径：空白或空闲（驻点额外算「驻点」），且该时段没派单。"""
    if dispatch_count:
        return False
    ok = ("", "空闲", "驻点") if eng_type == "驻点" else ("", "空闲")
    return raw in ok


def _days(start: str, n: int) -> list[str]:
    s = date.fromisoformat(start)
    return [(s + timedelta(days=i)).isoformat() for i in range(n)]


def _dispatch_counts(db: DB, day_from: str, day_to: str) -> dict:
    rows = db.q("SELECT engineer_id, day, slot, COUNT(*) AS n FROM dispatches "
                "WHERE day BETWEEN ? AND ? GROUP BY engineer_id, day, slot", (day_from, day_to))
    return {(r["engineer_id"], r["day"], r["slot"]): r["n"] for r in rows}


JOB_SQL = """
SELECT d.id AS dispatch_id, d.day, d.slot, d.engineer_id, d.customer, d.address, d.order_no, d.source,
       o.id AS order_id, o.udesk_no, o.service_order_no, o.d365_status, o.status AS order_status,
       o.status, o.d365_status_code, o.appointment, o.arrival_time, o.setoff_time, o.planned_day, o.planned_slot,
       o.problem, o.order_type, o.serial, o.model, o.contact
FROM dispatches d
LEFT JOIN orders o ON (d.booking_id IS NOT NULL AND o.d365_id = substr(d.booking_id, 4))
                   OR (d.booking_id IS NULL AND d.order_no <> '' AND o.service_order_no = d.order_no)
WHERE d.day BETWEEN ? AND ?
ORDER BY d.day, d.slot, o.appointment, d.id
"""


def jobs_between(db: DB, day_from: str, day_to: str) -> list[dict]:
    """派单记录 + 对应的订单（D365 工单）信息、上门时间来源和超期风险。"""
    s = db.settings()
    return [enrich(j, s) if j["order_id"] else j for j in db.q(JOB_SQL, (day_from, day_to))]


def schedule_grid(db: DB, start: str, days: int) -> dict:
    days = max(1, min(int(days), 366))
    dates = _days(start, days)
    raw = {(r["engineer_id"], r["day"], r["slot"]): r["status"]
           for r in db.q("SELECT * FROM schedule WHERE day BETWEEN ? AND ?", (dates[0], dates[-1]))}
    cnt = _dispatch_counts(db, dates[0], dates[-1])
    # 已停用的人员：这段时间里有排班或派单才显示（回看历史用）
    has_data = {k[0] for k in raw} | {k[0] for k in cnt}
    engs = [e for e in db.engineers(include_inactive=True) if e["active"] or e["id"] in has_data]
    jobs: dict = {}
    for j in jobs_between(db, dates[0], dates[-1]):
        jobs.setdefault((j["engineer_id"], j["day"], j["slot"]), []).append({
            "order_id": j["order_id"], "no": j["service_order_no"] or j["order_no"], "udesk_no": j["udesk_no"],
            "customer": j["customer"], "d365_status": j["d365_status"], "appointment": j["appointment"],
            "visit": j.get("visit"), "risk": j.get("risk")})
    rows = []
    totals = {"外勤": {}, "驻点": {}}
    day_totals: dict = {"外勤": {}, "驻点": {}}  # 当天全天可派（上午下午都空）的人数
    for e in engs:
        cells = {}
        for d in dates:
            for s in SLOTS:
                k = (e["id"], d, s)
                r, n = raw.get(k, ""), cnt.get(k, 0)
                state = "normal"
                if n:
                    state = "conflict" if base_status(r, e["type"]) in BLOCKING else "dispatched"
                cells[f"{d}|{s}"] = {"raw": r, "n": n, "state": state, "jobs": jobs.get(k, [])}
                bucket = totals.setdefault(e["type"], {})
                bucket[f"{d}|{s}"] = bucket.get(f"{d}|{s}", 0) + (1 if is_available(r, e["type"], n) else 0)
        # 整天：排班表按天显示
        day_cells = {}
        for d in dates:
            half = [cells[f"{d}|{s}"] for s in SLOTS]
            n = sum(c["n"] for c in half)
            state = ("conflict" if any(c["state"] == "conflict" for c in half)
                     else "dispatched" if n else "normal")
            raws = [c["raw"] for c in half]
            day_cells[d] = {"raw": pick_day(raws), "n": n, "state": state,
                            "jobs": [j for c in half for j in c["jobs"]],
                            # 上午下午填了两种不同状态时（如上午休假、下午培训），鼠标提示里写明
                            "halves": raws if all(raws) and len(set(raws)) > 1 else None}
            bucket = day_totals.setdefault(e["type"], {})
            free = all(is_available(c["raw"], e["type"], c["n"]) for c in half)
            bucket[d] = bucket.get(d, 0) + (1 if free else 0)
        rows.append({**e, "cells": cells, "days": day_cells})
    return {"dates": dates, "rows": rows, "totals": totals, "day_totals": day_totals,
            "today": date.today().isoformat()}


def day_status(db: DB, day: str, slot: str = "") -> dict:
    """某天每位工程师的排班和工单。按整天算：上午下午的排班合成一个（见 pick_day），
    当天有派单就算外出，上午下午都空闲才算「可派」。slot 参数保留兼容，已不使用。"""
    out = []
    all_jobs = jobs_between(db, day, day)
    for e in db.engineers():
        raws = [db.cell(e["id"], day, s) for s in SLOTS]
        jobs = [j for j in all_jobs if j["engineer_id"] == e["id"]]
        n_by = [sum(1 for j in jobs if j["slot"] == s) for s in SLOTS]
        sched = base_status(pick_day(raws), e["type"])
        n = len(jobs)
        status = (f"⚠ {sched}但已派单" if sched in BLOCKING else "外出（已派单）") if n else sched
        out.append({
            "id": e["id"], "name": e["name"], "type": e["type"], "schedule": sched, "status": status,
            "available": all(is_available(r, e["type"], k) for r, k in zip(raws, n_by)),
            "day_count": n, "jobs": jobs,
        })
    # 当天上门的工单（按实际到达 > 出发 > 本地计划；包括人员表以外的工程师）
    engs, s = db.engineers(), db.settings()
    orders = db.q("SELECT * FROM orders WHERE substr(arrival_time, 1, 10)=? "
                  "OR (COALESCE(arrival_time, '')='' AND substr(setoff_time, 1, 10)=?) "
                  "OR (COALESCE(arrival_time, '')='' AND COALESCE(setoff_time, '')='' AND planned_day=?)",
                  (day, day, day))
    orders = [enrich(o, s) for o in orders]
    orders = [o for o in orders if o["visit"] and o["visit"]["day"] == day]
    for o in orders:
        o.pop("d365_json", None)
        o["local"] = (match_engineer(engs, o["engineer"]) or {}).get("name", "")
    orders.sort(key=lambda o: (o["visit"]["time"] or "99", o["id"]))
    return {
        "day": day, "rows": out, "orders": orders,
        "field_available": sum(1 for r in out if r["type"] == "外勤" and r["available"]),
        "site_on_duty": sum(1 for r in out if r["type"] == "驻点" and r["available"]),
    }


REQUIRED = [("day", "日期"), ("slot", "时段"), ("engineer_id", "工程师"),
            ("customer", "客户"), ("address", "地址"), ("order_no", "工单号")]


def check_dispatch(db: DB, f: dict) -> dict:
    missing = [label for key, label in REQUIRED if not str(f.get(key) or "").strip()]
    result = {"missing": missing, "schedule": "", "slot_count": 0, "ok": False, "message": ""}
    if not f.get("engineer_id"):
        result["message"] = "请选择工程师"
        return result
    eid = int(f["engineer_id"])
    raw = db.cell(eid, f.get("day", ""), f.get("slot", "")) if f.get("day") and f.get("slot") else ""
    sched = raw or "空闲"
    n = db.one("SELECT COUNT(*) AS n FROM dispatches WHERE day=? AND engineer_id=? AND slot=?",
               (f.get("day"), eid, f.get("slot")))["n"]
    result.update(schedule=sched, slot_count=n)
    if sched in ("空闲", "外出"):
        result["ok"] = True
        result["message"] = f"✓ 可派（该时段已有 {n} 单）" if n else "✓ 可派"
    else:
        result["message"] = f"⚠ 该时段为{sched}，请换人"
    return result


def mark_out(db: DB, eid: int, day: str, slot: str) -> str:
    """派单后同步排班表：空白/空闲 → 外出（标记为自动）；外出不变；其它状态保留并返回提醒。"""
    cur = db.cell(eid, day, slot)
    if cur in ("", "空闲"):
        db.set_cell(eid, day, slot, "外出", auto=True)
        return ""
    if cur == "外出":
        return ""
    return f"排班表上当天是「{cur}」，未改成外出，请人工确认。"


def unmark_out(db: DB, eid: int, day: str, slot: str):
    """派单撤走后：该时段已无派单、且「外出」是自动标的，就清掉。手工标的不动。"""
    left = db.one("SELECT COUNT(*) AS n FROM dispatches WHERE engineer_id=? AND day=? AND slot=?", (eid, day, slot))
    if not left["n"] and db.cell(eid, day, slot) == "外出" and db.cell_auto(eid, day, slot):
        db.set_cell(eid, day, slot, "")


# ---------- 上门时间与超期（预约上门时间 = 考核截止，往往比实际上门晚很多天） ----------


def _dt(s: str | None) -> datetime | None:
    try:
        return datetime.strptime((s or "")[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def visit_of(o: dict, noon: int = 12) -> dict | None:
    """排班用的上门时间：实际到达 > 出发 > 本地计划；都没有 = 待排期（返回 None）。"""
    if o.get("status") == "已取消" or o.get("d365_status_code") == 15:
        return None
    for key, src in (("arrival_time", "到达"), ("setoff_time", "出发")):
        t = _dt(o.get(key))
        if t:
            return {"day": t.date().isoformat(), "slot": "上午" if t.hour < noon else "下午",
                    "time": t.strftime("%H:%M"), "source": src}
    if o.get("planned_day"):
        return {"day": o["planned_day"], "slot": o.get("planned_slot") or "上午", "time": "", "source": "计划"}
    return None


def _span(hours: float) -> str:
    return f"{int(hours)} 小时" if hours < 48 else f"{int(hours // 24)} 天"


def risk_of(o: dict, soon_hours: float = 24, now: datetime | None = None) -> dict:
    """超期风险（以预约上门时间为考核截止）：
    overdue 已超期未到达 · soon 即将超期 · plan_late 计划上门晚于截止 · late 已到达但超期 · 空 = 无风险"""
    out = {"level": "", "text": "", "left": ""}
    appt = _dt(o.get("appointment"))
    if not appt or o.get("status") == "已取消":
        return out
    arrived = _dt(o.get("arrival_time"))
    if arrived:
        if arrived > appt:
            out.update(level="late", text=f"超期 {_span((arrived - appt).total_seconds() / 3600)} 到达")
        return out
    if o.get("status") == "已完成":
        return out
    left = (appt - (now or datetime.now())).total_seconds() / 3600
    out["left"] = f"已过 {_span(-left)}" if left < 0 else f"剩 {_span(left)}"
    if left < 0:
        out.update(level="overdue", text=f"已超期 {_span(-left)}")
    elif left <= soon_hours:
        out.update(level="soon", text=f"{_span(left)}后超期")
    elif o.get("planned_day"):
        # 计划上门按整天：计划日期晚于截止日期才算「计划晚」
        if o["planned_day"] > appt.date().isoformat() and not _dt(o.get("setoff_time")):
            out.update(level="plan_late", text="计划晚于预约截止")
    return out


def enrich(o: dict, settings: dict) -> dict:
    """给订单加上 visit（排班上门时间）和 risk（超期风险）。"""
    o["visit"] = visit_of(o, int(settings.get("noon_hour") or 12))
    o["risk"] = risk_of(o, float(settings.get("risk_hours") or 24))
    return o


def submit_dispatch(db: DB, f: dict, force: bool = False) -> dict:
    chk = check_dispatch(db, f)
    if chk["missing"]:
        return {"ok": False, "need_confirm": False,
                "message": "请先把" + "、".join(chk["missing"]) + "填齐再提交。"}
    if not chk["ok"] and not force:
        return {"ok": False, "need_confirm": True, "message": chk["message"] + "\n仍要提交吗？"}
    eid = int(f["engineer_id"])
    hours = f.get("hours")
    hours = float(hours) if str(hours or "").strip() else None
    rid = db.x("INSERT INTO dispatches(day, engineer_id, slot, customer, address, order_no, hours) "
               "VALUES(?,?,?,?,?,?,?)",
               (f["day"], eid, f["slot"], f["customer"].strip(), f["address"].strip(),
                str(f["order_no"]).strip(), hours))
    warn = mark_out(db, eid, f["day"], f["slot"])
    name = db.one("SELECT name FROM engineers WHERE id=?", (eid,))["name"]
    msg = f"已提交：{name} {f['day']} {f['slot']}，排班表已同步。"
    return {"ok": True, "id": rid, "message": msg + (("\n" + warn) if warn else "")}


def list_dispatches(db: DB, day_from: str = "", day_to: str = "") -> list[dict]:
    sql = ("SELECT d.*, e.name AS engineer, e.type AS engineer_type, s.status AS raw "
           "FROM dispatches d JOIN engineers e ON e.id = d.engineer_id "
           "LEFT JOIN schedule s ON s.engineer_id = d.engineer_id AND s.day = d.day AND s.slot = d.slot ")
    params: list = []
    cond = []
    if day_from:
        cond.append("d.day >= ?")
        params.append(day_from)
    if day_to:
        cond.append("d.day <= ?")
        params.append(day_to)
    if cond:
        sql += "WHERE " + " AND ".join(cond) + " "
    sql += "ORDER BY d.day DESC, d.slot, e.seq"
    rows = db.q(sql, params)
    for r in rows:
        sched = r.pop("raw") or "空闲"
        r["schedule"] = sched
        r["check"] = "✓" if sched in ("外出", "空闲") else f"⚠ 该时段为{sched}"
    return rows


def clean_person(name: str) -> str:
    """D365 用户名形如 'Simon May  # ' 或 'Winzar  Charles '，去掉 # 和多余空格。"""
    return " ".join((name or "").replace("#", " ").split())


def match_engineer(engineers: list[dict], name: str) -> dict | None:
    """把 D365 工程师名（'Lee Ryan' / 'Winzar Charles'）对应到人员表：先比别名/全名，再比任一单词。"""
    n = clean_person(name).lower()
    if not n:
        return None
    for e in engineers:
        aliases = [clean_person(a).lower() for a in (e.get("aliases") or "").replace("，", ",").split(",")]
        if n == e["name"].lower() or n in aliases:
            return e
    tokens = set(n.split())
    for e in engineers:
        if e["name"].lower() in tokens:
            return e
    return None
