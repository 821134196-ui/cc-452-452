"""高峰样例数据（车牌/司机均为虚构）。

reseed 会清空数据库并重建一组早高峰场景：
  * 待卸挂队列（含一单超时待提醒、一单迟到）
  * 分位待解绑 / 待查验 / 待轮挡 各阶段在途工单
  * 已完成甩挂等待提挂、提挂核对中（铅封不符 → 待主管批异常放行）
  * 一单已挂接待出园、一单已关闭
  * 异常单覆盖：迟到、车位占用由操作时产生、钥匙错格、铅封不符、
    挂车损伤、司机替换、异常放行申请
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from .adapters import GateAdapter, KeyCabinetAdapter, PhotoStore
from .db import connect, init_db
from .service import YardService, iso
from .settings import DB_PATH, PHOTO_DIR, QUEUE_TIMEOUT_S

USERS = [
    ("zhang", "张门岗", "guard"),
    ("li", "李调度", "dispatcher"),
    ("wang", "王调度", "dispatcher"),
    ("zhou", "周钥匙", "key_clerk"),
    ("wu", "吴查验", "inspector"),
    ("zhao", "赵值班", "supervisor"),
]

# 虚构牵引车 / 挂车 / 司机
TRACTORS = [(f"沪D·T{1000+i}", f"LSJNT{1000+i}", "远帆物流" if i % 2 else "东陆集卡")
            for i in range(1, 13)]
TRAILERS = [(f"沪E·G{2000+i}挂", f"GH{2000+i}", "远帆物流" if i % 2 else "东陆集卡")
            for i in range(1, 13)]
DRIVERS = [
    ("310026000001", "陈师傅", "139-0000-0001"),
    ("310026000002", "林师傅", "139-0000-0002"),
    ("310026000003", "高师傅", "139-0000-0003"),
    ("310026000004", "黄师傅", "139-0000-0004"),
    ("310026000005", "徐师傅", "139-0000-0005"),
    ("310026000006", "马师傅", "139-0000-0006"),
    ("310026000007", "罗师傅", "139-0000-0007"),
    ("310026000008", "何师傅", "139-0000-0008"),
    ("310026000009", "郭师傅", "139-0000-0009"),
    ("310026000010", "梁师傅", "139-0000-0010"),
]


class _Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, minutes: int = 1):
        self.t += timedelta(minutes=minutes)


def _insert_masters(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO users(username,display_name,role) VALUES(?,?,?)", USERS)
    conn.executemany(
        "INSERT INTO tractors(plate,vin,fleet) VALUES(?,?,?)", TRACTORS)
    conn.executemany(
        "INSERT INTO trailers(plate,number,fleet) VALUES(?,?,?)", TRAILERS)
    conn.executemany(
        "INSERT INTO drivers(license_no,name,phone) VALUES(?,?,?)", DRIVERS)
    # 车位：A/B/C 三个区，每区 3 行 × 8 列，编号 区-行列，如 A-101
    for zone in ("A", "B", "C"):
        for row in range(1, 4):
            for col in range(1, 9):
                conn.execute(
                    "INSERT INTO stalls(code,zone,grid_row,grid_col,status) VALUES(?,?,?,?,'free')",
                    (f"{zone}-{row}{col:02d}", zone, row, col))
    conn.commit()


def _appt(conn, code, t_plate, v_plate, lic, planned, purpose="drop", status="booked"):
    conn.execute(
        "INSERT INTO appointments(code,tractor_plate,trailer_plate,driver_license,"
        "purpose,planned_arrival,status) VALUES(?,?,?,?,?,?,?)",
        (code, t_plate, v_plate, lic, purpose, iso(planned), status))


APPT_PLAN = [
    # (车牌序号, 计划到点) —— 计划时刻略晚于实际签到，模拟准点；末区一单 07:40 为迟到
    (1, "08:10"), (2, "08:15"), (4, "08:20"), (5, "08:25"),
    (7, "08:30"), (2, "08:33"), (9, "08:40"), (10, "08:42"),
    (11, "07:40"), (12, "08:45"), (3, "08:50"),
]


def build_scenario(conn: sqlite3.Connection, photo_dir: Path) -> None:
    photos = PhotoStore(photo_dir)
    clock = _Clock(datetime(2026, 9, 30, 7, 55))
    svc = YardService(conn, photos, GateAdapter(), KeyCabinetAdapter(), clock=clock)

    conn.execute("DELETE FROM appointments")
    conn.commit()
    base = datetime(2026, 9, 30, 0, 0)
    for n, (idx, hm) in enumerate(APPT_PLAN, start=1):
        h, m = map(int, hm.split(":"))
        planned = base.replace(hour=h, minute=m)
        t_plate = f"沪D·T{1000+idx}"
        v_plate = f"沪E·G{2000+idx}挂"
        lic = DRIVERS[(idx - 1) % len(DRIVERS)][0]
        # 第二辆 T1002 当天第二次入园，挂新挂
        if n == 6:
            v_plate = f"沪E·G{2008}挂"
            lic = DRIVERS[1][0]
        _appt(conn, f"AP0930-{n:02d}", t_plate, v_plate, lic, planned)
    conn.commit()

    def cell(token: str) -> str:
        return svc.cabinet.cell_for_token(token)

    # C1 完整甩挂交接，停 A-101，等待提挂
    clock.t = base.replace(hour=8, minute=2)
    c1 = svc.check_in("zhang", "沪D·T1001")["cycle"]
    clock.advance(4)
    svc.assign_stall("li", c1, "A-101")
    clock.advance(2)
    svc.unhitch("li", c1)
    clock.advance(3)
    svc.intake_check("wu", c1, "SEAL-A001")
    clock.advance(1)
    svc.chock("li", c1)
    clock.advance(1)
    svc.store_keys("zhou", c1, "KB-01", cell("KB-01"))

    # C2 甩挂完成（入位发现左部损伤），提挂司机替换，待核对
    clock.t = base.replace(hour=8, minute=9)
    c2 = svc.check_in("zhang", "沪D·T1002")["cycle"]
    clock.advance(3)
    svc.assign_stall("li", c2, "A-102")
    svc.unhitch("li", c2)
    clock.advance(2)
    svc.intake_check("wu", c2, "SEAL-A002",
                     damages={"left": "厢板划痕约 12cm"})
    svc.chock("li", c2)
    svc.store_keys("zhou", c2, "KB-02", cell("KB-02"))
    clock.advance(6)
    svc.start_pickup("li", c2, "沪D·T1003", DRIVERS[2][0])  # 司机替换

    # C4 钥匙先错格再纠正，完成交接
    clock.t = base.replace(hour=8, minute=14)
    c4 = svc.check_in("zhang", "沪D·T1004")["cycle"]
    svc.assign_stall("wang", c4, "A-103")
    svc.unhitch("wang", c4)
    clock.advance(2)
    svc.intake_check("wu", c4, "SEAL-A004")
    svc.chock("wang", c4)
    try:
        svc.store_keys("zhou", c4, "KB-04", "R1-05")  # 错格
    except Exception:
        pass
    svc.store_keys("zhou", c4, "KB-04", cell("KB-04"))

    # C5 提挂铅封不符 → 申请异常放行，留待主管处理
    clock.t = base.replace(hour=8, minute=17)
    c5 = svc.check_in("zhang", "沪D·T1005")["cycle"]
    svc.assign_stall("li", c5, "A-104")
    svc.unhitch("li", c5)
    svc.intake_check("wu", c5, "SEAL-A005")
    svc.chock("li", c5)
    svc.store_keys("zhou", c5, "KB-05", cell("KB-05"))
    clock.advance(5)
    svc.start_pickup("li", c5, "沪D·T1006", DRIVERS[4][0])
    try:
        svc.verify_pickup("wu", c5, DRIVERS[4][0], "SEAL-X999")
    except Exception:
        pass
    svc.request_abnormal_exit("zhang", c5, "铅封与登记不符，货主现场确认更改，申请先放行")

    # C7 全流程到待出园
    clock.t = base.replace(hour=8, minute=20)
    c7 = svc.check_in("zhang", "沪D·T1007")["cycle"]
    svc.assign_stall("wang", c7, "A-105")
    svc.unhitch("wang", c7)
    svc.intake_check("wu", c7, "SEAL-A007")
    svc.chock("wang", c7)
    svc.store_keys("zhou", c7, "KB-07", cell("KB-07"))
    clock.advance(4)
    svc.start_pickup("li", c7, "沪D·T1008", DRIVERS[6][0])
    svc.verify_pickup("wu", c7, DRIVERS[6][0], "SEAL-A007")
    svc.retrieve_keys("zhou", c7, "KB-07", cell("KB-07"))
    svc.hitch("li", c7)  # → exit_pending，等待门岗出园

    # C8（T1002 二次入园，挂 G2008）全流程关闭
    clock.t = base.replace(hour=8, minute=23)
    c8 = svc.check_in("zhang", "沪D·T1002")["cycle"]
    svc.assign_stall("li", c8, "A-106")
    svc.unhitch("li", c8)
    svc.intake_check("wu", c8, "SEAL-A008")
    svc.chock("li", c8)
    svc.store_keys("zhou", c8, "KB-08", cell("KB-08"))
    svc.start_pickup("wang", c8, "沪D·T1004", DRIVERS[1][0])
    svc.verify_pickup("wu", c8, DRIVERS[1][0], "SEAL-A008")
    svc.retrieve_keys("zhou", c8, "KB-08", cell("KB-08"))
    svc.hitch("wang", c8)
    svc.exit_confirm("zhang", c8)  # closed

    # 在途各阶段：C9 已分位待解绑；C10 已解绑待查验
    clock.t = base.replace(hour=8, minute=26)
    c9 = svc.check_in("zhang", "沪D·T1009")["cycle"]   # 排队中
    c10 = svc.check_in("zhang", "沪D·T1010")["cycle"]  # 排队中
    c11 = svc.check_in("zhang", "沪D·T1011")["cycle"]  # 迟到单（计划 07:40）
    clock.advance(2)
    c12 = svc.check_in("zhang", "沪D·T1012")["cycle"]
    svc.assign_stall("li", c12, "A-107")   # stall_assigned
    # 迟到单分到 A-108 并解绑，停在待查验
    svc.assign_stall("wang", c11, "A-108")
    svc.unhitch("wang", c11)

    # 让 c9 的排队等待在服务启动后立即超时，供进程内任务演示
    conn.execute(
        "UPDATE wait_tracking SET started_at=?, threshold_s=? WHERE cycle_id="
        "(SELECT id FROM cycles WHERE code=?) AND kind='queue'",
        (iso(datetime.now() - timedelta(seconds=QUEUE_TIMEOUT_S + 90)),
         QUEUE_TIMEOUT_S, c9))
    conn.commit()


def reseed(db_path: Path = DB_PATH, photo_dir: Path = PHOTO_DIR) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    for suffix in ("-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()
    if photo_dir.exists():
        for f in photo_dir.glob("*.svg"):
            f.unlink()
    conn = connect(db_path)
    init_db(conn)
    _insert_masters(conn)
    build_scenario(conn, photo_dir)
    return conn


def ensure_seed(db_path: Path = DB_PATH, photo_dir: Path = PHOTO_DIR) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    init_db(conn)
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        _insert_masters(conn)
        build_scenario(conn, photo_dir)
    return conn
