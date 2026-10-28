"""工作量统计：按时间段回看每位工程师的排班和工单（历史数据都在本地，不用再查 D365）。

口径：
- 上门工单：上门时间（实际到达 > 出发 > 本地计划，同排班表）落在这段时间里的工单
- 完工：现场完工时间落在这段时间里的工单
- 现场工时：到达 → 现场完工，单次超过 MAX_ONSITE_H 的多半是完工没及时提交，不计入，单独计数
- 响应：派工 → 到达
- 超期：到达晚于预约上门时间（考核截止），或至今未到达且已过截止
- 排班：只算工作日（周一到周五），按整天算：当天有派单算「外出」，否则按排班表的状态（留空 = 外勤空闲 /
  驻点在岗）；上午下午不一样时按 DAY_PRIORITY 取一个。周末的上门工单照样计入工单数和工时，另计「周末上门」
"""
from __future__ import annotations

from datetime import date, timedelta

from . import logic
from .db import DB, SLOTS

MAX_ONSITE_H = 12
MAX_DAYS = 400
SCHED_KEYS = ("外出", "驻点", "在岗", "空闲", "休假", "培训", "休息")
# 上午下午状态不同时，整天算哪个（靠前的优先）
DAY_PRIORITY = ("外出", "驻点", "培训", "休假", "休息", "在岗", "空闲")


def _hours(a: str, b: str) -> float | None:
    ta, tb = logic._dt(a), logic._dt(b)
    return (tb - ta).total_seconds() / 3600 if ta and tb else None


def _range(day_from: str, day_to: str) -> list[str]:
    s, e = date.fromisoformat(day_from), date.fromisoformat(day_to)
    if e < s:
        s, e = e, s
    n = min((e - s).days + 1, MAX_DAYS)
    return [(s + timedelta(days=i)).isoformat() for i in range(n)]


def _key(engs: list[dict], name: str) -> tuple[str, dict | None]:
    """工单上的工程师 → 统计分组：人员表里的人按 id，表外的按 D365 里的名字。"""
    e = logic.match_engineer(engs, name)
    return (f"e{e['id']}", e) if e else (f"x:{logic.clean_person(name)}", None)


def _orders(db: DB, days: list[str]) -> list[dict]:
    """上门或完工落在这段时间里的工单，附上排班口径的上门时间、超期、现场工时和配件。"""
    a, b = days[0], days[-1]
    rows = db.q("SELECT * FROM orders WHERE (substr(arrival_time,1,10) BETWEEN ?1 AND ?2) "
                "OR (substr(setoff_time,1,10) BETWEEN ?1 AND ?2) OR (planned_day BETWEEN ?1 AND ?2) "
                "OR (substr(complete_time,1,10) BETWEEN ?1 AND ?2)", (a, b))
    s = db.settings()
    parts = {}
    ids = [o["d365_id"] for o in rows if o["d365_id"]]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for p in db.q(f"SELECT order_d365_id, consumption_qty, is_charge, total, total_text FROM parts "
                      f"WHERE order_d365_id IN ({','.join('?' * len(chunk))})", chunk):
            parts.setdefault(p["order_d365_id"], []).append(p)
    out = []
    for o in rows:
        o.pop("d365_json", None)
        logic.enrich(o, s)
        v = o["visit"]
        o["visit_in"] = bool(v and a <= v["day"] <= b)
        o["done_in"] = a <= (o.get("complete_time") or "")[:10] <= b
        if not (o["visit_in"] or o["done_in"]) or not logic.clean_person(o["engineer"]):
            continue
        h = _hours(o["arrival_time"], o["complete_time"])
        o["onsite_h"] = round(h, 2) if h is not None and h >= 0 else None
        o["onsite_long"] = bool(o["onsite_h"] and o["onsite_h"] > MAX_ONSITE_H)
        r = _hours(o["dispatch_time"], o["arrival_time"])
        o["response_h"] = round(r, 1) if r is not None and r >= 0 else None
        o["late"] = o["risk"]["level"] in ("late", "overdue")
        ps = parts.get(o["d365_id"], [])
        o["parts_used"] = sum(p["consumption_qty"] or 0 for p in ps)
        o["charge"] = round(sum(p["total"] or 0 for p in ps if p["is_charge"]), 2)
        o["currency"] = next((p["total_text"][0] for p in ps
                              if p["total_text"] and not p["total_text"][0].isdigit()), "")
        out.append(o)
    out.sort(key=lambda o: ((o["visit"] or {}).get("day") or o["complete_time"][:10],
                            (o["visit"] or {}).get("time") or "", o["id"]))
    return out


def _schedule(db: DB, days: list[str]) -> dict:
    """(engineer_id, day, slot) → 半天状态。"""
    a, b = days[0], days[-1]
    raw = {(r["engineer_id"], r["day"], r["slot"]): r["status"]
           for r in db.q("SELECT * FROM schedule WHERE day BETWEEN ? AND ?", (a, b))}
    cnt = logic._dispatch_counts(db, a, b)
    return {"raw": raw, "cnt": cnt}


def _cell(sched: dict, e: dict, day: str, slot: str) -> tuple[str, int]:
    k = (e["id"], day, slot)
    n = sched["cnt"].get(k, 0)
    raw = sched["raw"].get(k, "")
    return ("外出" if n else logic.base_status(raw, e["type"])), n


