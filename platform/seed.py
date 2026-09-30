"""高峰样例数据与外观照片（SVG）生成。

运行：python -m platform.seed --reset
场景覆盖：排队超时、迟到、司机替换、钥匙错格、铅封不符、外观损伤、
无法提挂、异常批准放行、正常待出园等高峰实况。所有车牌、司机均为虚构。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
from datetime import datetime

from .adapters import CabinetAdapter, GateAdapter
from .clock import Clock
from .db import connect
from .permissions import ServiceError
from .service import YardService

# 沥青灰 / 车道白 / 信号黄
ASPHALT = "#2b2f33"
ASPHALT2 = "#3a3f45"
WHITE = "#f2f2f2"
YELLOW = "#f6c945"
RED = "#e05b4b"

POSITIONS = [
    ("front", "前端"),
    ("rear", "尾门"),
    ("left", "左侧"),
    ("right", "右侧"),
]


def photo_svg(pos: str, plate: str, damaged: bool, note: str) -> str:
    mark = ""
    if damaged:
        if pos in ("left", "right"):
            mark = (f'<path d="M150 120 q30 -26 60 0 t60 0" fill="none" '
                    f'stroke="{RED}" stroke-width="5" stroke-linecap="round"/>'
                    f'<circle cx="248" cy="112" r="6" fill="{RED}"/>')
        else:
            mark = (f'<rect x="190" y="120" width="100" height="46" rx="10" '
                    f'fill="none" stroke="{RED}" stroke-width="5"/>'
                    f'<path d="M196 126 l88 34 M284 126 l-88 34" '
                    f'stroke="{RED}" stroke-width="4"/>')
    if pos in ("left", "right"):
        body = (
            f'<rect x="60" y="90" width="360" height="120" rx="6" '
            f'fill="{ASPHALT2}" stroke="{WHITE}" stroke-width="3"/>'
            f'<rect x="78" y="106" width="324" height="88" rx="4" fill="none" '
            f'stroke="{YELLOW}" stroke-width="1.5" stroke-dasharray="10 8"/>'
            f'<rect x="40" y="150" width="26" height="14" fill="{ASPHALT2}" '
            f'stroke="{WHITE}" stroke-width="2"/>'
            f'<circle cx="130" cy="218" r="20" fill="#17191c" stroke="{WHITE}" stroke-width="3"/>'
            f'<circle cx="130" cy="218" r="8" fill="{ASPHALT2}"/>'
            f'<circle cx="350" cy="218" r="20" fill="#17191c" stroke="{WHITE}" stroke-width="3"/>'
            f'<circle cx="350" cy="218" r="8" fill="{ASPHALT2}"/>'
            f'{mark}')
        view_cn = "左侧厢面" if pos == "left" else "右侧厢面"
    elif pos == "front":
        body = (
            f'<rect x="140" y="70" width="200" height="170" rx="6" '
            f'fill="{ASPHALT2}" stroke="{WHITE}" stroke-width="3"/>'
            f'<rect x="158" y="88" width="164" height="70" rx="4" fill="none" '
            f'stroke="{YELLOW}" stroke-width="1.5"/>'
            f'<rect x="186" y="196" width="108" height="30" rx="4" fill="none" '
            f'stroke="{WHITE}" stroke-width="2"/>'
            f'<circle cx="175" cy="248" r="14" fill="#17191c" stroke="{WHITE}" stroke-width="2"/>'
            f'<circle cx="305" cy="248" r="14" fill="#17191c" stroke="{WHITE}" stroke-width="2"/>'
            f'{mark}')
        view_cn = "前端（支腿面）"
    else:
        body = (
            f'<rect x="140" y="70" width="200" height="170" rx="6" '
            f'fill="{ASPHALT2}" stroke="{WHITE}" stroke-width="3"/>'
            f'<line x1="240" y1="76" x2="240" y2="234" stroke="{YELLOW}" stroke-width="3"/>'
            f'<circle cx="240" cy="152" r="7" fill="none" stroke="{WHITE}" stroke-width="2"/>'
            f'<rect x="158" y="88" width="164" height="130" rx="4" fill="none" '
            f'stroke="{WHITE}" stroke-width="1.5" stroke-dasharray="8 6"/>'
            f'<circle cx="175" cy="248" r="14" fill="#17191c" stroke="{WHITE}" stroke-width="2"/>'
            f'<circle cx="305" cy="248" r="14" fill="#17191c" stroke="{WHITE}" stroke-width="2"/>'
            f'{mark}')
        view_cn = "尾门（铅封面）"
    flag = (f'<rect x="24" y="22" width="150" height="30" rx="4" fill="{RED}"/>'
            f'<text x="38" y="43" font-size="18" fill="{WHITE}">异常标记</text>') \
        if damaged else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="480" height="300" viewBox="0 0 480 300">
  <rect width="480" height="300" fill="{ASPHALT}"/>
  <line x1="0" y1="262" x2="480" y2="262" stroke="{WHITE}" stroke-width="2"
        stroke-dasharray="26 18" opacity="0.55"/>
  {flag}
  <text x="24" y="40" font-size="18" fill="{YELLOW}">{plate} · {view_cn}</text>
  {body}
  <text x="24" y="288" font-size="13" fill="{WHITE}" opacity="0.8">
    {note}（模拟相机拍摄，样例图）</text>
</svg>
"""


