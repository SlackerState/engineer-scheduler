"""SQLite 存储：人员、排班、派单记录、订单清单、设置。"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from datetime import date, timedelta
from pathlib import Path

STATUSES = ["空闲", "外出", "驻点", "休假", "培训", "休息"]
ORDER_STATUSES = ["待处理", "处理中", "已完成", "已取消"]
SLOTS = ["上午", "下午"]


def app_dir() -> Path:
    override = os.environ.get("ES_DATA_DIR")
    if override:
        d = Path(override)
    elif sys.platform == "win32":
        d = Path(os.environ.get("APPDATA", Path.home())) / "EngineerScheduler"
    elif sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / "EngineerScheduler"
    else:
        d = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "EngineerScheduler"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


DEFAULT_SETTINGS = {
    # D365（Udesk 创建的服务订单都会推送到 D365 的 mcs_serviceorder，来源 = Udesk）
    "d365_url": "https://sany.crm4.dynamics.com",
    "d365_appid": "686a745d-1b06-ef11-9f8b-6045bd8c985b",
    "d365_api_version": "v9.2",
    "order_sources": "60",          # mcs_ordersource：60 = Udesk，多个用逗号
    "network_filter": "UK Kent 01 S",  # 服务网点，留空 = 不过滤
    "lookback_days": 30,
    "udesk_url": "https://sany.s5.udesk.cn/entry/",
    "udesk_agent_id": "",           # Udesk 坐席 ID：在软件里建服务订单时标明是谁建的
    "udesk_mid_url": "https://sanyi-mid.s5.udesk.cn/sanyi",  # Udesk 建单插件背后的三一中间件
    "udesk_timezone": "Europe/London",
    "d365_status_overrides_local": True,
    "d365_to_dispatch": True,
    # 同步
    "auto_sync": False,
    "sync_interval_min": 15,
    "risk_hours": 24,  # 距预约（考核截止）不足这么多小时且未到达 = 即将超期
    "auto_check_update": True,
    "update_api_url": "",  # 留空 = GitHub Releases 最新版本
    "browser_channel": "auto",
    "headless": True,
    "noon_hour": 12,
    # 排班表显示
    "schedule_start": "",
    "schedule_days": 14,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS engineers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seq INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL DEFAULT '外勤',
    phone TEXT DEFAULT '',
    remark TEXT DEFAULT '',
    aliases TEXT DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1  -- 0 = 已停用（离职等）：不再出现在排班/人员里，但保留历史排班和工作量
);
CREATE TABLE IF NOT EXISTS schedule (
    engineer_id INTEGER NOT NULL REFERENCES engineers(id) ON DELETE CASCADE,
    day TEXT NOT NULL,
    slot TEXT NOT NULL,
    status TEXT NOT NULL,
    auto INTEGER NOT NULL DEFAULT 0,  -- 1 = 派单时自动标的「外出」，派单撤销时可自动清掉
    PRIMARY KEY (engineer_id, day, slot)
);
CREATE TABLE IF NOT EXISTS dispatches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    engineer_id INTEGER NOT NULL REFERENCES engineers(id) ON DELETE CASCADE,
    slot TEXT NOT NULL,
    customer TEXT DEFAULT '',
    address TEXT DEFAULT '',
    order_no TEXT DEFAULT '',
    hours REAL,
    source TEXT DEFAULT 'manual',
    booking_id TEXT,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS ix_dispatch_day ON dispatches(day, slot);
CREATE UNIQUE INDEX IF NOT EXISTS ux_dispatch_booking ON dispatches(booking_id, day, slot) WHERE booking_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT DEFAULT '',
    service_order_no TEXT DEFAULT '',
    loan_no TEXT DEFAULT '',
    customer TEXT DEFAULT '',
    serial TEXT DEFAULT '',
    problem TEXT DEFAULT '',
    engineer TEXT DEFAULT '',
    status TEXT DEFAULT '待处理',
    progress TEXT DEFAULT '',
    udesk_no TEXT DEFAULT '',
    order_type TEXT DEFAULT '',
    d365_id TEXT UNIQUE,
    d365_status TEXT DEFAULT '',
    d365_status_code INTEGER,
    appointment TEXT DEFAULT '',
    dispatch_time TEXT DEFAULT '',
    setoff_time TEXT DEFAULT '',
    arrival_time TEXT DEFAULT '',
    complete_time TEXT DEFAULT '',
    address TEXT DEFAULT '',
    contact TEXT DEFAULT '',
    network TEXT DEFAULT '',
    model TEXT DEFAULT '',
    warranty TEXT DEFAULT '',
    d365_json TEXT DEFAULT '',
    synced_at TEXT DEFAULT '',
    planned_day TEXT DEFAULT '',   -- 调度员定的实际计划上门日期（只存本地）
    planned_slot TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
);
-- D365 服务配件（mcs_servicepart），只读同步
CREATE TABLE IF NOT EXISTS parts (
    id TEXT PRIMARY KEY,
    order_d365_id TEXT NOT NULL,
    part_no TEXT DEFAULT '',
    name TEXT DEFAULT '',
    quantity REAL DEFAULT 0,
    confirm_qty REAL DEFAULT 0,
    remaining_qty REAL DEFAULT 0,
    consumption_qty REAL DEFAULT 0,
    writeoff_doc TEXT DEFAULT '',   -- 已在配件核销单上（非空 = 已核销，不能再改消耗）
    return_order TEXT DEFAULT '',   -- 已在归还单上
    is_charge INTEGER DEFAULT 0,
    price REAL DEFAULT 0,
    discount REAL DEFAULT 0,
    total REAL DEFAULT 0,
    total_text TEXT DEFAULT '',
    material_id TEXT DEFAULT '',
    material_code TEXT DEFAULT '',
    created TEXT DEFAULT '',
    modified TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_parts_order ON parts(order_d365_id);
-- D365 领料/借料/归还单（mcs_repairmaterialorder），只读同步
CREATE TABLE IF NOT EXISTS material_orders (
    id TEXT PRIMARY KEY,
    order_d365_id TEXT NOT NULL,
    code TEXT DEFAULT '',
    type_code INTEGER,
    type TEXT DEFAULT '',
    status_code INTEGER,
    status TEXT DEFAULT '',
    return_code INTEGER,
    return_status TEXT DEFAULT '',
    shipment TEXT DEFAULT '',
    borrow_id TEXT DEFAULT '',
    borrow_code TEXT DEFAULT '',
    reason TEXT DEFAULT '',
    created TEXT DEFAULT '',
    modified TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_mat_order ON material_orders(order_d365_id);
-- D365 服务订单处理进度（mcs_serviceorderprocessingprogress，工程师在 D365 / App 里填的进展），只读同步
CREATE TABLE IF NOT EXISTS order_progress (
    id TEXT PRIMARY KEY,
    order_d365_id TEXT NOT NULL,
    code TEXT DEFAULT '',      -- SPP-xxxxxxxx
    text TEXT DEFAULT '',
    time TEXT DEFAULT '',      -- 处理时间（本地时区）
    owner TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_progress_order ON order_progress(order_d365_id);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

ORDER_EDITABLE = [
    "day", "service_order_no", "loan_no", "customer", "serial",
    "problem", "engineer", "status", "progress",
]


class DB:
    def __init__(self, path: Path | None = None):
        self.path = path or app_dir() / "data.db"
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._migrate()

    def _migrate(self):
        """给旧版本建的数据库补新列。"""
        with self.lock:
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(orders)")}
            for name in ("planned_day", "planned_slot"):
                if name not in cols:
                    self.conn.execute(f"ALTER TABLE orders ADD COLUMN {name} TEXT DEFAULT ''")
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(parts)")}
            for name, ddl in (("consumption_qty", "REAL DEFAULT 0"), ("writeoff_doc", "TEXT DEFAULT ''"),
                              ("return_order", "TEXT DEFAULT ''")):
                if name not in cols:
                    self.conn.execute(f"ALTER TABLE parts ADD COLUMN {name} {ddl}")
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(engineers)")}
            if "active" not in cols:
                self.conn.execute("ALTER TABLE engineers ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(schedule)")}
            if "auto" not in cols:
                self.conn.execute("ALTER TABLE schedule ADD COLUMN auto INTEGER NOT NULL DEFAULT 0")
                # 旧版本按派单自动标的「外出」：所在格子有 D365 派单的，视为自动标记
                self.conn.execute(
                    "UPDATE schedule SET auto=1 WHERE status='外出' AND EXISTS (SELECT 1 FROM dispatches d "
                    "WHERE d.engineer_id=schedule.engineer_id AND d.day=schedule.day AND d.slot=schedule.slot "
                    "AND d.source='d365')")
            self.conn.commit()

    # ---------- 通用 ----------
    def q(self, sql: str, params=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params=()) -> dict | None:
        rows = self.q(sql, params)
        return rows[0] if rows else None

    def x(self, sql: str, params=()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur.lastrowid

    # ---------- 设置 ----------
    def settings(self) -> dict:
        s = dict(DEFAULT_SETTINGS)
        for r in self.q("SELECT key, value FROM settings"):
            s[r["key"]] = json.loads(r["value"])
        if not s["schedule_start"]:
            s["schedule_start"] = _monday(date.today()).isoformat()
        return s

    def set_raw_setting(self, key: str, value):
        """写内部记号（不在设置页里显示的键）。"""
        with self.lock:
            self.conn.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                              "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))
            self.conn.commit()

    def save_settings(self, data: dict) -> dict:
        with self.lock:
            for k, v in data.items():
                if k in DEFAULT_SETTINGS:
                    self.conn.execute(
                        "INSERT INTO settings(key, value) VALUES(?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (k, json.dumps(v, ensure_ascii=False)),
                    )
            self.conn.commit()
        return self.settings()

    # ---------- 人员 ----------
    def engineers(self, include_inactive: bool = False) -> list[dict]:
        """include_inactive：连已停用的一起返回（匹配历史工单、回看排班用）。"""
        where = "" if include_inactive else "WHERE active=1 "
        return self.q(f"SELECT * FROM engineers {where}ORDER BY seq, id")

    def save_engineer(self, e: dict) -> int:
        fields = (e.get("name", "").strip(), e.get("type") or "外勤",
                  e.get("phone", ""), e.get("remark", ""), e.get("aliases", ""))
        if not fields[0]:
            raise ValueError("姓名不能为空")
        off = self.one("SELECT * FROM engineers WHERE name=? AND active=0", (fields[0],))
        if e.get("id"):
            if off and off["id"] != int(e["id"]):
                raise ValueError(f"「{fields[0]}」已停用，请在人员页下方「已停用」里点恢复")
            self.x("UPDATE engineers SET name=?, type=?, phone=?, remark=?, aliases=? WHERE id=?",
                   (*fields, e["id"]))
            return e["id"]
        if off:  # 同名的已停用人员：恢复启用，接上原来的历史；没填的字段沿用原来的（尤其是 D365 别名，丢了工单就对不上了）
            self.x("UPDATE engineers SET type=?, phone=?, remark=?, aliases=?, active=1 WHERE id=?",
                   (e.get("type") or off["type"], e.get("phone") or off["phone"], e.get("remark") or off["remark"],
                    e.get("aliases") or off["aliases"], off["id"]))
            return off["id"]
        seq = (self.one("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM engineers") or {"n": 1})["n"]
        return self.x("INSERT INTO engineers(seq, name, type, phone, remark, aliases) VALUES(?,?,?,?,?,?)",
                      (seq, *fields))

    def delete_engineer(self, eid: int) -> str:
        """有排班或派单历史的只停用（保留历史），没有历史的直接删除。返回 'deactivated' / 'deleted'。"""
        if self.one("SELECT 1 AS x FROM schedule WHERE engineer_id=? UNION ALL "
                    "SELECT 1 FROM dispatches WHERE engineer_id=? LIMIT 1", (eid, eid)):
            self.x("UPDATE engineers SET active=0 WHERE id=?", (eid,))
            return "deactivated"
        self.x("DELETE FROM engineers WHERE id=?", (eid,))
        return "deleted"

    def restore_engineer(self, eid: int):
        self.x("UPDATE engineers SET active=1 WHERE id=?", (eid,))

    def move_engineer(self, eid: int, delta: int):
        rows = self.engineers()
        idx = next((i for i, r in enumerate(rows) if r["id"] == eid), None)
        if idx is None:
            return
        j = idx + delta
        if 0 <= j < len(rows):
            rows[idx], rows[j] = rows[j], rows[idx]
        with self.lock:
            for i, r in enumerate(rows, 1):
                self.conn.execute("UPDATE engineers SET seq=? WHERE id=?", (i, r["id"]))
            self.conn.commit()

    # ---------- 排班 ----------
    def set_cell(self, eid: int, day: str, slot: str, status: str, auto: bool = False):
        if status:
            self.x("INSERT INTO schedule(engineer_id, day, slot, status, auto) VALUES(?,?,?,?,?) "
                   "ON CONFLICT(engineer_id, day, slot) DO UPDATE SET status = excluded.status, auto = excluded.auto",
                   (eid, day, slot, status, int(auto)))
        else:
            self.x("DELETE FROM schedule WHERE engineer_id=? AND day=? AND slot=?", (eid, day, slot))

    def cell(self, eid: int, day: str, slot: str) -> str:
        r = self.one("SELECT status FROM schedule WHERE engineer_id=? AND day=? AND slot=?", (eid, day, slot))
        return r["status"] if r else ""

    def cell_auto(self, eid: int, day: str, slot: str) -> bool:
        r = self.one("SELECT auto FROM schedule WHERE engineer_id=? AND day=? AND slot=?", (eid, day, slot))
        return bool(r and r["auto"])

    # ---------- 订单 ----------
    def update_order(self, oid: int, fields: dict):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE orders SET {cols}, updated_at=datetime('now','localtime') WHERE id=?",
               (*fields.values(), oid))
