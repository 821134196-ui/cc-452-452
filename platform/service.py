"""核心业务服务：签到、甩挂交接、提挂核验、异常放行与出园关单。

交接完成四要件（缺一不可）：挂车停妥(入位) → 解绑登记 → 轮挡确认 → 钥匙入柜。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import timedelta
from typing import Any, Optional

from . import db as dbm
from .adapters import CabinetAdapter, GateAdapter
from .clock import Clock, iso, parse
from .permissions import ServiceError, require

# 队列等待超时阈值（分钟）
WAIT_TIMEOUT_MIN = 30
# 预约迟到宽限（分钟）
LATE_GRACE_MIN = 15

STAFF_COLS = ("id", "login", "name", "role")


def _photo_list(photos: Any) -> list[dict[str, str]]:
    if not photos:
        return []
    if isinstance(photos, str):
        photos = json.loads(photos)
    return [{"pos": p.get("pos", ""), "path": p.get("path", ""),
             "note": p.get("note", "")} for p in photos]


class YardService:
    """业务服务。

    连接采用线程本地 SQLite 连接（WAL 模式）：HTTP 工作线程各自持有连接，
    车位抢占等竞争由单条条件 UPDATE 在数据库层原子裁决。
    """

    def __init__(self, db_path: str, clock: Clock,
                 gate: Optional[GateAdapter] = None,
                 cabinet: Optional[CabinetAdapter] = None):
        self.db_path = db_path
        self.clock = clock
        self.gate = gate or GateAdapter()
        self.cabinet = cabinet or CabinetAdapter()
        self._local = threading.local()

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = dbm.connect(self.db_path)
            self._local.conn = c
        return c

    # ------------------------------------------------------------------ 人员
    def staff_by_login(self, login: str) -> Optional[sqlite3.Row]:
        return dbm.row(self.conn, "SELECT * FROM staff WHERE login=?", (login,))

    def _actor(self, actor_id: int) -> sqlite3.Row:
        r = dbm.row(self.conn, "SELECT * FROM staff WHERE id=?", (actor_id,))
        if not r:
            raise ServiceError("no_staff", "操作人员不存在", http=400)
        return r

    def _do(self, actor_id: int, action: str) -> sqlite3.Row:
        actor = self._actor(actor_id)
        require(actor["role"], action)
        return actor

    def _turn(self, turn_id: int) -> sqlite3.Row:
        r = dbm.row(self.conn, "SELECT * FROM turns WHERE id=?", (turn_id,))
        if not r:
            raise ServiceError("no_turn", "周转单不存在", http=404)
        return r

    # ------------------------------------------------------------- 车辆到园
    def arrive(self, actor_id: int, appointment_code: str,
               actual_tractor_id: Optional[int] = None,
               actual_driver_id: Optional[int] = None) -> dict[str, Any]:
        """门岗签到：校验预约 -> 道闸抬起 -> 进待卸挂队列。迟到/替换单独记录。"""
        actor = self._do(actor_id, "arrive")
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            appt = dbm.row(conn, "SELECT * FROM appointments WHERE code=?",
                           (appointment_code,))
            if not appt:
                raise ServiceError("no_appointment", "预约不存在", http=404)
            if appt["status"] in ("done", "cancelled"):
                raise ServiceError("appointment_closed", "该预约已结束或取消")
            existing = dbm.row(
                conn, "SELECT id FROM turns WHERE appointment_id=? AND status NOT IN "
                      "('closed','exited')", (appt["id"],))
            if existing:
                raise ServiceError("already_arrived", "该预约已有进行中的周转单")

            now = self.clock.now()
            tractor_id = actual_tractor_id or appt["tractor_id"]
            driver_id = actual_driver_id or appt["driver_id"]
            tractor = dbm.row(conn, "SELECT * FROM tractors WHERE id=?", (tractor_id,))
            driver = dbm.row(conn, "SELECT * FROM drivers WHERE id=?", (driver_id,))
            trailer = dbm.row(conn, "SELECT * FROM trailers WHERE id=?",
                              (appt["trailer_id"],))
            if not tractor or not driver or not trailer:
                raise ServiceError("bad_ref", "牵引车/司机/挂车资料缺失", http=400)

            late = now > parse(appt["planned_arrival"]) + timedelta(minutes=LATE_GRACE_MIN)
            conn.execute("UPDATE appointments SET status=? WHERE id=?",
                         ("late" if late else "arrived", appt["id"]))

            cur = conn.execute(
                """INSERT INTO turns(code,appointment_id,trailer_id,in_tractor_id,
                       in_driver_id,out_tractor_id,out_driver_id,status,
                       planned_arrival,arrived_at,queued_at)
                   VALUES(?,?,?,?,?,?,?,'queued',?,?,?)""",
                (f"T-{appt['code']}", appt["id"], trailer["id"], tractor["id"],
                 driver["id"], tractor["id"], driver["id"],
                 appt["planned_arrival"], iso(now), iso(now)))
            turn_id = cur.lastrowid

            # 到园时牵引车与挂车为有效挂接关系（受唯一索引保护）
            try:
                conn.execute(
                    """INSERT INTO hitches(turn_id,tractor_id,trailer_id,active,
                           linked_at,created_by) VALUES(?,?,?,1,?,?)""",
                    (turn_id, tractor["id"], trailer["id"], iso(now), actor["id"]))
            except sqlite3.IntegrityError:
                raise ServiceError("hitch_conflict",
                                   f"挂车 {trailer['plate_no']} 已存在有效挂接关系，"
                                   "一辆挂车同一时刻只能挂接一个牵引车")

            conn.execute("UPDATE tractors SET status='inbound' WHERE id=?",
                         (tractor["id"],))
            conn.execute("UPDATE trailers SET status='queued' WHERE id=?",
                         (trailer["id"],))

            gate_res = self.gate.open_barrier(
                f"T-{appt['code']}", tractor["plate_no"], "in")
            dbm.insert_event(conn, self.clock, "arrival", turn_id, actor["id"],
                             "info", {"gate": gate_res.__dict__,
                                      "tractor": tractor["plate_no"],
                                      "trailer": trailer["plate_no"]})
            if late:
                dbm.insert_event(conn, self.clock, "appointment_late", turn_id,
                                 actor["id"], "warn",
                                 {"planned": appt["planned_arrival"],
                                  "arrived": iso(now),
                                  "grace_min": LATE_GRACE_MIN})
            if actual_tractor_id and actual_tractor_id != appt["tractor_id"]:
                planned = dbm.row(conn, "SELECT plate_no FROM tractors WHERE id=?",
                                  (appt["tractor_id"],))
                dbm.insert_event(conn, self.clock, "tractor_substituted", turn_id,
                                 actor["id"], "warn",
                                 {"planned_plate": planned["plate_no"] if planned else None,
                                  "actual_plate": tractor["plate_no"]})
            if actual_driver_id and actual_driver_id != appt["driver_id"]:
                planned_d = dbm.row(conn, "SELECT name FROM drivers WHERE id=?",
                                    (appt["driver_id"],))
                dbm.insert_event(conn, self.clock, "driver_substituted", turn_id,
                                 actor["id"], "warn",
                                 {"planned_driver": planned_d["name"] if planned_d else None,
                                  "actual_driver": driver["name"]})
            conn.commit()
            return {"turn_id": turn_id, "code": f"T-{appt['code']}",
                    "late": late, "gate": gate_res.__dict__}
        except Exception:
            conn.rollback()
            raise

    # ------------------------------------------------------------- 调度入位
    def assign_slot(self, actor_id: int, turn_id: int, slot_code: str) -> dict[str, Any]:
        """调度员分配车位。条件 UPDATE 原子抢占：两个调度员仅首个成功。"""
        actor = self._do(actor_id, "assign_slot")
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            turn = self._turn(turn_id)
            if turn["status"] not in ("queued", "slotted"):
                raise ServiceError("bad_state",
                                   f"周转单状态 {turn['status']} 不可分配车位")
            slot = dbm.row(conn, "SELECT * FROM parking_slots WHERE code=?",
                           (slot_code,))
            if not slot:
                raise ServiceError("no_slot", "车位不存在", http=404)
            # 已停在该位：幂等返回
            if turn["slot_id"] == slot["id"]:
                conn.commit()
                return {"slot": slot_code, "wait_minutes": turn["wait_minutes"],
                        "noop": True}
            if slot["status"] == "locked":
                dbm.insert_event(conn, self.clock, "slot_locked", turn_id,
                                 actor["id"], "warn", {"slot": slot_code})
                conn.commit()
                raise ServiceError("slot_locked", f"车位 {slot_code} 已锁定（维保/预留）")

            # 原子抢占：仅 free 可改
            cur = conn.execute(
                "UPDATE parking_slots SET status='occupied',turn_id=? "
                "WHERE code=? AND status='free'", (turn_id, slot_code))
            if cur.rowcount == 0:
                holder = dbm.row(
                    conn, "SELECT t.code FROM parking_slots s LEFT JOIN turns t "
                          "ON t.id=s.turn_id WHERE s.code=?", (slot_code,))
                dbm.insert_event(conn, self.clock, "slot_occupied", turn_id,
                                 actor["id"], "warn",
                                 {"slot": slot_code,
                                  "holder": holder["code"] if holder else None})
                # 预警事件单独留痕后再拒绝
                conn.commit()
                raise ServiceError("slot_taken",
                                   f"车位 {slot_code} 已被占用"
                                   + (f"（{holder['code']}）" if holder else ""))

            # 释放此前车位（允许改派）
            if turn["slot_id"]:
                conn.execute(
                    "UPDATE parking_slots SET status='free',turn_id=NULL WHERE id=?",
                    (turn["slot_id"],))
            now = self.clock.now()
            queued = parse(turn["queued_at"])
            wait_min = int((now - queued).total_seconds() // 60) if queued else 0
            conn.execute(
                "UPDATE turns SET status='slotted',slot_id=?,slotted_at=?,"
                "wait_minutes=? WHERE id=?",
                (slot["id"], iso(now), wait_min, turn_id))
            conn.execute("UPDATE trailers SET status='slotted' WHERE id=?",
                         (turn["trailer_id"],))
            dbm.insert_event(conn, self.clock, "slot_assigned", turn_id,
                             actor["id"], "info",
                             {"slot": slot_code, "wait_minutes": wait_min})
            conn.commit()
            return {"slot": slot_code, "wait_minutes": wait_min}
        except Exception:
            conn.rollback()
            raise

    # ------------------------------------------------------------- 甩挂三步
    def _require_slotted(self, turn: sqlite3.Row) -> None:
        if not turn["slotted_at"]:
            raise ServiceError("not_slotted", "挂车尚未停妥入位，不能执行该步骤")

    def unhitch(self, actor_id: int, turn_id: int) -> dict[str, Any]:
        """登记牵引车与挂车解绑。"""
        actor = self._do(actor_id, "unhitch")
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            turn = self._turn(turn_id)
            self._require_slotted(turn)
            if turn["unhitched_at"]:
                raise ServiceError("already_done", "解绑已登记，请勿重复操作")
            now = iso(self.clock.now())
            conn.execute(
                "UPDATE hitches SET active=0,unlinked_at=? WHERE trailer_id=? "
                "AND active=1 AND turn_id=?", (now, turn["trailer_id"], turn_id))
            conn.execute("UPDATE turns SET unhitched_at=?,unhitched_by=? WHERE id=?",
                         (now, actor["id"], turn_id))
            conn.execute("UPDATE tractors SET status='dropped' WHERE id=?",
                         (turn["in_tractor_id"],))
            conn.execute("UPDATE trailers SET status='dropped' WHERE id=?",
                         (turn["trailer_id"],))
            dbm.insert_event(conn, self.clock, "unhitched", turn_id, actor["id"],
                             "info", {})
            self._maybe_complete_handover(conn, turn_id)
            conn.commit()
            return {"unhitched_at": now}
        except Exception:
            conn.rollback()
            raise

    def chock(self, actor_id: int, turn_id: int) -> dict[str, Any]:
        """确认轮挡放置。"""
        actor = self._do(actor_id, "chock")
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            turn = self._turn(turn_id)
            self._require_slotted(turn)
            if turn["chocked_at"]:
                raise ServiceError("already_done", "轮挡已确认，请勿重复操作")
            now = iso(self.clock.now())
            conn.execute("UPDATE turns SET chocked_at=?,chocked_by=? WHERE id=?",
                         (now, actor["id"], turn_id))
            dbm.insert_event(conn, self.clock, "chocked", turn_id, actor["id"],
                             "info", {})
            self._maybe_complete_handover(conn, turn_id)
            conn.commit()
            return {"chocked_at": now}
        except Exception:
            conn.rollback()
            raise

    def deposit_key(self, actor_id: int, turn_id: int,
                    box_id: str, cell_no: str) -> dict[str, Any]:
        """钥匙入柜；错格单独记录且不构成交接完成。"""
        actor = self._do(actor_id, "deposit_key")
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            turn = self._turn(turn_id)
            self._require_slotted(turn)
            if turn["key_deposited_at"]:
                raise ServiceError("already_done", "钥匙已入柜，请勿重复存入")
            trailer = dbm.row(conn, "SELECT * FROM trailers WHERE id=?",
                              (turn["trailer_id"],))
            tag = f"KEY-{trailer['plate_no']}-{turn_id:04d}"
            res = self.cabinet.put(tag, box_id, cell_no)
            now = iso(self.clock.now())
            prior = dbm.row(
                conn, "SELECT * FROM keys WHERE tag=? AND status='wrong_cell'", (tag,))
            if not res.ok:
                if prior:
                    conn.execute(
                        "UPDATE keys SET box_id=?,cell_no=?,note=?,deposited_at=? "
                        "WHERE id=?", (box_id, cell_no, res.message, now, prior["id"]))
                else:
                    conn.execute(
                        """INSERT INTO keys(tag,turn_id,trailer_id,box_id,cell_no,status,
                               deposited_at,deposited_by,note)
                           VALUES(?,?,?,?,?,'wrong_cell',?,?,?)""",
                        (tag, turn_id, trailer["id"], box_id, cell_no, now,
                         actor["id"], res.message))
                dbm.insert_event(conn, self.clock, "key_wrong_cell", turn_id,
                                 actor["id"], "exception",
                                 {"box": box_id, "cell": cell_no,
                                  "occupied_by": res.occupied_by,
                                  "message": res.message})
                conn.commit()
                raise ServiceError("key_wrong_cell", res.message)
            if prior:
                conn.execute(
                    "UPDATE keys SET box_id=?,cell_no=?,status='in_cabinet',"
                    "note=NULL,deposited_at=?,deposited_by=? WHERE id=?",
                    (box_id, cell_no, now, actor["id"], prior["id"]))
            else:
                conn.execute(
                    """INSERT INTO keys(tag,turn_id,trailer_id,box_id,cell_no,status,
                           deposited_at,deposited_by)
                       VALUES(?,?,?,?,?,'in_cabinet',?,?)""",
                    (tag, turn_id, trailer["id"], box_id, cell_no, now, actor["id"]))
            conn.execute("UPDATE turns SET key_deposited_at=?,key_deposited_by=? "
                         "WHERE id=?", (now, actor["id"], turn_id))
            dbm.insert_event(conn, self.clock, "key_deposited", turn_id,
                             actor["id"], "info",
                             {"box": box_id, "cell": cell_no, "tag": tag})
            self._maybe_complete_handover(conn, turn_id)
            conn.commit()
            return {"deposited_at": now, "tag": tag, "cabinet": res.__dict__}
        except ServiceError:
            raise
        except Exception:
            conn.rollback()
            raise

    def _maybe_complete_handover(self, conn: sqlite3.Connection, turn_id: int) -> None:
        """四要件齐备：停妥 + 解绑 + 轮挡 + 钥匙入柜。"""
        turn = dbm.row(conn, "SELECT * FROM turns WHERE id=?", (turn_id,))
        if (turn["status"] == "slotted" and turn["slotted_at"] and
                turn["unhitched_at"] and turn["chocked_at"] and
                turn["key_deposited_at"]):
            conn.execute("UPDATE turns SET status='handed_over' WHERE id=?",
                         (turn_id,))
            dbm.insert_event(conn, self.clock, "handover_completed", turn_id, None,
                             "info", {"at": iso(self.clock.now())})

    # ------------------------------------------------------------- 提挂核验
    def start_pickup(self, actor_id: int, turn_id: int,
                     out_tractor_id: int, out_driver_id: int) -> dict[str, Any]:
        """调度员发起提挂，指定提挂牵引车与司机（司机替换单独记录）。"""
        actor = self._do(actor_id, "start_pickup")
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            turn = self._turn(turn_id)
            if turn["status"] not in ("handed_over", "pickup_started"):
                raise ServiceError("bad_state",
                                   "挂车尚未完成交接，不能发起提挂")
            tractor = dbm.row(conn, "SELECT * FROM tractors WHERE id=?",
                              (out_tractor_id,))
            driver = dbm.row(conn, "SELECT * FROM drivers WHERE id=?",
                             (out_driver_id,))
            if not tractor or not driver:
                raise ServiceError("bad_ref", "牵引车或司机资料不存在", http=400)
            # 挂接唯一性预检（唯一索引为最终防线）
            live = dbm.row(conn, "SELECT id FROM hitches WHERE trailer_id=? AND active=1",
                           (turn["trailer_id"],))
            if live:
                raise ServiceError("hitch_conflict",
                                   "该挂车存在有效挂接关系，无法再次提挂")
            conn.execute(
                "UPDATE turns SET status='pickup_started',out_tractor_id=?,"
                "out_driver_id=?,pickup_started_at=?,pickup_started_by=? WHERE id=?",
                (out_tractor_id, driver["id"], iso(self.clock.now()),
                 actor["id"], turn_id))
            dbm.insert_event(conn, self.clock, "pickup_started", turn_id,
                             actor["id"], "info",
                             {"tractor": tractor["plate_no"],
                              "driver": driver["name"]})
            if out_driver_id != turn["in_driver_id"]:
                prev = dbm.row(conn, "SELECT name FROM drivers WHERE id=?",
                               (turn["in_driver_id"],))
                dbm.insert_event(conn, self.clock, "driver_substituted", turn_id,
                                 actor["id"], "warn",
                                 {"planned_driver": prev["name"] if prev else None,
                                  "actual_driver": driver["name"],
                                  "phase": "pickup"})
            conn.commit()
            return {"status": "pickup_started"}
        except Exception:
            conn.rollback()
            raise

    def verify_seal(self, actor_id: int, turn_id: int, seal_no: str) -> dict[str, Any]:
        """查验员核对铅封号；不符即挂起，等待主管批准。"""
        actor = self._do(actor_id, "verify_seal")
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            turn = self._turn(turn_id)
            if turn["status"] not in ("pickup_started", "held"):
                raise ServiceError("bad_state", "请先发起提挂再核对铅封")
            trailer = dbm.row(conn, "SELECT * FROM trailers WHERE id=?",
                              (turn["trailer_id"],))
            now = iso(self.clock.now())
            ok = (seal_no or "").strip() == (trailer["seal_no"] or "").strip()
            if ok:
                conn.execute(
                    "UPDATE turns SET seal_verified_at=?,seal_verified_by=? WHERE id=?",
                    (now, actor["id"], turn_id))
                if turn["status"] == "held":
                    conn.execute("UPDATE turns SET status='pickup_started' WHERE id=?",
                                 (turn_id,))
                dbm.insert_event(conn, self.clock, "seal_verified", turn_id,
                                 actor["id"], "info", {"seal": seal_no})
            else:
                conn.execute(
                    "UPDATE turns SET status='held',hold_reason='seal_mismatch' "
                    "WHERE id=?", (turn_id,))
                dbm.insert_event(conn, self.clock, "seal_mismatch", turn_id,
                                 actor["id"], "exception",
                                 {"expected": trailer["seal_no"],
                                  "actual": seal_no})
            conn.commit()
            return {"match": ok, "expected": trailer["seal_no"], "actual": seal_no}
        except Exception:
            conn.rollback()
            raise

    def inspect_appearance(self, actor_id: int, turn_id: int, phase: str,
                           result: str, damage_desc: str = "",
                           photos: Optional[list[dict[str, str]]] = None) -> dict[str, Any]:
        """外观检查（到园/提挂各一次），照片按方位存本地并登记。"""
        actor = self._do(actor_id, "inspect_appearance")
        if phase not in ("arrival", "departure"):
            raise ServiceError("bad_phase", "检查阶段只能是 arrival/departure", http=400)
        if result not in ("pass", "damage"):
            raise ServiceError("bad_result", "检查结果只能是 pass/damage", http=400)
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            turn = self._turn(turn_id)
            photos = photos or []
            now = iso(self.clock.now())
            conn.execute(
                """INSERT INTO inspections(turn_id,phase,result,damage_desc,photos,
                       inspector_id,checked_at) VALUES(?,?,?,?,?,?,?)""",
                (turn_id, phase, result, damage_desc,
                 json.dumps(photos, ensure_ascii=False), actor["id"], now))
            if phase == "arrival":
                conn.execute("UPDATE turns SET appearance_in_at=?,"
                             "appearance_in_by=? WHERE id=?",
                             (now, actor["id"], turn_id))
                if result == "damage":
                    dbm.insert_event(conn, self.clock, "damage_found", turn_id,
                                     actor["id"], "warn",
                                     {"phase": "arrival", "desc": damage_desc,
                                      "photos": [p["path"] for p in photos]})
            else:
                conn.execute("UPDATE turns SET appearance_out_at=?,"
                             "appearance_out_by=? WHERE id=?",
                             (now, actor["id"], turn_id))
                if result == "damage":
                    conn.execute(
                        "UPDATE turns SET status='held',hold_reason='appearance_damage' "
                        "WHERE id=?", (turn_id,))
                    dbm.insert_event(conn, self.clock, "damage_found", turn_id,
                                     actor["id"], "exception",
                                     {"phase": "departure", "desc": damage_desc,
                                      "photos": [p["path"] for p in photos]})
                else:
                    dbm.insert_event(conn, self.clock, "appearance_passed", turn_id,
                                     actor["id"], "info", {"phase": "departure"})
                    if turn["status"] == "held" and turn["hold_reason"] is None:
                        conn.execute("UPDATE turns SET status='pickup_started' WHERE id=?",
                                     (turn_id,))
            conn.commit()
            return {"result": result, "photos": photos}
        except Exception:
            conn.rollback()
            raise

    def cannot_pickup(self, actor_id: int, turn_id: int, reason: str) -> dict[str, Any]:
        """无法提挂（故障/缺件/司机放弃等）单独记录。"""
        actor = self._do(actor_id, "cannot_pickup")
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            turn = self._turn(turn_id)
            if turn["status"] not in ("handed_over", "pickup_started", "held"):
                raise ServiceError("bad_state", "当前状态不允许登记无法提挂")
            conn.execute("UPDATE turns SET status='held',hold_reason='cannot_pickup' "
                         "WHERE id=?", (turn_id,))
            dbm.insert_event(conn, self.clock, "cannot_pickup", turn_id,
                             actor["id"], "exception", {"reason": reason})
            conn.commit()
            return {"recorded": True}
        except Exception:
            conn.rollback()
            raise

    def hitch(self, actor_id: int, turn_id: int) -> dict[str, Any]:
        """提挂挂接：铅封与外观必须通过（或异常已批准）。唯一索引兜底。"""
        actor = self._do(actor_id, "hitch")
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            turn = self._turn(turn_id)
            if turn["status"] != "pickup_started":
                raise ServiceError("bad_state",
                                   "挂接受阻：请先完成发起提挂/解除挂起状态")
            if not turn["seal_verified_at"]:
                raise ServiceError("seal_unchecked", "铅封尚未核对通过")
            dep = dbm.row(
                conn, "SELECT result FROM inspections WHERE turn_id=? AND phase='departure' "
                      "ORDER BY id DESC LIMIT 1", (turn_id,))
            if not dep:
                raise ServiceError("appearance_unchecked", "提挂外观检查尚未完成")
            if dep["result"] != "pass" and not turn["abnormal"]:
                raise ServiceError("damage_open", "外观损伤未处理，禁止挂接")

            now = self.clock.now()
            try:
                conn.execute(
                    """INSERT INTO hitches(turn_id,tractor_id,trailer_id,active,
                           linked_at,created_by) VALUES(?,?,?,1,?,?)""",
                    (turn_id, turn["out_tractor_id"], turn["trailer_id"],
                     iso(now), actor["id"]))
            except sqlite3.IntegrityError:
                raise ServiceError("hitch_conflict",
                                   "挂车已存在有效挂接关系，挂接失败")
            conn.execute(
                "UPDATE turns SET hitched_at=?,hitched_by=? WHERE id=?",
                (iso(now), actor["id"], turn_id))
            conn.execute("UPDATE tractors SET status='hitched' WHERE id=?",
                         (turn["out_tractor_id"],))
            conn.execute("UPDATE trailers SET status='hitched' WHERE id=?",
                         (turn["trailer_id"],))
            dbm.insert_event(conn, self.clock, "hitched", turn_id, actor["id"],
                             "info", {})
            conn.commit()
            return {"hitched_at": iso(now)}
        except Exception:
            conn.rollback()
            raise

    def release_key(self, actor_id: int, turn_id: int,
                    cell_no: Optional[str] = None) -> dict[str, Any]:
        """钥匙员按标签从登记格口取钥放行；错格单独告警。"""
        actor = self._do(actor_id, "release_key")
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            turn = self._turn(turn_id)
            if not turn["hitched_at"]:
                raise ServiceError("not_hitched", "尚未完成挂接，不能发放钥匙")
            keyrow = dbm.row(
                conn, "SELECT * FROM keys WHERE turn_id=? AND status IN "
                      "('in_cabinet','wrong_cell') ORDER BY id DESC LIMIT 1",
                (turn_id,))
            if not keyrow:
                raise ServiceError("no_key", "柜内无该挂车钥匙记录")
            target_cell = cell_no or keyrow["cell_no"]
            res = self.cabinet.fetch(keyrow["tag"], keyrow["box_id"], target_cell)
            now = iso(self.clock.now())
            if not res.ok:
                conn.execute("UPDATE keys SET status='wrong_cell',note=? WHERE id=?",
                             (res.message, keyrow["id"]))
                dbm.insert_event(conn, self.clock, "key_wrong_cell", turn_id,
                                 actor["id"], "exception",
                                 {"box": keyrow["box_id"], "cell": target_cell,
                                  "tag": keyrow["tag"], "message": res.message})
                conn.commit()
                raise ServiceError("key_wrong_cell", res.message)
            conn.execute(
                "UPDATE keys SET status='checked_out',released_at=?,released_by=? "
                "WHERE id=?", (now, actor["id"], keyrow["id"]))
            conn.execute("UPDATE turns SET key_released_at=?,key_released_by=? "
                         "WHERE id=?", (now, actor["id"], turn_id))
            dbm.insert_event(conn, self.clock, "key_released", turn_id,
                             actor["id"], "info", {"tag": keyrow["tag"]})
            if turn["hitched_at"]:
                conn.execute("UPDATE turns SET status='ready_to_exit' WHERE id=?",
                             (turn_id,))
            conn.commit()
            return {"released_at": now, "cabinet": res.__dict__}
        except ServiceError:
            raise
        except Exception:
            conn.rollback()
            raise

    # ------------------------------------------------------------- 异常审批
    def request_approval(self, actor_id: int, turn_id: int,
                         reason_code: str, detail: str = "") -> dict[str, Any]:
        """一线岗位发起异常放行申请。"""
        actor = self._actor(actor_id)
        if actor["role"] not in ("gate", "dispatcher", "inspector", "keyclerk"):
            raise ServiceError("forbidden", "该角色无需发起异常申请", http=403)
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            turn = self._turn(turn_id)
            pend = dbm.row(conn,
                           "SELECT id FROM approvals WHERE turn_id=? AND status='pending'",
                           (turn_id,))
            if pend:
                raise ServiceError("approval_pending", "已有待审批申请，请勿重复提交")
            cur = conn.execute(
                """INSERT INTO approvals(turn_id,reason_code,detail,requested_by,
                       status,created_at) VALUES(?,?,?,?,'pending',?)""",
                (turn_id, reason_code, detail, actor["id"], iso(self.clock.now())))
            dbm.insert_event(conn, self.clock, "approval_requested", turn_id,
                             actor["id"], "warn",
                             {"reason": reason_code, "detail": detail})
            conn.commit()
            return {"approval_id": cur.lastrowid, "status": "pending"}
        except Exception:
            conn.rollback()
            raise

    def decide_approval(self, actor_id: int, approval_id: int,
                        approve: bool) -> dict[str, Any]:
        """值班主管批准/驳回；批准后解除挂起并标记异常放行。"""
        actor = self._do(actor_id, "decide_approval")
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            ap = dbm.row(conn, "SELECT * FROM approvals WHERE id=?", (approval_id,))
            if not ap:
                raise ServiceError("no_approval", "审批单不存在", http=404)
            if ap["status"] != "pending":
                raise ServiceError("approval_decided", "该审批已出结论")
            now = iso(self.clock.now())
            result = "approved" if approve else "rejected"
            conn.execute(
                "UPDATE approvals SET status=?,decided_by=?,decided_at=? WHERE id=?",
                (result, actor["id"], now, approval_id))
            turn = self._turn(ap["turn_id"])
            if approve:
                conn.execute(
                    "UPDATE turns SET abnormal=1,status='pickup_started',"
                    "hold_reason=NULL WHERE id=?", (turn["id"],))
                dbm.insert_event(conn, self.clock, "approval_approved",
                                 turn["id"], actor["id"], "critical",
                                 {"reason": ap["reason_code"],
                                  "detail": ap["detail"]})
            else:
                dbm.insert_event(conn, self.clock, "approval_rejected",
                                 turn["id"], actor["id"], "warn",
                                 {"reason": ap["reason_code"]})
            conn.commit()
            return {"approval_id": approval_id, "status": result}
        except Exception:
            conn.rollback()
            raise

    # ------------------------------------------------------------- 出园关单
    def exit_gate(self, actor_id: int, turn_id: int) -> dict[str, Any]:
        """出园核验：异常车辆须有主管批准；道闸抬起后关闭本次周转。"""
        actor = self._do(actor_id, "exit_gate")
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            turn = self._turn(turn_id)
            if turn["status"] not in ("ready_to_exit", "held"):
                raise ServiceError("bad_state", "车辆未达到出园条件")
            if turn["status"] == "held":
                raise ServiceError("bad_state", "车辆仍处于挂起状态，不能放行")
            if not turn["hitched_at"]:
                raise ServiceError("not_hitched", "挂车尚未挂接，不能出园")
            if not turn["key_released_at"]:
                raise ServiceError("key_held", "钥匙未发放，不能出园")
            if turn["abnormal"]:
                ap = dbm.row(
                    conn, "SELECT * FROM approvals WHERE turn_id=? AND status='approved' "
                          "ORDER BY id DESC LIMIT 1", (turn_id,))
                if not ap:
                    raise ServiceError("approval_required",
                                       "异常车辆须经值班主管批准后方可放行")

            tractor = dbm.row(conn, "SELECT * FROM tractors WHERE id=?",
                              (turn["out_tractor_id"],))
            gate_res = self.gate.open_barrier(turn["code"],
                                              tractor["plate_no"] if tractor else "?",
                                              "out")
            if not gate_res.ok:
                dbm.insert_event(conn, self.clock, "gate_fault", turn_id,
                                 actor["id"], "exception", {"gate": gate_res.__dict__})
                conn.commit()
                raise ServiceError("gate_fault", gate_res.message)

            now = iso(self.clock.now())
            final_status = "abnormal_release" if turn["abnormal"] else "exited"
            conn.execute(
                "UPDATE turns SET status=?,exited_at=?,exited_by=? WHERE id=?",
                (final_status, now, actor["id"], turn_id))
            dbm.insert_event(conn, self.clock, "exit_gate", turn_id, actor["id"],
                             "critical" if turn["abnormal"] else "info",
                             {"gate": gate_res.__dict__, "abnormal": bool(turn["abnormal"])})

            # 关闭本次周转：解除挂接、释放车位、车辆离场
            conn.execute(
                "UPDATE hitches SET active=0,unlinked_at=? WHERE trailer_id=? AND active=1",
                (now, turn["trailer_id"]))
            conn.execute(
                "UPDATE parking_slots SET status='free',turn_id=NULL WHERE turn_id=?",
                (turn_id,))
            conn.execute("UPDATE trailers SET status='outbound' WHERE id=?",
                         (turn["trailer_id"],))
            conn.execute("UPDATE tractors SET status='outbound' WHERE id=?",
                         (turn["out_tractor_id"],))
            if turn["appointment_id"]:
                conn.execute("UPDATE appointments SET status='done' WHERE id=?",
                             (turn["appointment_id"],))
            conn.execute("UPDATE turns SET status='closed',closed_at=? WHERE id=?",
                         (now, turn_id))
            dbm.insert_event(conn, self.clock, "turn_closed", turn_id, actor["id"],
                             "info", {"closed_at": now})
            conn.commit()
            return {"exited_at": now, "gate": gate_res.__dict__,
                    "abnormal_release": bool(turn["abnormal"])}
        except Exception:
            conn.rollback()
            raise

    # ============================================================ 查询看板
    def _decorate_turn(self, conn: sqlite3.Connection, t: sqlite3.Row) -> dict[str, Any]:
        d = dict(t)
        trailer = dbm.row(conn, "SELECT plate_no,spec,seal_no FROM trailers WHERE id=?",
                          (t["trailer_id"],))
        intractor = dbm.row(conn, "SELECT plate_no FROM tractors WHERE id=?",
                            (t["in_tractor_id"],))
        outtractor = dbm.row(conn, "SELECT plate_no FROM tractors WHERE id=?",
                             (t["out_tractor_id"],))
        indrv = dbm.row(conn, "SELECT name FROM drivers WHERE id=?",
                        (t["in_driver_id"],))
        outdrv = dbm.row(conn, "SELECT name FROM drivers WHERE id=?",
                         (t["out_driver_id"],))
        slot = dbm.row(conn, "SELECT code,zone,row_no,col_no FROM parking_slots WHERE id=?",
                       (t["slot_id"],))
        d.update(trailer_plate=trailer["plate_no"] if trailer else None,
                 trailer_spec=trailer["spec"] if trailer else None,
                 seal_no=trailer["seal_no"] if trailer else None,
                 in_tractor_plate=intractor["plate_no"] if intractor else None,
                 out_tractor_plate=outtractor["plate_no"] if outtractor else None,
                 in_driver_name=indrv["name"] if indrv else None,
                 out_driver_name=outdrv["name"] if outdrv else None,
                 slot=dict(slot) if slot else None)
        return d

    def arrivals_board(self) -> list[dict[str, Any]]:
        conn = self.conn
        out = []
        rows = dbm.allrows(
            conn,
            """SELECT a.*, t.plate_no AS tractor_plate, tr.plate_no AS trailer_plate,
                      d.name AS driver_name,
                      (SELECT id FROM turns tn WHERE tn.appointment_id=a.id
                         AND tn.status!='closed' LIMIT 1) AS open_turn_id
               FROM appointments a
               JOIN tractors t ON t.id=a.tractor_id
               JOIN trailers tr ON tr.id=a.trailer_id
               JOIN drivers d ON d.id=a.driver_id
               ORDER BY a.planned_arrival""")
        now = self.clock.now()
        for r in rows:
            d = dict(r)
            d["is_late_now"] = (a_late := (
                d["status"] == "scheduled"
                and now > parse(r["planned_arrival"]) + timedelta(minutes=LATE_GRACE_MIN)))
            out.append(d)
        return out

    def drop_queue(self) -> list[dict[str, Any]]:
        conn = self.conn
        rows = dbm.allrows(
            conn, "SELECT * FROM turns WHERE status IN ('queued','slotted') ORDER BY queued_at")
        out = []
        now = self.clock.now()
        for t in rows:
            d = self._decorate_turn(conn, t)
            queued = parse(t["queued_at"])
            waited = int((now - queued).total_seconds() // 60) if queued else 0
            d["waiting_min"] = waited
            d["overtime"] = waited >= WAIT_TIMEOUT_MIN
            out.append(d)
        return out

    def slot_map(self) -> dict[str, Any]:
        conn = self.conn
        slots = [dict(r) for r in dbm.allrows(
            conn, "SELECT * FROM parking_slots ORDER BY zone,row_no,col_no")]
        for s in slots:
            if s["turn_id"]:
                t = dbm.row(conn, "SELECT code,trailer_id FROM turns WHERE id=?",
                            (s["turn_id"],))
                if t:
                    tr = dbm.row(conn, "SELECT plate_no FROM trailers WHERE id=?",
                                 (t["trailer_id"],))
                    s["turn_code"] = t["code"]
                    s["trailer_plate"] = tr["plate_no"] if tr else None
        zones = {}
        for s in slots:
            zones.setdefault(s["zone"], []).append(s)
        return {"zones": zones, "slots": slots}

    def active_hitches(self) -> list[dict[str, Any]]:
        rows = dbm.allrows(
            self.conn,
            """SELECT h.*, tr.plate_no AS tractor_plate, tl.plate_no AS trailer_plate,
                      t.code AS turn_code, st.name AS staff_name
               FROM hitches h
               JOIN tractors tr ON tr.id=h.tractor_id
               JOIN trailers tl ON tl.id=h.trailer_id
               JOIN turns t ON t.id=h.turn_id
               LEFT JOIN staff st ON st.id=h.created_by
               WHERE h.active=1 ORDER BY h.linked_at""")
        return [dict(r) for r in rows]

    def turns_all(self) -> list[dict[str, Any]]:
        return [self._decorate_turn(self.conn, t) for t in dbm.allrows(
            self.conn, "SELECT * FROM turns ORDER BY id")]

    def turn_detail(self, turn_id: int) -> dict[str, Any]:
        conn = self.conn
        t = self._turn(turn_id)
        d = self._decorate_turn(conn, t)
        d["keys"] = [dict(k) for k in dbm.allrows(
            conn, "SELECT * FROM keys WHERE turn_id=? ORDER BY id", (turn_id,))]
        d["inspections"] = []
        for ins in dbm.allrows(
                conn, "SELECT i.*, s.name AS staff_name FROM inspections i "
                      "LEFT JOIN staff s ON s.id=i.inspector_id "
                      "WHERE i.turn_id=? ORDER BY i.id", (turn_id,)):
            dd = dict(ins)
            dd["photos"] = _photo_list(ins["photos"])
            d["inspections"].append(dd)
        d["approvals"] = [dict(a) for a in dbm.allrows(
            conn, "SELECT * FROM approvals WHERE turn_id=? ORDER BY id", (turn_id,))]
        d["events"] = [dict(e) for e in dbm.allrows(
            conn, "SELECT * FROM events WHERE turn_id=? ORDER BY id", (turn_id,))]
        return d

    def key_ledger(self) -> list[dict[str, Any]]:
        rows = dbm.allrows(
            self.conn,
            """SELECT k.*, tl.plate_no AS trailer_plate, t.code AS turn_code
               FROM keys k JOIN trailers tl ON tl.id=k.trailer_id
               JOIN turns t ON t.id=k.turn_id ORDER BY k.box_id,k.cell_no""")
        return [dict(r) for r in rows]

    def exit_checklist(self) -> list[dict[str, Any]]:
        rows = dbm.allrows(
            self.conn,
            "SELECT * FROM turns WHERE status IN "
            "('pickup_started','ready_to_exit','held') ORDER BY id")
        out = []
        for t in rows:
            d = self._decorate_turn(self.conn, t)
            dep = dbm.row(
                self.conn,
                "SELECT result FROM inspections WHERE turn_id=? AND phase='departure' "
                "ORDER BY id DESC LIMIT 1", (t["id"],))
            ap = dbm.row(
                self.conn, "SELECT status,reason_code FROM approvals WHERE turn_id=? "
                           "AND status='approved' ORDER BY id DESC LIMIT 1", (t["id"],))
            d["checks"] = {
                "pickup_started": bool(t["pickup_started_at"]),
                "seal_verified": bool(t["seal_verified_at"]),
                "appearance_departure": dep["result"] if dep else None,
                "hitched": bool(t["hitched_at"]),
                "key_released": bool(t["key_released_at"]),
                "abnormal_approved": dict(ap) if ap else None,
            }
            out.append(d)
        return out

    def events(self, limit: int = 200, etype: Optional[str] = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events"
        params: list[Any] = []
        if etype:
            sql += " WHERE type=?"
            params.append(etype)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(e) for e in dbm.allrows(self.conn, sql, tuple(params))]

    def staff_list(self) -> list[dict[str, Any]]:
        return [dict(r) for r in dbm.allrows(self.conn, "SELECT * FROM staff ORDER BY id")]

    # ------------------------------------------------------------- 等待超时
    def scan_wait_timeouts(self) -> list[int]:
        """进程内定时任务调用：排队超时写一次 wait_timeout 事件。返回本轮命中周转单。"""
        conn = self.conn
        hit: list[int] = []
        now = self.clock.now()
        rows = dbm.allrows(conn, "SELECT * FROM turns WHERE status='queued'")
        for t in rows:
            queued = parse(t["queued_at"])
            if not queued:
                continue
            waited = (now - queued).total_seconds() / 60
            if waited >= WAIT_TIMEOUT_MIN:
                already = dbm.row(
                    conn, "SELECT 1 FROM events WHERE turn_id=? AND type='wait_timeout' "
                          "LIMIT 1", (t["id"],))
                if not already:
                    dbm.insert_event(conn, self.clock, "wait_timeout", t["id"], None,
                                     "warn", {"waited_min": int(waited),
                                              "threshold_min": WAIT_TIMEOUT_MIN})
                    hit.append(t["id"])
        return hit


class WaitMonitor(threading.Thread):
    """后台守护线程：周期扫描排队等待超时。"""

    def __init__(self, svc: YardService, interval_sec: float = 20.0):
        super().__init__(daemon=True, name="wait-monitor")
        self.svc = svc
        self.interval = interval_sec
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.svc.scan_wait_timeouts()
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()
