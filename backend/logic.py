"""排班表 / 今日查询 / 派单录入 的业务规则（对应原 Excel 公式和「提交派单」宏）。"""
from __future__ import annotations

from datetime import date, timedelta

from .db import DB, SLOTS

BLOCKING = ("休假", "培训", "休息", "驻点")


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
       o.appointment, o.problem, o.order_type, o.serial, o.model, o.contact
FROM dispatches d
LEFT JOIN orders o ON (d.booking_id IS NOT NULL AND o.d365_id = substr(d.booking_id, 4))
                   OR (d.booking_id IS NULL AND d.order_no <> '' AND o.service_order_no = d.order_no)
WHERE d.day BETWEEN ? AND ?
ORDER BY d.day, d.slot, o.appointment, d.id
"""


def jobs_between(db: DB, day_from: str, day_to: str) -> list[dict]:
    """派单记录 + 对应的订单（D365 工单）信息。"""
    return db.q(JOB_SQL, (day_from, day_to))


def schedule_grid(db: DB, start: str, days: int) -> dict:
    days = max(1, min(int(days), 366))
    dates = _days(start, days)
    engs = db.engineers()
    raw = {(r["engineer_id"], r["day"], r["slot"]): r["status"]
           for r in db.q("SELECT * FROM schedule WHERE day BETWEEN ? AND ?", (dates[0], dates[-1]))}
    cnt = _dispatch_counts(db, dates[0], dates[-1])
    jobs: dict = {}
    for j in jobs_between(db, dates[0], dates[-1]):
        jobs.setdefault((j["engineer_id"], j["day"], j["slot"]), []).append({
            "order_id": j["order_id"], "no": j["service_order_no"] or j["order_no"], "udesk_no": j["udesk_no"],
            "customer": j["customer"], "d365_status": j["d365_status"], "appointment": j["appointment"]})
    rows = []
    totals = {"外勤": {}, "驻点": {}}
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
        rows.append({**e, "cells": cells})
    return {"dates": dates, "rows": rows, "totals": totals, "today": date.today().isoformat()}


def day_status(db: DB, day: str, slot: str) -> dict:
    out = []
    all_jobs = jobs_between(db, day, day)
    for e in db.engineers():
        raw = db.cell(e["id"], day, slot)
        jobs = [j for j in all_jobs if j["engineer_id"] == e["id"]]
        n_slot = sum(1 for j in jobs if j["slot"] == slot)
        out.append({
            "id": e["id"], "name": e["name"], "type": e["type"],
            "schedule": base_status(raw, e["type"]),
            "status": effective_status(raw, e["type"], n_slot),
            "available": is_available(raw, e["type"], n_slot),
            "day_count": len(jobs), "slot_count": n_slot, "jobs": jobs,
        })
    # 当天所有预约上门的 D365 工单（包括人员表以外的工程师）
    engs = db.engineers()
    orders = db.q("SELECT id, udesk_no, service_order_no, customer, engineer, d365_status, status, appointment, "
                  "address, order_type, problem FROM orders WHERE substr(appointment, 1, 10)=? "
                  "ORDER BY appointment, id", (day,))
    for o in orders:
        o["local"] = (match_engineer(engs, o["engineer"]) or {}).get("name", "")
    return {
        "day": day, "slot": slot, "rows": out, "orders": orders,
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
    """派单后同步排班表：空白/空闲 → 外出；外出不变；其它状态保留并返回提醒。"""
    cur = db.cell(eid, day, slot)
    if cur in ("", "空闲"):
        db.set_cell(eid, day, slot, "外出")
        return ""
    if cur == "外出":
        return ""
    return f"排班表上该时段是「{cur}」，未改成外出，请人工确认。"


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