def _day(sched: dict, e: dict, day: str) -> tuple[str, int]:
    """整天的排班状态和派单数。"""
    cells = [_cell(sched, e, day, sl) for sl in SLOTS]
    sts = [c[0] for c in cells]
    st = next((x for x in DAY_PRIORITY if x in sts), sts[0])
    return st, sum(c[1] for c in cells)


def _workdays(days: list[str]) -> list[str]:
    return [d for d in days if date.fromisoformat(d).weekday() < 5]


def _blank(name: str, typ: str, key: str, active: bool) -> dict:
    return {"key": key, "name": name, "type": typ, "active": active, "in_table": key.startswith("e"),
            "visits": 0, "visit_days": 0, "completed": 0, "onsite_h": 0.0, "onsite_n": 0, "onsite_long": 0,
            "response_sum": 0.0, "response_n": 0, "late": 0, "parts_used": 0.0, "charge": 0.0, "weekend_visits": 0,
            "sched": {k: 0 for k in SCHED_KEYS}, "_days": set()}


def summary(db: DB, day_from: str, day_to: str) -> dict:
    days = _range(day_from, day_to)
    engs = db.engineers(include_inactive=True)
    orders = _orders(db, days)
    sched = _schedule(db, days)
    groups: dict[str, dict] = {}
    for e in engs:
        g = _blank(e["name"], e["type"], f"e{e['id']}", bool(e["active"]))
        for d in _workdays(days):
            st, _ = _day(sched, e, d)
            if st in g["sched"]:
                g["sched"][st] += 1
        groups[g["key"]] = g
    currency = ""
    for o in orders:
        key, e = _key(engs, o["engineer"])
        g = groups.get(key) or groups.setdefault(key, _blank(logic.clean_person(o["engineer"]), "", key, True))
        if o["visit_in"]:
            g["visits"] += 1
            g["_days"].add(o["visit"]["day"])
            g["weekend_visits"] += date.fromisoformat(o["visit"]["day"]).weekday() >= 5
            if o["onsite_h"] is not None:
                if o["onsite_long"]:
                    g["onsite_long"] += 1
                else:
                    g["onsite_h"] += o["onsite_h"]
                    g["onsite_n"] += 1
            if o["response_h"] is not None:
                g["response_sum"] += o["response_h"]
                g["response_n"] += 1
            g["late"] += o["late"]
            g["parts_used"] += o["parts_used"]
            g["charge"] += o["charge"]
            currency = currency or o["currency"]
        if o["done_in"]:
            g["completed"] += 1
    rows = []
    for g in groups.values():
        busy = g["visits"] or g["completed"] or any(g["sched"][k] for k in ("外出", "休假", "培训"))
        if not g["active"] and not busy:
            continue  # 已停用、这段时间也没活的不显示
        g["visit_days"] = len(g.pop("_days"))
        g["onsite_h"] = round(g["onsite_h"], 1)
        g["onsite_avg"] = round(g["onsite_h"] / g["onsite_n"], 1) if g["onsite_n"] else None
        g["response_avg"] = round(g.pop("response_sum") / g["response_n"], 1) if g["response_n"] else None
        g.pop("response_n")
        g["charge"] = round(g["charge"], 2)
        rows.append(g)
    order = {f"e{e['id']}": i for i, e in enumerate(engs)}
    rows.sort(key=lambda g: (not g["in_table"], order.get(g["key"], 0), -g["visits"], g["name"]))
    tot = {k: sum(r[k] for r in rows) for k in ("visits", "completed", "onsite_h", "onsite_long", "late",
                                                 "parts_used", "charge", "weekend_visits")}
    tot["onsite_h"], tot["charge"] = round(tot["onsite_h"], 1), round(tot["charge"], 2)
    return {"from": days[0], "to": days[-1], "days": len(days), "workdays": len(_workdays(days)),
            "truncated": len(days) == MAX_DAYS,
            "rows": rows, "total": tot, "currency": currency, "max_onsite_h": MAX_ONSITE_H}


def detail(db: DB, key: str, day_from: str, day_to: str) -> dict:
    """一位工程师在这段时间的工单明细和每个工作日的排班。key 同 summary 里的 key。"""
    days = _range(day_from, day_to)
    engs = db.engineers(include_inactive=True)
    orders = [o for o in _orders(db, days) if _key(engs, o["engineer"])[0] == key]
    e = next((x for x in engs if f"e{x['id']}" == key), None)
    cal = []
    if e:
        sched = _schedule(db, days)
        for d in _workdays(days):
            st, n = _day(sched, e, d)
            cal.append({"day": d, "weekday": date.fromisoformat(d).weekday(), "status": st, "n": n})
    return {"key": key, "name": e["name"] if e else key[2:], "orders": orders, "calendar": cal,
            "max_onsite_h": MAX_ONSITE_H}


def export_rows(db: DB, day_from: str, day_to: str) -> dict:
    """导出 Excel 用：汇总、工单明细、排班明细。"""
    sm = summary(db, day_from, day_to)
    engs = db.engineers(include_inactive=True)
    days = _range(day_from, day_to)
    orders = _orders(db, days)
    for o in orders:
        key, e = _key(engs, o["engineer"])
        o["local"] = e["name"] if e else logic.clean_person(o["engineer"])
    sched = _schedule(db, days)
    cal = []
    for e in engs:
        if not any(r["key"] == f"e{e['id']}" for r in sm["rows"]):
            continue
        for d in _workdays(days):
            st, n = _day(sched, e, d)
            cal.append([d, "一二三四五"[date.fromisoformat(d).weekday()], e["name"], st, n or None])
    return {"summary": sm, "orders": orders, "calendar": cal}
