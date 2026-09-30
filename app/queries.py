"""只读查询模型：聚合看板、队列、车位图、台账、外观等界面数据。"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from .settings import QUEUE_TIMEOUT_S, PICKUP_TIMEOUT_S

PHASE_CN = {
    "queued": "待卸挂排队",
    "stall_assigned": "已分位·待解绑",
    "unhitched": "已解绑·待查验",
    "parked": "停妥·待轮挡",
    "chocked": "轮挡完成·待钥匙",
    "keys_cabinet": "甩挂交接完成·待提挂",
    "pickup_pending": "待提挂核对",
    "pickup_verified": "提挂核对通过·待挂接",
    "hitched": "已挂接",
    "exit_pending": "待出园",
    "closed": "已关闭",
}

PHASE_ORDER = [
    "queued", "stall_assigned", "unhitched", "parked", "chocked",
    "keys_cabinet", "pickup_pending", "pickup_verified",
    "hitched", "exit_pending", "closed",
]

EXCEPTION_CN = {
    "late_arrival": "预约迟到",
    "stall_occupied": "车位占用",
    "key_wrong_cell": "钥匙错格",
    "seal_mismatch": "铅封不符",
    "trailer_damage": "挂车损伤",
    "driver_swap": "司机替换",
    "cannot_pick": "无法提挂",
    "abnormal_exit": "异常放行",
}


def _elapsed_min(started: str | None, now: datetime) -> int | None:
    if not started:
        return None
    t = datetime.strptime(started, "%Y-%m-%d %H:%M:%S")
    return max(0, int((now - t).total_seconds() // 60))


def _cycle_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT c.*, s.code stall_code, d.name driver_name, "
        "pd.name pickup_driver_name "
        "FROM cycles c LEFT JOIN stalls s ON s.id=c.stall_id "
        "LEFT JOIN drivers d ON d.license_no=c.driver_license "
        "LEFT JOIN drivers pd ON pd.license_no=c.pickup_driver_license "
        "ORDER BY c.id"
    ).fetchall()


def _cycle_dict(r: sqlite3.Row, now: datetime) -> dict:
    d = dict(r)
    d["phase_cn"] = PHASE_CN.get(r["phase"], r["phase"])
    d["queue_elapsed"] = _elapsed_min(r["queued_at"], now)
    d["pickup_elapsed"] = _elapsed_min(r["pickup_wait_from"], now)
    return d


def snapshot(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    cycles = [_cycle_dict(r, now) for r in _cycle_rows(conn)]
    open_cycles = [c for c in cycles if c["phase"] != "closed"]

    # 到园看板：阶段计数
    phase_counts = {p: 0 for p in PHASE_ORDER}
    for c in cycles:
        phase_counts[c["phase"]] = phase_counts.get(c["phase"], 0) + 1

    waiting = conn.execute(
        "SELECT wt.*, c.code FROM wait_tracking wt JOIN cycles c ON c.id=wt.cycle_id "
        "WHERE wt.done=0 ORDER BY wt.started_at"
    ).fetchall()
    wait_list = []
    for w in waiting:
        elapsed = int((now - datetime.strptime(w["started_at"], "%Y-%m-%d %H:%M:%S")).total_seconds())
        wait_list.append({
            "cycle": w["code"], "kind": w["kind"],
            "kind_cn": "待卸挂排队" if w["kind"] == "queue" else "提挂等待",
            "elapsed_s": elapsed, "threshold_s": w["threshold_s"],
            "over": elapsed >= w["threshold_s"],
            "alerted": bool(w["alerted"]),
        })

    # 甩挂队列
    queue = [c for c in cycles if c["phase"] == "queued"]

    # 车位图
    stall_rows = conn.execute(
        "SELECT s.*, o.trailer_plate, c.code cycle_code "
        "FROM stalls s LEFT JOIN stall_occupancy o ON o.stall_id=s.id AND o.released=0 "
        "LEFT JOIN cycles c ON c.id=o.cycle_id ORDER BY s.zone, s.grid_row, s.grid_col"
    ).fetchall()
    zones: dict[str, list] = {}
    for s in stall_rows:
        zones.setdefault(s["zone"], []).append(dict(s))

    # 有效挂接
    couplings = [dict(r) for r in conn.execute(
        "SELECT * FROM couplings WHERE deleted=0 ORDER BY id")]

    # 钥匙柜台账（密集清单）
    keys = [dict(r) for r in conn.execute(
        "SELECT * FROM key_ledger ORDER BY id DESC")]
    cabinet_now = {}
    for k in keys:
        if k["status"] in ("in_cabinet", "misplaced"):
            cabinet_now[k["token"]] = k

    # 外观：按工单聚合
    appearance = {}
    for r in conn.execute("SELECT * FROM appearance_checks ORDER BY cycle_id, stage, side"):
        d = dict(r)
        d["url"] = f"/api/photos/{d['photo_file']}"
        appearance.setdefault(r["cycle_id"], []).append(d)

    # 铅封记录
    seals = [dict(r) for r in conn.execute(
        "SELECT * FROM seal_records ORDER BY id")]

    # 异常单
    exceptions = [dict(r, kind_cn=EXCEPTION_CN.get(r["kind"], r["kind"]))
                  for r in conn.execute("SELECT * FROM exceptions ORDER BY id DESC")]

    # 事件流
    events = [dict(r) for r in conn.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT 60")]

    # 主数据
    users = [dict(r) for r in conn.execute("SELECT * FROM users WHERE active=1")]
    tractors = [r["plate"] for r in conn.execute("SELECT plate FROM tractors ORDER BY id")]
    trailers = [dict(r) for r in conn.execute(
        "SELECT plate,number FROM trailers ORDER BY id")]
    drivers = [dict(r) for r in conn.execute(
        "SELECT license_no,name FROM drivers ORDER BY id")]
    appointments = [dict(r) for r in conn.execute(
        "SELECT * FROM appointments ORDER BY id")]

    closed_today = phase_counts.get("closed", 0)
    return {
        "server_time": now.strftime("%Y-%m-%d %H:%M:%S"),
        "thresholds": {"queue": QUEUE_TIMEOUT_S, "pickup": PICKUP_TIMEOUT_S},
        "cycles": cycles,
        "open_cycles": open_cycles,
        "phase_counts": phase_counts,
        "phase_cn": PHASE_CN,
        "phase_order": PHASE_ORDER,
        "waiting": wait_list,
        "queue": queue,
        "stall_zones": zones,
        "couplings": couplings,
        "keys": keys,
        "cabinet_now": cabinet_now,
        "appearance": {str(k): v for k, v in appearance.items()},
        "seals": seals,
        "exceptions": exceptions,
        "exception_cn": EXCEPTION_CN,
        "events": events,
        "users": users,
        "tractors": tractors,
        "trailers": trailers,
        "drivers": drivers,
        "appointments": appointments,
        "closed_today": closed_today,
    }
