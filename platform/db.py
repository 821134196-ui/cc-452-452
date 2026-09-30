"""SQLite 连接、建表与初始数据访问层。

核心约束:
* 一个挂车同一时刻只能存在一组有效挂接关系 -> hitches 部分唯一索引。
* 车位抢占依靠单条条件 UPDATE 的原子性，仅首个成功。
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Optional

from .clock import Clock, iso

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- 人员与角色 -------------------------------------------------
CREATE TABLE IF NOT EXISTS staff (
    id        INTEGER PRIMARY KEY,
    login     TEXT UNIQUE NOT NULL,
    name      TEXT NOT NULL,
    role      TEXT NOT NULL CHECK(role IN
              ('gate','dispatcher','keyclerk','inspector','supervisor'))
);

-- 司机（虚构资料）--------------------------------------------
CREATE TABLE IF NOT EXISTS drivers (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    license_no TEXT UNIQUE NOT NULL,
    phone      TEXT NOT NULL,
    employer   TEXT NOT NULL
);

-- 牵引车 ------------------------------------------------------
CREATE TABLE IF NOT EXISTS tractors (
    id       INTEGER PRIMARY KEY,
    plate_no TEXT UNIQUE NOT NULL,
    model    TEXT NOT NULL,
    status   TEXT NOT NULL DEFAULT 'idle'
             CHECK(status IN ('idle','inbound','dropped','hitched','outbound','held'))
);

-- 挂车 --------------------------------------------------------
CREATE TABLE IF NOT EXISTS trailers (
    id       INTEGER PRIMARY KEY,
    plate_no TEXT UNIQUE NOT NULL,
    spec     TEXT NOT NULL,
    seal_no  TEXT,                      -- 当前铅封号
    status   TEXT NOT NULL DEFAULT 'enroute'
             CHECK(status IN ('enroute','queued','slotted','dropped','hitched','outbound','held'))
);

-- 停车位（按真实编号排成平面行列）-----------------------------
CREATE TABLE IF NOT EXISTS parking_slots (
    id      INTEGER PRIMARY KEY,
    code    TEXT UNIQUE NOT NULL,       -- 如 A-03
    zone    TEXT NOT NULL,              -- A/B/C 区
    row_no  INTEGER NOT NULL,
    col_no  INTEGER NOT NULL,
    kind    TEXT NOT NULL DEFAULT 'standard',
    status  TEXT NOT NULL DEFAULT 'free' CHECK(status IN ('free','occupied','locked')),
    turn_id INTEGER REFERENCES turns(id)
);

-- 到离园预约 ---------------------------------------------------
CREATE TABLE IF NOT EXISTS appointments (
    id               INTEGER PRIMARY KEY,
    code             TEXT UNIQUE NOT NULL,
    tractor_id       INTEGER NOT NULL REFERENCES tractors(id),
    trailer_id       INTEGER NOT NULL REFERENCES trailers(id),
    driver_id        INTEGER NOT NULL REFERENCES drivers(id),
    planned_arrival  TEXT NOT NULL,
    planned_departure TEXT,
    kind             TEXT NOT NULL CHECK(kind IN ('drop','pickup','both')),
    status           TEXT NOT NULL DEFAULT 'scheduled'
                     CHECK(status IN ('scheduled','arrived','late','noshow','done','cancelled')),
    note             TEXT
);

-- 周转单（一次到园到出园的完整生命周期）------------------------
CREATE TABLE IF NOT EXISTS turns (
    id                 INTEGER PRIMARY KEY,
    code               TEXT UNIQUE NOT NULL,
    appointment_id     INTEGER REFERENCES appointments(id),
    trailer_id         INTEGER NOT NULL REFERENCES trailers(id),
    in_tractor_id      INTEGER REFERENCES tractors(id),
    in_driver_id       INTEGER REFERENCES drivers(id),
    out_tractor_id     INTEGER REFERENCES tractors(id),
    out_driver_id      INTEGER REFERENCES drivers(id),
    status             TEXT NOT NULL DEFAULT 'expected'
                       CHECK(status IN
                       ('expected','queued','slotted','handed_over',
                        'pickup_pending','pickup_started','ready_to_exit',
                        'exited','closed','held','abnormal_release')),
    planned_arrival    TEXT,
    arrived_at         TEXT,
    queued_at          TEXT,
    slot_id            INTEGER REFERENCES parking_slots(id),
    slotted_at         TEXT,
    unhitched_at       TEXT,
    unhitched_by       INTEGER REFERENCES staff(id),
    chocked_at         TEXT,
    chocked_by         INTEGER REFERENCES staff(id),
    key_deposited_at   TEXT,
    key_deposited_by   INTEGER REFERENCES staff(id),
    pickup_started_at  TEXT,
    pickup_started_by  INTEGER REFERENCES staff(id),
    hitched_at         TEXT,
    hitched_by         INTEGER REFERENCES staff(id),
    seal_verified_at   TEXT,
    seal_verified_by   INTEGER REFERENCES staff(id),
    appearance_in_at   TEXT,
    appearance_in_by   INTEGER REFERENCES staff(id),
    appearance_out_at  TEXT,
    appearance_out_by  INTEGER REFERENCES staff(id),
    key_released_at    TEXT,
    key_released_by    INTEGER REFERENCES staff(id),
    exited_at          TEXT,
    exited_by          INTEGER REFERENCES staff(id),
    closed_at          TEXT,
    abnormal           INTEGER NOT NULL DEFAULT 0,  -- 是否异常放行离场
    hold_reason        TEXT,
    wait_minutes       INTEGER NOT NULL DEFAULT 0   -- 队列等待时长（累计）
);

-- 挂接关系 -----------------------------------------------------
CREATE TABLE IF NOT EXISTS hitches (
    id         INTEGER PRIMARY KEY,
    turn_id    INTEGER NOT NULL REFERENCES turns(id),
    tractor_id INTEGER NOT NULL REFERENCES tractors(id),
    trailer_id INTEGER NOT NULL REFERENCES trailers(id),
    active     INTEGER NOT NULL DEFAULT 1,
    linked_at  TEXT NOT NULL,
    unlinked_at TEXT,
    created_by INTEGER REFERENCES staff(id)
);
-- 关键：一个挂车同时只能有一组有效挂接
CREATE UNIQUE INDEX IF NOT EXISTS idx_hitch_one_active_per_trailer
    ON hitches(trailer_id) WHERE active = 1;

-- 钥匙台账 -----------------------------------------------------
CREATE TABLE IF NOT EXISTS keys (
    id           INTEGER PRIMARY KEY,
    tag          TEXT UNIQUE NOT NULL,   -- 可扫描条码
    turn_id      INTEGER NOT NULL REFERENCES turns(id),
    trailer_id   INTEGER NOT NULL REFERENCES trailers(id),
    box_id       TEXT NOT NULL,          -- 钥匙柜
    cell_no      TEXT NOT NULL,          -- 格口号
    status       TEXT NOT NULL CHECK(status IN ('in_cabinet','checked_out','wrong_cell')),
    deposited_at TEXT,
    deposited_by INTEGER REFERENCES staff(id),
    released_at  TEXT,
    released_by  INTEGER REFERENCES staff(id),
    note         TEXT
);

-- 外观检查（到园 / 提挂两次）-----------------------------------
CREATE TABLE IF NOT EXISTS inspections (
    id          INTEGER PRIMARY KEY,
    turn_id     INTEGER NOT NULL REFERENCES turns(id),
    phase       TEXT NOT NULL CHECK(phase IN ('arrival','departure')),
    result      TEXT NOT NULL CHECK(result IN ('pass','damage')),
    damage_desc TEXT,
    photos      TEXT NOT NULL DEFAULT '[]',  -- JSON 数组：方位->文件
    inspector_id INTEGER REFERENCES staff(id),
    checked_at  TEXT NOT NULL
);

-- 异常放行审批 -------------------------------------------------
CREATE TABLE IF NOT EXISTS approvals (
    id           INTEGER PRIMARY KEY,
    turn_id      INTEGER NOT NULL REFERENCES turns(id),
    reason_code  TEXT NOT NULL,          -- seal_mismatch/damage/cannot_pickup/...
    detail       TEXT,
    requested_by INTEGER NOT NULL REFERENCES staff(id),
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK(status IN ('pending','approved','rejected')),
    decided_by   INTEGER REFERENCES staff(id),
    decided_at   TEXT,
    created_at   TEXT NOT NULL
);

-- 事件流水（迟到/占用/错格/铅封/损伤/替换/无法提挂等单独记录）-
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY,
    ts       TEXT NOT NULL,
    turn_id  INTEGER REFERENCES turns(id),
    type     TEXT NOT NULL,
    actor_id INTEGER REFERENCES staff(id),
    severity TEXT NOT NULL DEFAULT 'info'
             CHECK(severity IN ('info','warn','exception','critical')),
    detail   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_turn ON events(turn_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);

CREATE TABLE IF NOT EXISTS meta_kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""

ROLES = ("gate", "dispatcher", "keyclerk", "inspector", "supervisor")
ROLE_CN = {
    "gate": "门岗",
    "dispatcher": "调度员",
    "keyclerk": "钥匙员",
    "inspector": "查验员",
    "supervisor": "值班主管",
}


def connect(db_path: str, clock: Optional[Clock] = None) -> sqlite3.Connection:
    first = not os.path.exists(db_path)
    conn = sqlite3.connect(db_path, timeout=8, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=8000")
    conn.executescript(SCHEMA)
    if first:
        conn.execute("INSERT OR IGNORE INTO meta_kv(k,v) VALUES('created','1')")
    return conn


# ---- 小工具 ---------------------------------------------------

def row(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> Optional[sqlite3.Row]:
    return conn.execute(sql, params).fetchone()


def allrows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def insert_event(conn: sqlite3.Connection, clock: Clock, etype: str,
                 turn_id: Optional[int] = None, actor_id: Optional[int] = None,
                 severity: str = "info", detail: Optional[dict[str, Any]] = None) -> int:
    cur = conn.execute(
        "INSERT INTO events(ts,turn_id,type,actor_id,severity,detail) VALUES(?,?,?,?,?,?)",
        (iso(clock.now()), turn_id, etype, actor_id, severity,
         json.dumps(detail or {}, ensure_ascii=False)))
    return cur.lastrowid


def dump(r: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
    return dict(r) if r is not None else None
