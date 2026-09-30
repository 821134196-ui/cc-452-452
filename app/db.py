"""数据库连接与建表。

使用 SQLite 保存主数据、周转工单、挂接关系、事件与异常。
关键约束：
  * 一个挂车同一时刻至多存在一组有效（未解除）挂接 —— 部分唯一索引；
  * 一个车位同一时刻至多停放一辆挂车 —— 部分唯一索引；
  * 分位竞争依赖 stalls 的条件 UPDATE（compare-and-set），仅首个成功。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode = WAL;

-- 园区角色：guard 门岗 / dispatcher 调度员 / key_clerk 钥匙员 / inspector 查验员 / supervisor 值班主管
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN
                  ('guard','dispatcher','key_clerk','inspector','supervisor')),
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS tractors (
    id          INTEGER PRIMARY KEY,
    plate       TEXT NOT NULL UNIQUE,          -- 牵引车车牌（虚构）
    vin         TEXT NOT NULL UNIQUE,
    fleet       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trailers (
    id          INTEGER PRIMARY KEY,
    plate       TEXT NOT NULL UNIQUE,          -- 挂车车牌（虚构）
    number      TEXT NOT NULL UNIQUE,          -- 挂车自编号
    fleet       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drivers (
    id          INTEGER PRIMARY KEY,
    license_no  TEXT NOT NULL UNIQUE,          -- 驾驶证号（虚构）
    name        TEXT NOT NULL,
    phone       TEXT NOT NULL
);

-- 停车位：真实编号按行列排布，如 A-01
CREATE TABLE IF NOT EXISTS stalls (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    zone        TEXT NOT NULL,
    grid_row    INTEGER NOT NULL,
    grid_col    INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'free'   -- free / occupied / locked
);

-- 到离园预约
CREATE TABLE IF NOT EXISTS appointments (
    id              INTEGER PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,      -- 预约号
    tractor_plate   TEXT NOT NULL,
    trailer_plate   TEXT NOT NULL,
    driver_license  TEXT NOT NULL,
    purpose         TEXT NOT NULL,             -- drop 甩挂 / pick 提挂
    planned_arrival TEXT NOT NULL,             -- ISO 时间串（虚构时刻）
    actual_arrival  TEXT,
    status          TEXT NOT NULL DEFAULT 'booked', -- booked/arrived/late/done/cancelled
    linked_cycle    INTEGER                    -- 到达后产生的周转工单
);

-- 一次园区周转（签到 → 甩挂 → 提挂 → 出园）
CREATE TABLE IF NOT EXISTS cycles (
    id               INTEGER PRIMARY KEY,
    code             TEXT NOT NULL UNIQUE,
    tractor_plate    TEXT NOT NULL,
    trailer_plate    TEXT NOT NULL,
    driver_license   TEXT NOT NULL,
    appointment_code TEXT,
    purpose          TEXT NOT NULL DEFAULT 'drop',
    pickup_tractor_plate TEXT,              -- 提挂牵引车（可能不同于到园车）
    pickup_driver_license TEXT,             -- 提挂司机（替换时记录异常）
    phase            TEXT NOT NULL DEFAULT 'queued',
      -- queued 待卸挂 / stall_assigned 已分位 / unhitched 已解绑 /
      -- parked 停妥 / chocked 轮挡 / keys_cabinet 钥匙入柜 交接完成 /
      -- pickup_pending 待提挂 / pickup_verified 提挂核对通过 /
      -- hitched 已挂接 / exit_pending 待出园 / closed 已关闭
    stall_id         INTEGER REFERENCES stalls(id),
    seal_code        TEXT,                     -- 登记铅封号
    key_token        TEXT,                     -- 钥匙柜格口/牌证号
    key_cell         TEXT,
    arrived_at       TEXT NOT NULL,
    queued_at        TEXT,
    assigned_at      TEXT,
    unhitched_at     TEXT,
    parked_at        TEXT,
    chocked_at       TEXT,
    keys_at          TEXT,
    pickup_wait_from TEXT,                     -- 等待计时起点（进入待提挂）
    hitched_at       TEXT,
    exit_check_at    TEXT,
    closed_at        TEXT,
    wait_minutes     INTEGER,                  -- 排队等待分钟
    pickup_wait_minutes INTEGER,               -- 提挂等待分钟
    exit_approved    INTEGER NOT NULL DEFAULT 0,
    abnormal_exit    INTEGER NOT NULL DEFAULT 0,
    exit_note        TEXT,
    driver_confirmed INTEGER NOT NULL DEFAULT 0,
    seal_confirmed   INTEGER NOT NULL DEFAULT 0,
    appearance_confirmed INTEGER NOT NULL DEFAULT 0
);

-- 当前有效挂接关系（牵引车 ↔ 挂车）。解除后 deleted=1。
CREATE TABLE IF NOT EXISTS couplings (
    id           INTEGER PRIMARY KEY,
    tractor_plate TEXT NOT NULL,
    trailer_plate TEXT NOT NULL,
    cycle_id     INTEGER REFERENCES cycles(id),
    coupled_at   TEXT NOT NULL,
    uncoupled_at TEXT,
    by_user      TEXT NOT NULL,
    deleted      INTEGER NOT NULL DEFAULT 0
);
-- 一个挂车同一时刻只能有一组有效挂接
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_coupling_per_trailer
    ON couplings(trailer_plate) WHERE deleted = 0;
-- 一个牵引车同一时刻也只允许挂一辆（运营规则）
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_coupling_per_tractor
    ON couplings(tractor_plate) WHERE deleted = 0;

-- 每个挂车的当前车位占用（停妥期间存在记录）
CREATE TABLE IF NOT EXISTS stall_occupancy (
    id           INTEGER PRIMARY KEY,
    stall_id     INTEGER NOT NULL REFERENCES stalls(id),
    trailer_plate TEXT NOT NULL,
    cycle_id     INTEGER NOT NULL REFERENCES cycles(id),
    since        TEXT NOT NULL,
    released     INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_stall_active
    ON stall_occupancy(stall_id) WHERE released = 0;

-- 钥匙柜台账：每条钥匙生命周期（入柜/借出/归还/取走）
CREATE TABLE IF NOT EXISTS key_ledger (
    id           INTEGER PRIMARY KEY,
    token        TEXT NOT NULL,               -- 钥匙牌号（可扫描）
    cell         TEXT NOT NULL,               -- 柜格
    trailer_plate TEXT,
    tractor_plate TEXT,
    status       TEXT NOT NULL,               -- in_cabinet / lent_out / released / misplaced
    logged_at    TEXT NOT NULL,
    by_user      TEXT NOT NULL,
    note         TEXT
);

-- 外观检查（入位/提挂各一次，照片方位 front/rear/left/right/roof）
CREATE TABLE IF NOT EXISTS appearance_checks (
    id           INTEGER PRIMARY KEY,
    cycle_id     INTEGER NOT NULL REFERENCES cycles(id),
    trailer_plate TEXT NOT NULL,
    stage        TEXT NOT NULL,               -- intake 入位 / pickup 提挂
    side         TEXT NOT NULL,
    photo_file   TEXT NOT NULL,
    damage       INTEGER NOT NULL DEFAULT 0,
    note         TEXT,
    checked_by   TEXT NOT NULL,
    checked_at   TEXT NOT NULL
);

-- 铅封登记与核对
CREATE TABLE IF NOT EXISTS seal_records (
    id           INTEGER PRIMARY KEY,
    cycle_id     INTEGER NOT NULL REFERENCES cycles(id),
    trailer_plate TEXT NOT NULL,
    stage        TEXT NOT NULL,               -- intake / pickup
    seal_code    TEXT NOT NULL,
    matched      INTEGER,                     -- 提挂时与入位是否相符
    checked_by   TEXT NOT NULL,
    checked_at   TEXT NOT NULL
);

-- 事件流（看板/审计）
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
    cycle_code  TEXT,
    kind        TEXT NOT NULL,
    actor       TEXT,
    detail      TEXT
);

-- 异常单：迟到/车位占用/钥匙错格/铅封不符/挂车损伤/司机替换/无法提挂/异常放行
CREATE TABLE IF NOT EXISTS exceptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    cycle_code  TEXT,
    ref         TEXT,
    description TEXT NOT NULL,
    raised_by   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',  -- open / approved / rejected / closed
    decided_by  TEXT,
    decided_at  TEXT,
    decision_note TEXT
);

-- 等待超时监控（进程内任务扫描）
CREATE TABLE IF NOT EXISTS wait_tracking (
    id          INTEGER PRIMARY KEY,
    cycle_id    INTEGER NOT NULL REFERENCES cycles(id),
    kind        TEXT NOT NULL,                 -- queue 卸挂排队 / pickup 提挂等待
    started_at  TEXT NOT NULL,
    threshold_s INTEGER NOT NULL,
    alerted     INTEGER NOT NULL DEFAULT 0,
    done        INTEGER NOT NULL DEFAULT 0
);
"""


_lock = threading.local()


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 4000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
