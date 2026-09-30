"""服务层：交接流程状态机、挂接唯一性、车位竞争、异常与角色权限。

周转阶段（cycles.phase）：
  queued → stall_assigned → unhitched → parked → chocked → keys_cabinet
  （甩挂交接完成）→ pickup_pending → pickup_verified → hitched
  → exit_pending → closed

并发控制：
  * 分位用 stalls 条件 UPDATE（compare-and-set），两个调度员抢同一车位仅首个成功；
  * 挂接/解绑依赖 couplings 的部分唯一索引，同一挂车有效挂接唯一。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from .adapters import SIDES, GateAdapter, KeyCabinetAdapter, PhotoStore, trailer_number_of
from .settings import PERMISSIONS, PICKUP_TIMEOUT_S, QUEUE_TIMEOUT_S


class ServiceError(Exception):
    """业务规则错误（非法状态、竞争失败、权限不足等）。"""

    def __init__(self, message: str, code: str = "rule"):
        super().__init__(message)
        self.code = code


class PermissionErrorSvc(ServiceError):
    def __init__(self, user: str, action: str):
        super().__init__(f"无权操作：{user} 不能执行 {action}", "forbidden")
        self.code = "forbidden"


def now() -> datetime:
    return datetime.now()


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 权限

def _require(conn: sqlite3.Connection, username: str, action: str) -> sqlite3.Row:
    u = conn.execute("SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()
    if u is None:
        raise ServiceError(f"用户不存在或已停用：{username}", "forbidden")
    if u["role"] not in PERMISSIONS[action]:
        raise PermissionErrorSvc(f"{u['display_name']}({u['role']})", action)
    return u


# ---------------------------------------------------------------- 事件/异常

def log_event(conn: sqlite3.Connection, kind: str, actor: str | None,
              detail: str, cycle_code: str | None = None, commit: bool = True) -> None:
    conn.execute(
        "INSERT INTO events(ts,cycle_code,kind,actor,detail) VALUES(?,?,?,?,?)",
        (iso(now()), cycle_code, kind, actor, detail),
    )
    if commit:
        conn.commit()


def raise_exception(conn: sqlite3.Connection, kind: str, raised_by: str,
                    description: str, cycle_code: str | None = None,
                    ref: str | None = None, commit: bool = True) -> int:
    cur = conn.execute(
        "INSERT INTO exceptions(ts,kind,cycle_code,ref,description,raised_by) "
        "VALUES(?,?,?,?,?,?)",
        (iso(now()), kind, cycle_code, ref, description, raised_by),
    )
    log_event(conn, f"exception_{kind}", raised_by, description, cycle_code, commit=False)
    if commit:
        conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------- 辅助

def _get_cycle(conn: sqlite3.Connection, code: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM cycles WHERE code=?", (code,)).fetchone()
    if row is None:
        raise ServiceError(f"周转工单不存在：{code}", "not_found")
    return row


def _expect_phase(cycle: sqlite3.Row, allowed: set[str]) -> None:
    if cycle["phase"] not in allowed:
        raise ServiceError(
            f"工单 {cycle['code']} 当前阶段为 {cycle['phase']}，不能执行该操作",
            "bad_phase",
        )


def _seal_expected(conn: sqlite3.Connection, cycle_id: int) -> str | None:
    row = conn.execute(
        "SELECT seal_code FROM seal_records WHERE cycle_id=? AND stage='intake' "
        "ORDER BY id DESC LIMIT 1",
        (cycle_id,),
    ).fetchone()
    return row["seal_code"] if row else None


class YardService:
    def __init__(self, conn: sqlite3.Connection, photos: PhotoStore,
                 gate: GateAdapter | None = None, cabinet: KeyCabinetAdapter | None = None,
                 clock=None):
        self.conn = conn
        self.photos = photos
        self.gate = gate or GateAdapter()
        self.cabinet = cabinet or KeyCabinetAdapter()
        self.clock = clock or now

    def _ts(self) -> str:
        return iso(self.clock())

    # ============================================================ 1. 到园签到

    def check_in(self, username: str, plate: str, gate_lane: str = "G1") -> dict:
        """门岗扫描车牌 → 迟到判定 → 进入待卸挂队列。"""
        u = _require(self.conn, username, "check_in")
        scan = self.gate.scan_plate(gate_lane, plate)
        appt = self.conn.execute(
            "SELECT * FROM appointments WHERE tractor_plate=? AND status IN ('booked','late') "
            "AND linked_cycle IS NULL ORDER BY id ASC LIMIT 1",
            (plate,),
        ).fetchone()
        if appt is None:
            raise ServiceError(f"未找到牵引车 {plate} 的有效预约", "no_appointment")

        late = False
        if self._ts() > appt["planned_arrival"]:
            late = True
            self.conn.execute("UPDATE appointments SET status='late' WHERE id=?", (appt["id"],))
            raise_exception(
                self.conn, "late_arrival", u["display_name"],
                f"预约 {appt['code']} 计划 {appt['planned_arrival']} 后迟到（{plate}）",
                cycle_code=None, ref=appt["code"], commit=False,
            )
        else:
            self.conn.execute("UPDATE appointments SET status='arrived' WHERE id=?", (appt["id"],))

        code = f"YC{self._ts()[2:10].replace('-', '')}-{appt['id']:03d}"
        cur = self.conn.execute(
            "INSERT INTO cycles(code,tractor_plate,trailer_plate,driver_license,"
            "appointment_code,purpose,phase,arrived_at,queued_at) "
            "VALUES(?,?,?,?,?,?,'queued',?,?)",
            (code, appt["tractor_plate"], appt["trailer_plate"], appt["driver_license"],
             appt["code"], appt["purpose"], self._ts(), self._ts()),
        )
        cycle_id = cur.lastrowid
        self.conn.execute("UPDATE appointments SET linked_cycle=? WHERE id=?", (cycle_id, appt["id"]))
        self.conn.execute(
            "INSERT INTO wait_tracking(cycle_id,kind,started_at,threshold_s) VALUES(?,?,?,?)",
            (cycle_id, "queue", self._ts(), QUEUE_TIMEOUT_S),
        )
        self.gate.raise_barrier(gate_lane, plate, "签到放行")
        wait_min = 0
        log_event(self.conn, "check_in", u["display_name"],
                  f"{plate} 签到{'（迟到）' if late else ''}，挂 {appt['trailer_plate']} 进入待卸挂队列",
                  code, commit=False)
        self.conn.commit()
        return {"cycle": code, "late": late, "scan": scan, "wait_minutes": wait_min}

    # ============================================================ 2. 分位（竞争）

    def assign_stall(self, username: str, cycle_code: str, stall_code: str) -> dict:
        """调度员分配车位。两个调度员抢同一车位：条件 UPDATE 仅首个成功。"""
        u = _require(self.conn, username, "assign_stall")
        cycle = _get_cycle(self.conn, cycle_code)
        _expect_phase(cycle, {"queued"})
        stall = self.conn.execute("SELECT * FROM stalls WHERE code=?", (stall_code,)).fetchone()
        if stall is None:
            raise ServiceError(f"车位不存在：{stall_code}", "not_found")

        # compare-and-set：仅当 free 才占用
        cur = self.conn.execute(
            "UPDATE stalls SET status='occupied' WHERE id=? AND status='free'",
            (stall["id"],),
        )
        if cur.rowcount == 0:
            # 车位已被占用 → 异常：车位占用
            occ = self.conn.execute(
                "SELECT trailer_plate FROM stall_occupancy WHERE stall_id=? AND released=0",
                (stall["id"],),
            ).fetchone()
            who = occ["trailer_plate"] if occ else "未知"
            raise_exception(
                self.conn, "stall_occupied", u["display_name"],
                f"调度 {u['display_name']} 抢占 {stall_code} 失败，已被 {who} 占用",
                cycle_code, stall_code, commit=False,
            )
            self.conn.commit()
            raise ServiceError(f"车位 {stall_code} 已被占用（{who}），抢占失败", "stall_race")

        self.conn.execute(
            "UPDATE cycles SET phase='stall_assigned', stall_id=?, assigned_at=? WHERE id=?",
            (stall["id"], self._ts(), cycle["id"]),
        )
        self.conn.execute(
            "INSERT INTO stall_occupancy(stall_id,trailer_plate,cycle_id,since) VALUES(?,?,?,?)",
            (stall["id"], cycle["trailer_plate"], cycle["id"], self._ts()),
        )
        # 排队等待计时结束
        waited = self._close_wait(cycle["id"], "queue")
        log_event(self.conn, "assign_stall", u["display_name"],
                  f"分配 {stall_code} 给 {cycle['trailer_plate']}，排队等待 {waited} 分钟",
                  cycle_code, commit=False)
        self.conn.commit()
        return {"stall": stall_code, "queue_wait_minutes": waited}

    def _close_wait(self, cycle_id: int, kind: str) -> int:
        row = self.conn.execute(
            "SELECT * FROM wait_tracking WHERE cycle_id=? AND kind=? AND done=0",
            (cycle_id, kind),
        ).fetchone()
        if row is None:
            return 0
        started = datetime.strptime(row["started_at"], "%Y-%m-%d %H:%M:%S")
        mins = max(0, int((self.clock() - started).total_seconds() // 60))
        self.conn.execute("UPDATE wait_tracking SET done=1, alerted=1 WHERE id=?", (row["id"],))
        col = "wait_minutes" if kind == "queue" else "pickup_wait_minutes"
        self.conn.execute(f"UPDATE cycles SET {col}=? WHERE id=?", (mins, cycle_id))
        return mins

    # ============================================================ 3. 解绑牵引车与挂车

    def unhitch(self, username: str, cycle_code: str) -> dict:
        u = _require(self.conn, username, "unhitch")
        cycle = _get_cycle(self.conn, cycle_code)
        _expect_phase(cycle, {"stall_assigned"})

        # 解除当前有效挂接；唯一索引保证一车一挂
        cur = self.conn.execute(
            "UPDATE couplings SET deleted=1, uncoupled_at=? "
            "WHERE trailer_plate=? AND deleted=0",
            (self._ts(), cycle["trailer_plate"]),
        )
        if cur.rowcount == 0:
            # 无登记挂接也要允许解绑流转，但记录说明
            log_event(self.conn, "unhitch", u["display_name"],
                      f"{cycle['trailer_plate']} 无在册有效挂接，按现场解绑登记",
                      cycle_code, commit=False)
        else:
            log_event(self.conn, "unhitch", u["display_name"],
                      f"{cycle['tractor_plate']} 与 {cycle['trailer_plate']} 解除挂接",
                      cycle_code, commit=False)
        self.conn.execute(
            "UPDATE cycles SET phase='unhitched', unhitched_at=? WHERE id=?",
            (self._ts(), cycle["id"]),
        )
        self.conn.commit()
        return {"uncoupled": cur.rowcount}

    # ============================================================ 4. 入位外观 + 铅封

    def intake_check(self, username: str, cycle_code: str, seal_code: str,
                     damages: dict[str, str] | None = None) -> dict:
        """查验员登记铅封号并对 5 个方位拍照；损伤方位记入异常。"""
        u = _require(self.conn, username, "intake_check")
        cycle = _get_cycle(self.conn, cycle_code)
        _expect_phase(cycle, {"unhitched"})
        damages = damages or {}

        tnum = trailer_number_of(self.conn, cycle["trailer_plate"])
        files = []
        for side in SIDES:
            note = damages.get(side)
            fname = self.photos.save(tnum, cycle["trailer_plate"], "intake", side, note)
            self.conn.execute(
                "INSERT INTO appearance_checks(cycle_id,trailer_plate,stage,side,"
                "photo_file,damage,note,checked_by,checked_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (cycle["id"], cycle["trailer_plate"], "intake", side, fname,
                 1 if note else 0, note, u["display_name"], self._ts()),
            )
            files.append(fname)
            if note:
                raise_exception(
                    self.conn, "trailer_damage", u["display_name"],
                    f"{cycle['trailer_plate']} {side} 部发现损伤：{note}",
                    cycle_code, ref=fname, commit=False,
                )

        self.conn.execute(
            "INSERT INTO seal_records(cycle_id,trailer_plate,stage,seal_code,checked_by,checked_at) "
            "VALUES(?,?, 'intake', ?,?,?)",
            (cycle["id"], cycle["trailer_plate"], seal_code, u["display_name"], self._ts()),
        )
        self.conn.execute(
            "UPDATE cycles SET phase='parked', seal_code=?, parked_at=?, appearance_confirmed=1 WHERE id=?",
            (seal_code, self._ts(), cycle["id"]),
        )
        log_event(self.conn, "intake_check", u["display_name"],
                  f"{cycle['trailer_plate']} 停妥，铅封 {seal_code}，外观 {len(files)} 张"
                  + (f"，损伤 {len(damages)} 处" if damages else ""),
                  cycle_code, commit=False)
        self.conn.commit()
        return {"photos": files, "seal": seal_code, "damages": list(damages.keys())}

    # ============================================================ 5. 轮挡确认

    def chock(self, username: str, cycle_code: str) -> dict:
        u = _require(self.conn, username, "chock")
        cycle = _get_cycle(self.conn, cycle_code)
        _expect_phase(cycle, {"parked"})
        self.conn.execute(
            "UPDATE cycles SET phase='chocked', chocked_at=? WHERE id=?",
            (self._ts(), cycle["id"]),
        )
        log_event(self.conn, "chock", u["display_name"],
                  f"{cycle['trailer_plate']} 轮挡已放置确认", cycle_code, commit=False)
        self.conn.commit()
        return {"chocked": True}

    # ============================================================ 6. 钥匙入柜

    def store_keys(self, username: str, cycle_code: str, key_token: str, cell: str) -> dict:
        """钥匙员扫码入柜；放错格口 → 钥匙错格异常，交接不完成。"""
        u = _require(self.conn, username, "store_keys")
        cycle = _get_cycle(self.conn, cycle_code)
        _expect_phase(cycle, {"chocked"})

        result = self.cabinet.store(key_token, cell)
        if not result["ok"]:
            raise_exception(
                self.conn, "key_wrong_cell", u["display_name"],
                f"{cycle['trailer_plate']} 钥匙 {key_token} 放入 {cell}，应为 {result['expected_cell']}",
                cycle_code, ref=key_token, commit=False,
            )
            self.conn.execute(
                "INSERT INTO key_ledger(token,cell,trailer_plate,tractor_plate,status,"
                "logged_at,by_user,note) VALUES(?,?,?,?, 'misplaced',?,?,?)",
                (key_token, cell, cycle["trailer_plate"], cycle["tractor_plate"],
                 self._ts(), u["display_name"], result["message"]),
            )
            log_event(self.conn, "key_wrong_cell", u["display_name"], result["message"],
                      cycle_code, commit=False)
            self.conn.commit()
            raise ServiceError(result["message"], "key_wrong_cell")

        self.conn.execute(
            "INSERT INTO key_ledger(token,cell,trailer_plate,tractor_plate,status,"
            "logged_at,by_user,note) VALUES(?,?,?,?, 'in_cabinet',?,?,?)",
            (key_token, cell, cycle["trailer_plate"], cycle["tractor_plate"],
             self._ts(), u["display_name"], "甩挂交接钥匙入柜"),
        )
        self.conn.execute(
            "UPDATE cycles SET phase='keys_cabinet', keys_at=?, key_token=?, key_cell=? WHERE id=?",
            (self._ts(), key_token, cell, cycle["id"]),
        )
        log_event(self.conn, "keys_stored", u["display_name"],
                  f"钥匙 {key_token} 入柜 {cell}，甩挂交接完成", cycle_code, commit=False)
        self.conn.commit()
        return {"token": key_token, "cell": cell, "handoff_complete": True}

    # ============================================================ 7. 发起提挂

    def start_pickup(self, username: str, cycle_code: str,
                     tractor_plate: str, driver_license: str) -> dict:
        """调度员安排提挂车辆/司机；司机与预约不同 → 司机替换异常。"""
        u = _require(self.conn, username, "start_pickup")
        cycle = _get_cycle(self.conn, cycle_code)
        _expect_phase(cycle, {"keys_cabinet"})

        tractor = self.conn.execute("SELECT * FROM tractors WHERE plate=?", (tractor_plate,)).fetchone()
        if tractor is None:
            raise ServiceError(f"牵引车不存在：{tractor_plate}", "not_found")
        driver = self.conn.execute("SELECT * FROM drivers WHERE license_no=?", (driver_license,)).fetchone()
        if driver is None:
            raise ServiceError(f"司机不存在：{driver_license}", "not_found")

        replaced = driver_license != cycle["driver_license"]
        if replaced:
            orig = self.conn.execute("SELECT name FROM drivers WHERE license_no=?",
                                     (cycle["driver_license"],)).fetchone()
            raise_exception(
                self.conn, "driver_swap", u["display_name"],
                f"{cycle['trailer_plate']} 提挂司机由 {orig['name'] if orig else cycle['driver_license']}"
                f" 替换为 {driver['name']}（{driver_license}）",
                cycle_code, ref=driver_license, commit=False,
            )

        self.conn.execute(
            "UPDATE cycles SET phase='pickup_pending', pickup_tractor_plate=?, "
            "pickup_driver_license=?, pickup_wait_from=? WHERE id=?",
            (tractor_plate, driver_license, self._ts(), cycle["id"]),
        )
        self.conn.execute(
            "INSERT INTO wait_tracking(cycle_id,kind,started_at,threshold_s) VALUES(?,?,?,?)",
            (cycle["id"], "pickup", self._ts(), PICKUP_TIMEOUT_S),
        )
        log_event(self.conn, "start_pickup", u["display_name"],
                  f"提挂车辆 {tractor_plate} / 司机 {driver['name']}"
                  + ("（司机替换，已开异常单）" if replaced else ""),
                  cycle_code, commit=False)
        self.conn.commit()
        return {"driver_replaced": replaced}

    # ============================================================ 8. 提挂核对（司机/铅封/外观）

    def verify_pickup(self, username: str, cycle_code: str,
                      driver_license: str, seal_code: str,
                      pickup_damages: dict[str, str] | None = None) -> dict:
        u = _require(self.conn, username, "verify_pickup")
        cycle = _get_cycle(self.conn, cycle_code)
        _expect_phase(cycle, {"pickup_pending"})

        problems: list[str] = []
        # 司机核对
        driver_ok = driver_license == (cycle["pickup_driver_license"] or cycle["driver_license"])
        if not driver_ok:
            problems.append("司机不符")
            raise_exception(self.conn, "driver_swap", u["display_name"],
                            f"提挂到场司机 {driver_license} 与登记不符", cycle_code,
                            commit=False)
        # 铅封核对
        expected = _seal_expected(self.conn, cycle["id"]) or cycle["seal_code"]
        seal_ok = seal_code == expected
        self.conn.execute(
            "INSERT INTO seal_records(cycle_id,trailer_plate,stage,seal_code,matched,checked_by,checked_at) "
            "VALUES(?,?,'pickup',?,?,?,?)",
            (cycle["id"], cycle["trailer_plate"], seal_code, 1 if seal_ok else 0,
             u["display_name"], self._ts()),
        )
        if not seal_ok:
            problems.append(f"铅封不符（登记 {expected} / 现场 {seal_code}）")
            raise_exception(self.conn, "seal_mismatch", u["display_name"],
                            f"{cycle['trailer_plate']} 铅封不符：登记 {expected}，现场 {seal_code}",
                            cycle_code, ref=seal_code, commit=False)

        # 外观对照（与入位照片逐方位比对）
        tnum = trailer_number_of(self.conn, cycle["trailer_plate"])
        intake_damaged = {
            r["side"]: r["note"] for r in self.conn.execute(
                "SELECT side,note FROM appearance_checks WHERE cycle_id=? AND stage='intake'",
                (cycle["id"],)).fetchall()
        }
        pickup_damages = pickup_damages or {}
        new_damage: list[str] = []
        for side in SIDES:
            # 入位既有的伤留档对照；入位无损而提挂发现的伤为新增损伤
            note = intake_damaged.get(side)
            is_new = note is None and side in pickup_damages
            if is_new:
                note = pickup_damages[side]
            fname = self.photos.save(tnum, cycle["trailer_plate"], "pickup", side, note)
            if is_new:
                new_damage.append(side)
                raise_exception(
                    self.conn, "trailer_damage", u["display_name"],
                    f"{cycle['trailer_plate']} 提挂复核发现新增{side}部损伤：{note}",
                    cycle_code, ref=fname, commit=False,
                )
            self.conn.execute(
                "INSERT INTO appearance_checks(cycle_id,trailer_plate,stage,side,"
                "photo_file,damage,note,checked_by,checked_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (cycle["id"], cycle["trailer_plate"], "pickup", side, fname,
                 1 if note else 0, note, u["display_name"], self._ts()),
            )
        appearance_ok = len(new_damage) == 0
        if new_damage:
            problems.append(f"外观新增损伤 {len(new_damage)} 处")

        if problems:
            self.conn.commit()
            raise ServiceError("提挂核对未通过：" + "、".join(problems), "verify_failed")

        self.conn.execute(
            "UPDATE cycles SET phase='pickup_verified', driver_confirmed=1, seal_confirmed=1, "
            "appearance_confirmed=1 WHERE id=?",
            (cycle["id"],),
        )
        waited = self._close_wait(cycle["id"], "pickup")
        log_event(self.conn, "verify_pickup", u["display_name"],
                  f"司机/铅封 {seal_code}/外观五项核对通过，提挂等待 {waited} 分钟",
                  cycle_code, commit=False)
        self.conn.commit()
        return {"driver_ok": driver_ok, "seal_ok": seal_ok, "appearance_ok": appearance_ok,
                "pickup_wait_minutes": waited}

    # ============================================================ 9. 取钥匙 + 挂接（唯一性）

    def retrieve_keys(self, username: str, cycle_code: str, key_token: str, cell: str) -> dict:
        u = _require(self.conn, username, "retrieve_keys")
        cycle = _get_cycle(self.conn, cycle_code)
        _expect_phase(cycle, {"pickup_verified"})
        result = self.cabinet.retrieve(key_token, cell)
        if not result["ok"]:
            raise_exception(self.conn, "key_wrong_cell", u["display_name"],
                            f"取钥匙 {key_token} 格口不符：{result['message']}",
                            cycle_code, ref=key_token, commit=False)
            self.conn.commit()
            raise ServiceError(result["message"], "key_wrong_cell")
        self.conn.execute(
            "INSERT INTO key_ledger(token,cell,trailer_plate,tractor_plate,status,"
            "logged_at,by_user,note) VALUES(?,?,?,?, 'released',?,?,?)",
            (key_token, cell, cycle["trailer_plate"],
             cycle["pickup_tractor_plate"] or cycle["tractor_plate"],
             self._ts(), u["display_name"], "提挂取走钥匙"),
        )
        log_event(self.conn, "keys_retrieved", u["display_name"],
                  f"钥匙 {key_token} 从 {cell} 取走", cycle_code, commit=False)
        self.conn.commit()
        return result

    def hitch(self, username: str, cycle_code: str) -> dict:
        """挂接：依赖部分唯一索引，同一挂车并发挂接只有一个成功。"""
        u = _require(self.conn, username, "hitch")
        cycle = _get_cycle(self.conn, cycle_code)
        _expect_phase(cycle, {"pickup_verified"})

        tractor_plate = cycle["pickup_tractor_plate"] or cycle["tractor_plate"]
        # 先校验牵引车是否已挂别的挂车
        existing_t = self.conn.execute(
            "SELECT trailer_plate FROM couplings WHERE tractor_plate=? AND deleted=0",
            (tractor_plate,),
        ).fetchone()
        if existing_t:
            raise ServiceError(
                f"牵引车 {tractor_plate} 已挂 {existing_t['trailer_plate']}，不能重复挂接",
                "coupling_busy",
            )
        existing_tr = self.conn.execute(
            "SELECT tractor_plate FROM couplings WHERE trailer_plate=? AND deleted=0",
            (cycle["trailer_plate"],),
        ).fetchone()
        if existing_tr:
            raise ServiceError(
                f"挂车 {cycle['trailer_plate']} 已与 {existing_tr['tractor_plate']} 存在有效挂接",
                "coupling_exists",
            )
        try:
            self.conn.execute(
                "INSERT INTO couplings(tractor_plate,trailer_plate,cycle_id,coupled_at,by_user) "
                "VALUES(?,?,?,?,?)",
                (tractor_plate, cycle["trailer_plate"], cycle["id"], self._ts(), u["display_name"]),
            )
        except sqlite3.IntegrityError as e:
            raise ServiceError(
                f"挂接唯一性冲突：{cycle['trailer_plate']} 已存在有效挂接",
                "coupling_race",
            ) from e

        # 释放车位
        self.conn.execute(
            "UPDATE stall_occupancy SET released=1 WHERE stall_id=? AND released=0",
            (cycle["stall_id"],),
        )
        self.conn.execute("UPDATE stalls SET status='free' WHERE id=?", (cycle["stall_id"],))
        self.conn.execute(
            "UPDATE cycles SET phase='exit_pending', hitched_at=? WHERE id=?",
            (self._ts(), cycle["id"]),
        )
        log_event(self.conn, "hitch", u["display_name"],
                  f"{tractor_plate} 挂接 {cycle['trailer_plate']}，车位释放，进入待出园",
                  cycle_code, commit=False)
        self.conn.commit()
        return {"hitched": True, "tractor": tractor_plate}

    # ============================================================ 10. 无法提挂

    def cannot_pick(self, username: str, cycle_code: str, reason: str) -> dict:
        u = _require(self.conn, username, "cannot_pick")
        cycle = _get_cycle(self.conn, cycle_code)
        _expect_phase(cycle, {"pickup_pending", "pickup_verified"})
        self._close_wait(cycle["id"], "pickup")
        raise_exception(self.conn, "cannot_pick", u["display_name"],
                        f"{cycle['trailer_plate']} 无法完成提挂：{reason}",
                        cycle_code, commit=False)
        # 回退到待提挂，等待重新安排
        self.conn.execute(
            "UPDATE cycles SET phase='pickup_pending', driver_confirmed=0, seal_confirmed=0 WHERE id=?",
            (cycle["id"],),
        )
        log_event(self.conn, "cannot_pick", u["display_name"], reason, cycle_code, commit=False)
        self.conn.commit()
        return {"noted": True}

    # ============================================================ 11. 异常放行审批

    def request_abnormal_exit(self, username: str, cycle_code: str, note: str) -> int:
        u = _require(self.conn, username, "request_abnormal_exit")
        cycle = _get_cycle(self.conn, cycle_code)
        eid = raise_exception(
            self.conn, "abnormal_exit", u["display_name"],
            f"{cycle['trailer_plate']} 申请异常出园：{note}", cycle_code, commit=False,
        )
        self.conn.commit()
        return eid

    def decide_exception(self, username: str, exception_id: int,
                         approve: bool, note: str = "") -> dict:
        """仅值班主管可批准异常放行。"""
        u = _require(self.conn, username, "decide_exception")
        ex = self.conn.execute("SELECT * FROM exceptions WHERE id=?", (exception_id,)).fetchone()
        if ex is None:
            raise ServiceError(f"异常单不存在：{exception_id}", "not_found")
        if ex["status"] != "open":
            raise ServiceError("异常单已处理", "already_decided")

        status = "approved" if approve else "rejected"
        self.conn.execute(
            "UPDATE exceptions SET status=?, decided_by=?, decided_at=?, decision_note=? WHERE id=?",
            (status, u["display_name"], self._ts(), note, exception_id),
        )
        if approve and ex["kind"] == "abnormal_exit" and ex["cycle_code"]:
            self.conn.execute(
                "UPDATE cycles SET abnormal_exit=1, exit_approved=1 WHERE code=?",
                (ex["cycle_code"],),
            )
        log_event(self.conn, "exception_decided", u["display_name"],
                  f"异常单 {exception_id} {'批准' if approve else '驳回'}：{note}",
                  ex["cycle_code"], commit=False)
        self.conn.commit()
        return {"status": status}

    # ============================================================ 12. 出园确认（关闭周转）

    def exit_confirm(self, username: str, cycle_code: str) -> dict:
        u = _require(self.conn, username, "exit_confirm")
        cycle = _get_cycle(self.conn, cycle_code)

        if cycle["phase"] == "exit_pending":
            abnormal = bool(cycle["abnormal_exit"])
        elif cycle["abnormal_exit"] and cycle["exit_approved"] and cycle["phase"] != "closed":
            # 主管批准的异常车辆可在任意在园阶段放行
            abnormal = True
        else:
            raise ServiceError(
                "未挂接完成且无主管批准的异常放行，不能出园", "exit_not_allowed")

        if abnormal and not cycle["exit_approved"]:
            raise ServiceError("异常车辆出园需值班主管批准", "approval_required")

        self.gate.raise_barrier("G2", cycle["trailer_plate"],
                                "异常放行" if abnormal else "正常出园")
        self.conn.execute(
            "UPDATE cycles SET phase='closed', closed_at=?, exit_check_at=? WHERE id=?",
            (self._ts(), self._ts(), cycle["id"]),
        )
        if cycle["appointment_code"]:
            self.conn.execute(
                "UPDATE appointments SET status='done' WHERE code=?",
                (cycle["appointment_code"],),
            )
        log_event(self.conn, "exit", u["display_name"],
                  f"{cycle['trailer_plate']}{'（异常放行）' if abnormal else ''}出园，本次周转关闭",
                  cycle_code, commit=False)
        self.conn.commit()
        return {"closed": True, "abnormal": abnormal}

    # ============================================================ 超时扫描（进程内任务）

    def scan_timeouts(self) -> list[dict]:
        """后台定时调用：对超时仍在等待的工单打超时事件（只提醒一次）。"""
        rows = self.conn.execute(
            "SELECT wt.*, c.code FROM wait_tracking wt JOIN cycles c ON c.id=wt.cycle_id "
            "WHERE wt.done=0 AND wt.alerted=0"
        ).fetchall()
        alerts = []
        for r in rows:
            started = datetime.strptime(r["started_at"], "%Y-%m-%d %H:%M:%S")
            elapsed = (self.clock() - started).total_seconds()
            if elapsed >= r["threshold_s"]:
                kind_cn = "待卸挂排队" if r["kind"] == "queue" else "提挂等待"
                mins = int(elapsed // 60)
                log_event(self.conn, "wait_timeout", "system",
                          f"{r['code']} {kind_cn}超时（已 {mins} 分钟）", r["code"], commit=False)
                self.conn.execute("UPDATE wait_tracking SET alerted=1 WHERE id=?", (r["id"],))
                alerts.append({"cycle": r["code"], "kind": r["kind"], "minutes": mins})
        if alerts:
            self.conn.commit()
        return alerts
