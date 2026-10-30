"""配件：消耗、是否收费、借料归还（数据只读，来自 D365 的服务配件和领料/借料/归还单）。

借料单（向子公司借料）上的配件，没用完的「剩余数量」要走归还单退回仓库。判断：
  已归还        借料单的归还状态 = 已归还
  已用完        借出的配件剩余数量都是 0，不用归还
  归还审批中    已提交归还申请，等 BPP 审批
  审批通过待退  归还申请已通过，配件还没退回
  归还被拒      归还申请被拒绝，需要处理
  待归还        工单已经完工/取消，还有剩余配件，但没申请归还  ← 需要提醒
  借用中        工单还没完工，配件可能还会用到
借料单本身还没批下来时：借料待提交（已创建 / 被拒）· 借料审批中（已提交）· 已取消
"""
from __future__ import annotations

from datetime import datetime

from .d365 import MAT_BORROW
from .db import DB

TODO = ("待归还", "归还被拒")  # 需要人去处理的
ORDER_DONE = ("已完成", "已取消")


def _days(created: str) -> int | None:
    try:
        return (datetime.now() - datetime.strptime(created[:16], "%Y-%m-%d %H:%M")).days
    except ValueError:
        return None


def borrow_state(mat: dict, remaining: float, order_status: str) -> str:
    st = mat.get("status_code")
    if st == 60:
        return "已取消"
    if st in (10, 40):
        return "借料待提交"
    if st == 20:
        return "借料审批中"
    rc = mat.get("return_code")
    if rc == 50:
        return "已归还"
    if remaining <= 0:
        return "已用完"
    if rc == 20:
        return "归还审批中"
    if rc == 30:
        return "审批通过待退"
    if rc == 40:
        return "归还被拒"
    return "待归还" if order_status in ORDER_DONE else "借用中"


def borrows(db: DB, order_d365_id: str | None = None) -> list[dict]:
    """所有借料单及其配件、剩余数量和归还状态。"""
    sql = ("SELECT m.*, o.id AS order_id, o.service_order_no, o.udesk_no, o.customer, o.engineer, o.serial, o.status AS order_status, "
           "o.d365_status FROM material_orders m LEFT JOIN orders o ON o.d365_id = m.order_d365_id WHERE m.type_code = ?")
    params: list = [MAT_BORROW]
    if order_d365_id:
        sql += " AND m.order_d365_id = ?"
        params.append(order_d365_id)
    out = []
    for m in db.q(sql + " ORDER BY m.created", params):
        items = db.q("SELECT part_no, name, quantity, confirm_qty, remaining_qty, is_charge FROM parts "
                     "WHERE material_id = ? ORDER BY part_no", (m["id"],))
        remaining = sum(p["remaining_qty"] or 0 for p in items)
        returns = db.q("SELECT code, status, return_status, created FROM material_orders WHERE borrow_id = ? "
                       "ORDER BY created", (m["id"],))
        out.append({**m, "parts": items, "remaining": remaining, "returns": returns,
                    "state": borrow_state(m, remaining, m["order_status"] or ""), "days": _days(m["created"])})
    return out


def order_parts(db: DB, order_d365_id: str) -> dict:
    parts = db.q("SELECT * FROM parts WHERE order_d365_id = ? ORDER BY created, part_no", (order_d365_id,))
    mats = db.q("SELECT * FROM material_orders WHERE order_d365_id = ? ORDER BY created", (order_d365_id,))
    charged = [p for p in parts if p["is_charge"]]
    return {
        "parts": parts, "materials": mats, "borrows": borrows(db, order_d365_id),
        "writeoffs": db.q("SELECT * FROM writeoff_docs WHERE order_d365_id = ? ORDER BY created DESC", (order_d365_id,)),
        "progress": db.q("SELECT * FROM order_progress WHERE order_d365_id = ? ORDER BY time DESC", (order_d365_id,)),
        "charge_total": round(sum(p["total"] or 0 for p in charged), 2),
        "currency": next((p["total_text"][0] for p in parts if p["total_text"] and not p["total_text"][0].isdigit()), ""),
    }


def to_return_by_order(db: DB) -> dict[str, int]:
    """每张工单有几张需要处理的借料单（待归还 / 归还被拒）。"""
    out: dict[str, int] = {}
    for b in borrows(db):
        if b["state"] in TODO:
            out[b["order_d365_id"]] = out.get(b["order_d365_id"], 0) + 1
    return out


def charges(db: DB, day_from: str = "", day_to: str = "", engineer: str = "", only_charge: bool = True) -> dict:
    """配件明细（默认只看收费的），按工单创建日期筛选。"""
    sql = ("SELECT p.*, o.id AS order_id, o.day, o.service_order_no, o.udesk_no, o.customer, o.engineer, "
           "o.status AS order_status, o.d365_status FROM parts p LEFT JOIN orders o ON o.d365_id = p.order_d365_id "
           "WHERE 1=1")
    params: list = []
    if only_charge:
        sql += " AND p.is_charge = 1"
    if day_from:
        sql += " AND o.day >= ?"
        params.append(day_from)
    if day_to:
        sql += " AND o.day <= ?"
        params.append(day_to)
    if engineer:
        sql += " AND o.engineer LIKE ?"
        params.append(f"%{engineer}%")
    rows = db.q(sql + " ORDER BY o.day DESC, o.service_order_no, p.part_no", params)
    cur = next((r["total_text"][0] for r in rows if r["total_text"] and not r["total_text"][0].isdigit()), "")
    return {"rows": rows, "total": round(sum(r["total"] or 0 for r in rows if r["is_charge"]), 2), "currency": cur,
            "orders": len({r["order_d365_id"] for r in rows})}