def seed(db_path: str, photos_dir: str, reset: bool = True) -> YardService:
    if reset:
        if os.path.exists(db_path):
            os.remove(db_path)
        for suffix in ("-wal", "-shm"):
            p = db_path + suffix
            if os.path.exists(p):
                os.remove(p)
        if os.path.isdir(photos_dir):
            shutil.rmtree(photos_dir)
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    os.makedirs(photos_dir, exist_ok=True)

    conn = connect(db_path)
    clock = Clock(datetime(2026, 9, 30, 7, 0))
    svc = YardService(db_path, clock, GateAdapter(), CabinetAdapter())

    # ---------------------------------------------------------- 基础资料
    staff = [
        ("gate01", "周永福", "gate"),
        ("disp01", "张敏", "dispatcher"),
        ("disp02", "李国梁", "dispatcher"),
        ("key01", "赵小钥", "keyclerk"),
        ("insp01", "孙查验", "inspector"),
        ("sup01", "钱振山", "supervisor"),
    ]
    conn.executemany(
        "INSERT INTO staff(login,name,role) VALUES(?,?,?)", staff)

    drivers = [
        ("马铁柱", "320311198703052211", "139-0000-1801", "宏远运输"),
        ("罗向前", "320311198209123417", "139-0000-1802", "宏远运输"),
        ("吴大江", "320311199101246633", "139-0000-1803", "顺安达车队"),
        ("郑海生", "320311197906181139", "139-0000-1804", "顺安达车队"),
        ("冯守义", "320311198511304415", "139-0000-1805", "冀通物流"),
        ("褚连军", "320311199207075524", "139-0000-1806", "冀通物流"),
        ("卫成栋", "320311198812017736", "139-0000-1807", "海州快运"),
        ("蒋东雷", "320311199304158842", "139-0000-1808", "海州快运"),
        ("沈庆山", "320311198402289958", "139-0000-1809", "乾通货运"),
        ("韩夜行", "320311199005172016", "139-0000-1810", "乾通货运"),
    ]
    conn.executemany(
        "INSERT INTO drivers(name,license_no,phone,employer) VALUES(?,?,?,?)",
        drivers)

    tractor_rows = []
    for i in range(1, 16):
        plate = f"沪B·T{1000+i}"
        cur = conn.execute(
            "INSERT INTO tractors(plate_no,model,status) VALUES(?,?,'idle')",
            (plate, ["解放 J6P", "东风天龙", "重汽豪沃"][i % 3]))
        tractor_rows.append(cur.lastrowid)

    trailer_rows = []
    seals = []
    for i in range(1, 13):
        plate = f"沪E·G{3000+i}"
        seal = f"SL-7{300+i}-26"
        cur = conn.execute(
            "INSERT INTO trailers(plate_no,spec,seal_no,status) "
            "VALUES(?,'13.95m 三轴厢式半挂车',?,'enroute')",
            (plate, seal))
        trailer_rows.append(cur.lastrowid)
        seals.append(seal)

    # 车位：A 区 4×6，B 区 3×6，C 区 3×4（真实编号平面行列）
    layout = (("A", 4, 6), ("B", 3, 6), ("C", 3, 4))
    for zone, rows_n, cols_n in layout:
        for r in range(1, rows_n + 1):
            for c in range(1, cols_n + 1):
                code = f"{zone}{r}{c:02d}"
                conn.execute(
                    "INSERT INTO parking_slots(code,zone,row_no,col_no,kind,status) "
                    "VALUES(?,?,?,?,'standard','free')",
                    (code, zone, r, c))
    conn.execute("UPDATE parking_slots SET status='locked' WHERE code='C304'")

    def appointment(idx, tractor_i, trailer_i, driver_i, planned, kind="both"):
        code = f"AP0930-{idx:02d}"
        conn.execute(
            """INSERT INTO appointments(code,tractor_id,trailer_id,driver_id,
                   planned_arrival,kind,status,note)
               VALUES(?,?,?,?,?,?,'scheduled','高峰样例（虚构）')""",
            (code, tractor_rows[tractor_i - 1], trailer_rows[trailer_i - 1],
             driver_i, planned, kind))
        return code

    P = lambda h, m: datetime(2026, 9, 30, h, m).strftime("%Y-%m-%d %H:%M:%S")

    ap01 = appointment(1, 1, 1, 1, P(7, 0))
    ap02 = appointment(2, 2, 2, 2, P(7, 50), "drop")
    ap03 = appointment(3, 3, 3, 3, P(8, 25), "drop")
    ap04 = appointment(4, 4, 4, 4, P(8, 0), "drop")
    ap05 = appointment(5, 5, 5, 5, P(7, 30))
    ap06 = appointment(6, 6, 6, 6, P(7, 55))
    ap07 = appointment(7, 7, 7, 7, P(7, 45))
    ap08 = appointment(8, 8, 8, 8, P(8, 0))
    ap09 = appointment(9, 9, 9, 9, P(8, 5))
    appointment(10, 10, 10, 3, P(9, 10), "pickup")
    appointment(11, 15, 11, 5, P(8, 0), "drop")

    GATE, DISP1, DISP2, KEY, INSP, SUP = 1, 2, 3, 4, 5, 6

    def take_photos(turn_id: int, damaged_pos: dict[str, str] | None = None):
        damaged_pos = damaged_pos or {}
        out = []
        trailer = conn.execute(
            "SELECT plate_no FROM trailers WHERE id="
            "(SELECT trailer_id FROM turns WHERE id=?)", (turn_id,)).fetchone()
        tdir = os.path.join(photos_dir, f"turn_{turn_id}")
        os.makedirs(tdir, exist_ok=True)
        for pos, cn in POSITIONS:
            note = damaged_pos.get(pos)
            svg = photo_svg(pos, trailer["plate_no"], bool(note),
                            note or f"{cn}外观完好，封签位置正常")
            fn = os.path.join(tdir, f"{pos}.svg")
            with open(fn, "w", encoding="utf-8") as f:
                f.write(svg)
            out.append({"pos": pos, "path": f"turn_{turn_id}/{pos}.svg",
                        "note": note or f"{cn}完好"})
        return out

    # 1) 历史完整周转（07:05-07:42，已关闭）
    clock.set(datetime(2026, 9, 30, 7, 5))
    r = svc.arrive(GATE, ap01)
    t1 = r["turn_id"]
    svc.inspect_appearance(INSP, t1, "arrival", "pass", photos=take_photos(t1))
    clock.set(datetime(2026, 9, 30, 7, 8))
    svc.assign_slot(DISP1, t1, "A204")
    svc.unhitch(DISP1, t1)
    svc.chock(DISP2, t1)
    svc.deposit_key(KEY, t1, "BOX-A", "07")
    clock.set(datetime(2026, 9, 30, 7, 35))
    svc.start_pickup(DISP1, t1, tractor_rows[10], 1)   # R11 提挂
    svc.verify_seal(INSP, t1, seals[0])
    svc.inspect_appearance(INSP, t1, "departure", "pass",
                           photos=take_photos(t1))
    svc.hitch(DISP1, t1)
    svc.release_key(KEY, t1)
    clock.set(datetime(2026, 9, 30, 7, 42))
    svc.exit_gate(GATE, t1)

    # 高峰当前时刻 08:30 各状态 --------------------------------
    # 2) 排队 34 分钟（超时）；3) 刚到排队
    clock.set(datetime(2026, 9, 30, 7, 56))
    t2 = svc.arrive(GATE, ap02)["turn_id"]
    svc.inspect_appearance(INSP, t2, "arrival", "pass", photos=take_photos(t2))
    clock.set(datetime(2026, 9, 30, 8, 28))
    t3 = svc.arrive(GATE, ap03)["turn_id"]
    svc.inspect_appearance(INSP, t3, "arrival", "pass", photos=take_photos(t3))

    # 4) 已入位 A103，尚未解绑
    clock.set(datetime(2026, 9, 30, 8, 10))
    t4 = svc.arrive(GATE, ap04)["turn_id"]
    svc.inspect_appearance(INSP, t4, "arrival", "pass", photos=take_photos(t4))
    clock.set(datetime(2026, 9, 30, 8, 15))
    svc.assign_slot(DISP1, t4, "A103")

    # 5) 交接完成挂车等提挂；演示钥匙错格（B12 有遗留钥匙）
    clock.set(datetime(2026, 9, 30, 7, 50))
    t5 = svc.arrive(GATE, ap05)["turn_id"]   # 预约 07:30 -> 迟到 20 分钟
    svc.inspect_appearance(INSP, t5, "arrival", "pass", photos=take_photos(t5))
    clock.set(datetime(2026, 9, 30, 7, 55))
    svc.assign_slot(DISP2, t5, "B202")
    svc.unhitch(DISP2, t5)
    svc.chock(DISP1, t5)
    svc.cabinet.put("KEY-LEFT-9231", "BOX-B", "12")  # 柜内遗留（模拟）
    try:
        svc.deposit_key(KEY, t5, "BOX-B", "12")
    except ServiceError:
        pass
    svc.deposit_key(KEY, t5, "BOX-B", "13")  # 改格口，交接完成

    # 6) 提挂中铅封不符 -> 挂起，等待主管审批（待批）
    clock.set(datetime(2026, 9, 30, 8, 0))
    t6 = svc.arrive(GATE, ap06)["turn_id"]
    svc.inspect_appearance(INSP, t6, "arrival", "pass", photos=take_photos(t6))
    clock.set(datetime(2026, 9, 30, 8, 4))
    svc.assign_slot(DISP1, t6, "B205")
    svc.unhitch(DISP1, t6)
    svc.chock(DISP1, t6)
    svc.deposit_key(KEY, t6, "BOX-B", "05")
    clock.set(datetime(2026, 9, 30, 8, 12))
    svc.start_pickup(DISP2, t6, tractor_rows[11], 7)  # R12/司机7 替换
    seal_res = svc.verify_seal(INSP, t6, "SL-7306-99")  # 错误铅封号
    assert seal_res["match"] is False
    svc.request_approval(DISP2, t6, "seal_mismatch",
                         "实车铅封 SL-7306-99 与登记 SL-7306-26 不符，司机称途中查验")

    # 7) 提挂外观损伤 -> 主管已批准异常放行 -> 已挂接待出园
    clock.set(datetime(2026, 9, 30, 7, 52))
    t7 = svc.arrive(GATE, ap07)["turn_id"]
    svc.inspect_appearance(INSP, t7, "arrival", "pass", photos=take_photos(t7))
    clock.set(datetime(2026, 9, 30, 7, 58))
    svc.assign_slot(DISP2, t7, "B301")
    svc.unhitch(DISP2, t7)
    svc.chock(DISP2, t7)
    svc.deposit_key(KEY, t7, "BOX-B", "01")
    clock.set(datetime(2026, 9, 30, 8, 5))
    svc.start_pickup(DISP2, t7, tractor_rows[12], 8)  # R13/司机8
    svc.verify_seal(INSP, t7, seals[6])
    dmg = {"left": "左侧厢板约 12cm 凹陷划痕", "rear": "尾门左下角碰撞变形"}
    svc.inspect_appearance(INSP, t7, "departure", "damage",
                           dmg["left"] + "；" + dmg["rear"],
                           photos=take_photos(t7, dmg))
    ap_id = svc.request_approval(DISP2, t7, "appearance_damage",
                                 "左厢板凹陷、尾门变形，货主已知情同意提走出险")["approval_id"]
    clock.set(datetime(2026, 9, 30, 8, 20))
    svc.decide_approval(SUP, ap_id, True)
    svc.hitch(DISP2, t7)
    svc.release_key(KEY, t7)  # ready_to_exit，出园时需异常放行校验

    # 8) 无法提挂（牵引鞍座故障）；到园即有轻微损伤记录
    clock.set(datetime(2026, 9, 30, 8, 5))
    t8 = svc.arrive(GATE, ap08)["turn_id"]
    arr_dmg = {"right": "右侧防护栏轻微刮擦（到园登记）"}
    svc.inspect_appearance(INSP, t8, "arrival", "damage",
                           arr_dmg["right"], photos=take_photos(t8, arr_dmg))
    clock.set(datetime(2026, 9, 30, 8, 9))
    svc.assign_slot(DISP1, t8, "C101")
    svc.unhitch(DISP1, t8)
    svc.chock(DISP1, t8)
    svc.deposit_key(KEY, t8, "BOX-C", "03")
    clock.set(datetime(2026, 9, 30, 8, 24))
    svc.start_pickup(DISP1, t8, tractor_rows[13], 8)  # R13
    svc.verify_seal(INSP, t8, seals[7])
    svc.cannot_pickup(DISP1, t8, "牵引鞍座气路故障，无法锁销，司机申请改派")

    # 9) 全流程到待出园（正常）；到园司机替换、提挂司机再次替换
    clock.set(datetime(2026, 9, 30, 8, 9))
    t9 = svc.arrive(GATE, ap09, actual_driver_id=10)["turn_id"]  # 实际司机10
    svc.inspect_appearance(INSP, t9, "arrival", "pass", photos=take_photos(t9))
    clock.set(datetime(2026, 9, 30, 8, 13))
    svc.assign_slot(DISP2, t9, "C202")
    svc.unhitch(DISP2, t9)
    svc.chock(DISP2, t9)
    svc.deposit_key(KEY, t9, "BOX-C", "06")
    clock.set(datetime(2026, 9, 30, 8, 26))
    svc.start_pickup(DISP1, t9, tractor_rows[13], 9)  # R14/司机9
    svc.verify_seal(INSP, t9, seals[8])
    svc.inspect_appearance(INSP, t9, "departure", "pass", photos=take_photos(t9))
    svc.hitch(DISP1, t9)
    svc.release_key(KEY, t9)

    # 回到高峰时刻，扫描排队超时
    clock.set(datetime(2026, 9, 30, 8, 30))
    svc.scan_wait_timeouts()

    conn.commit()
    return svc


def main() -> None:
    ap = argparse.ArgumentParser(description="加载甩挂平台高峰样例")
    ap.add_argument("--db", default="data/yard.db")
    ap.add_argument("--photos", default="data/photos")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()
    svc = seed(args.db, args.photos, reset=True)
    c = svc.conn
    print("样例加载完成：")
    for table in ("staff", "drivers", "tractors", "trailers", "parking_slots",
                  "appointments", "turns", "hitches", "keys", "inspections",
                  "approvals", "events"):
        n = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table:14s} {n}")


if __name__ == "__main__":
    main()
