"""平台核心规则测试：
挂接唯一性、车位竞争、交接步骤、异常放行、角色权限，
以及迟到/错格/替换/等待超时/门岗故障等事件留痕。

运行：python -m unittest -v
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime

from platform.adapters import CabinetAdapter, GateAdapter
from platform.clock import Clock
from platform.db import connect
from platform.permissions import PERMISSIONS, ServiceError, can
from platform.seed import seed
from platform.service import YardService

GATE, DISP1, DISP2, KEY, INSP, SUP = 1, 2, 3, 4, 5, 6


class YardTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.db")
        self.photos = os.path.join(self.tmp.name, "photos")
        self.svc = seed(self.db, self.photos, reset=True)
        # 测试与服务在同一线程内共享线程局部连接
        self.conn = self.svc.conn
        self.clock = self.svc.clock
        self.clock.set(datetime(2026, 9, 30, 9, 0))

    def tearDown(self):
        self.tmp.cleanup()

    def turn(self, turn_id):
        return self.conn.execute("SELECT * FROM turns WHERE id=?",
                                 (turn_id,)).fetchone()

    def fresh_appointment(self, code_suffix="X"):
        """再补一条预约并返回 (appt_code, tractor_id, trailer_id, driver_id)。"""
        c = self.conn
        tr = c.execute("INSERT INTO tractors(plate_no,model,status) VALUES(?,?,?)",
                       (f"测B·T{code_suffix}", "测试牵引", "idle")).lastrowid
        tl = c.execute(
            "INSERT INTO trailers(plate_no,spec,seal_no,status) VALUES(?,?,?,?)",
            (f"测E·G{code_suffix}", "测试挂车", f"SEAL-{code_suffix}", "enroute")
        ).lastrowid
        dv = c.execute(
            "INSERT INTO drivers(name,license_no,phone,employer) VALUES(?,?,?,?)",
            (f"司机{code_suffix}", f"ID{code_suffix}", "13800000000", "测试车队")
        ).lastrowid
        code = f"AP-TEST-{code_suffix}"
        c.execute(
            """INSERT INTO appointments(code,tractor_id,trailer_id,driver_id,
                   planned_arrival,kind,status) VALUES(?,?,?,?,?,'both','scheduled')""",
            (code, tr, tl, dv, "2026-09-30 08:00:00"))
        return code, tr, tl, dv


class TestHitchUniqueness(YardTestBase):
    def test_active_hitch_unique_index(self):
        """一个挂车同一时刻只能存在一组有效挂接关系（索引级保证）。"""
        _, _, tl, _ = self.fresh_appointment("U1")
        t2 = self.conn.execute(
            "INSERT INTO tractors(plate_no,model,status) VALUES(?,?,?)",
            ("测B·TU2", "第二牵引", "idle")).lastrowid
        now = "2026-09-30 09:00:00"
        self.conn.execute(
            "INSERT INTO hitches(turn_id,tractor_id,trailer_id,active,linked_at) "
            "VALUES(1,?,?,1,?)", (1, tl, now))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO hitches(turn_id,tractor_id,trailer_id,active,linked_at) "
                "VALUES(2,?,?,1,?)", (t2, tl, now))

    def test_arrive_rejects_trailer_already_hitched(self):
        """挂车已有有效挂接时，再次到园签到必须被业务层拦截。"""
        c = self.conn
        tr = c.execute("INSERT INTO tractors(plate_no,model,status) VALUES(?,?,?)",
                       ("测B·TZ", "测", "idle")).lastrowid
        dv = c.execute("SELECT id FROM drivers LIMIT 1").fetchone()[0]
        tl = c.execute("SELECT id FROM trailers WHERE plate_no='沪E·G3002'").fetchone()[0]
        c.execute(
            """INSERT INTO appointments(code,tractor_id,trailer_id,driver_id,
                   planned_arrival,kind,status) VALUES(?,?,?,?,?,'drop','scheduled')""",
            ("AP-DUP-G2", tr, tl, dv, "2026-09-30 08:00:00"))
        with self.assertRaises(ServiceError) as cm:
            self.svc.arrive(GATE, "AP-DUP-G2")
        self.assertEqual(cm.exception.code, "hitch_conflict")
        # 挂车没有挂接的预约（G3010）应可正常签到
        r = self.svc.arrive(GATE, "AP0930-10")
        self.assertIn("turn_id", r)

    def test_pickup_hitch_conflict_guarded(self):
        """提挂时挂车已被挂接，hitch() 抛 hitch_conflict。"""
        # 交接完成的 turn5，手工制造一条残留有效挂接，再走提挂流程
        t5 = 5
        self.svc.start_pickup(DISP1, t5, 12, 1)
        self.svc.verify_seal(INSP, t5, self.conn.execute(
            "SELECT seal_no FROM trailers WHERE id=(SELECT trailer_id FROM turns WHERE id=?)",
            (t5,)).fetchone()[0])
        self.svc.inspect_appearance(INSP, t5, "departure", "pass")
        # 插入竞争者的有效挂接（模拟另一车组抢先挂走）
        tr = self.conn.execute(
            "INSERT INTO tractors(plate_no,model,status) VALUES(?,?,?)",
            ("测B·TC", "抢占", "idle")).lastrowid
        tl = self.turn(t5)["trailer_id"]
        self.conn.execute(
            "INSERT INTO hitches(turn_id,tractor_id,trailer_id,active,linked_at) "
            "VALUES(?,?,?,1,'2026-09-30 09:05:00')", (t5, tr, tl))
        with self.assertRaises(ServiceError) as cm:
            self.svc.hitch(DISP1, t5)
        self.assertEqual(cm.exception.code, "hitch_conflict")
        active = self.conn.execute(
            "SELECT COUNT(*) FROM hitches WHERE trailer_id=? AND active=1",
            (tl,)).fetchone()[0]
        self.assertEqual(active, 1)


class TestSlotCompetition(YardTestBase):
    def test_two_dispatchers_only_first_wins(self):
        """两个调度员抢占同一车位：仅首个成功，第二个收到 slot_taken。"""
        code_a, _, _, _ = self.fresh_appointment("CA")
        code_b, _, _, _ = self.fresh_appointment("CB")
        ta = self.svc.arrive(GATE, code_a)["turn_id"]
        tb = self.svc.arrive(GATE, code_b)["turn_id"]

        errors = []

        def assign(turn_id, dispatcher):
            try:
                self.svc.assign_slot(dispatcher, turn_id, "C102")
            except ServiceError as e:
                errors.append(e.code)

        t1 = threading.Thread(target=assign, args=(ta, DISP1))
        t2 = threading.Thread(target=assign, args=(tb, DISP2))
        t1.start(); t2.start(); t1.join(); t2.join()

        self.assertEqual(errors, ["slot_taken"])
        slot = self.conn.execute(
            "SELECT * FROM parking_slots WHERE code='C102'").fetchone()
        self.assertEqual(slot["status"], "occupied")
        self.assertIn(slot["turn_id"], (ta, tb))
        winner = self.turn(slot["turn_id"])
        self.assertEqual(winner["status"], "slotted")
        # 占用事件单独留痕
        n = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE type='slot_occupied'").fetchone()[0]
        self.assertEqual(n, 1)

    def test_locked_slot_rejected(self):
        code, _, _, _ = self.fresh_appointment("LK")
        t = self.svc.arrive(GATE, code)["turn_id"]
        with self.assertRaises(ServiceError) as cm:
            self.svc.assign_slot(DISP1, t, "C304")  # 样例锁定维保位
        self.assertEqual(cm.exception.code, "slot_locked")

    def test_reassign_releases_old_slot(self):
        code, _, _, _ = self.fresh_appointment("MV")
        t = self.svc.arrive(GATE, code)["turn_id"]
        self.svc.assign_slot(DISP1, t, "C102")
        self.svc.assign_slot(DISP2, t, "C103")
        self.assertEqual(self.conn.execute(
            "SELECT status FROM parking_slots WHERE code='C102'").fetchone()[0], "free")
        self.assertEqual(self.conn.execute(
            "SELECT status FROM parking_slots WHERE code='C103'").fetchone()[0],
            "occupied")


class TestHandoverSteps(YardTestBase):
    def _slotted_turn(self, suffix="HS"):
        code, _, _, _ = self.fresh_appointment(suffix)
        t = self.svc.arrive(GATE, code)["turn_id"]
        self.svc.assign_slot(DISP1, t, "A101")
        return t

    def test_cannot_unhitch_before_slot(self):
        code, _, _, _ = self.fresh_appointment("H0")
        t = self.svc.arrive(GATE, code)["turn_id"]
        with self.assertRaises(ServiceError) as cm:
            self.svc.unhitch(DISP1, t)
        self.assertEqual(cm.exception.code, "not_slotted")

    def test_handover_needs_all_four(self):
        t = self._slotted_turn()
        self.assertEqual(self.turn(t)["status"], "slotted")
        self.svc.unhitch(DISP1, t)
        self.assertEqual(self.turn(t)["status"], "slotted")
        self.svc.chock(DISP2, t)
        self.assertEqual(self.turn(t)["status"], "slotted")  # 还差钥匙
        self.svc.deposit_key(KEY, t, "BOX-A", "19")
        self.assertEqual(self.turn(t)["status"], "handed_over")
        ev = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE turn_id=? AND type='handover_completed'",
            (t,)).fetchone()[0]
        self.assertEqual(ev, 1)

    def test_duplicate_steps_rejected(self):
        t = self._slotted_turn()
        self.svc.unhitch(DISP1, t)
        with self.assertRaises(ServiceError):
            self.svc.unhitch(DISP1, t)
        self.svc.chock(DISP1, t)
        with self.assertRaises(ServiceError):
            self.svc.chock(DISP1, t)

    def test_pickup_blocked_until_handover(self):
        code, _, _, _ = self.fresh_appointment("H1")
        t = self.svc.arrive(GATE, code)["turn_id"]
        self.svc.assign_slot(DISP1, t, "A101")
        self.svc.unhitch(DISP1, t)
        with self.assertRaises(ServiceError) as cm:
            self.svc.start_pickup(DISP1, t, 12, 1)
        self.assertEqual(cm.exception.code, "bad_state")

    def test_full_happy_path_closes_turn(self):
        t = self._slotted_turn("HP")
        self.svc.unhitch(DISP1, t)
        self.svc.chock(DISP1, t)
        self.svc.deposit_key(KEY, t, "BOX-A", "20")
        self.assertEqual(self.turn(t)["status"], "handed_over")
        self.svc.start_pickup(DISP2, t, 12, 1)
        seal = self.conn.execute(
            "SELECT seal_no FROM trailers WHERE id=(SELECT trailer_id FROM turns WHERE id=?)",
            (t,)).fetchone()[0]
        self.svc.verify_seal(INSP, t, seal)
        self.svc.inspect_appearance(INSP, t, "departure", "pass")
        self.svc.hitch(DISP1, t)
        self.svc.release_key(KEY, t)
        self.assertEqual(self.turn(t)["status"], "ready_to_exit")
        r = self.svc.exit_gate(GATE, t)
        self.assertFalse(r["abnormal_release"])
        self.assertEqual(self.turn(t)["status"], "closed")
        # 车位释放、挂接解除
        self.assertEqual(self.conn.execute(
            "SELECT status FROM parking_slots WHERE code='A101'").fetchone()[0], "free")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM hitches WHERE turn_id=? AND active=1",
            (t,)).fetchone()[0], 0)


class TestKeyWrongCell(YardTestBase):
    def test_deposit_into_occupied_cell_fails_then_retry_ok(self):
        code, _, _, _ = self.fresh_appointment("KW")
        t = self.svc.arrive(GATE, code)["turn_id"]
        self.svc.assign_slot(DISP1, t, "A102")
        self.svc.unhitch(DISP1, t)
        self.svc.chock(DISP1, t)
        self.svc.cabinet.put("KEY-PREEXISTING", "BOX-A", "21")
        with self.assertRaises(ServiceError) as cm:
            self.svc.deposit_key(KEY, t, "BOX-A", "21")
        self.assertEqual(cm.exception.code, "key_wrong_cell")
        n = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE turn_id=? AND type='key_wrong_cell'",
            (t,)).fetchone()[0]
        self.assertEqual(n, 1)
        self.assertEqual(self.turn(t)["status"], "slotted")  # 错格不算交接
        self.svc.deposit_key(KEY, t, "BOX-A", "22")
        self.assertEqual(self.turn(t)["status"], "handed_over")

    def test_release_from_wrong_cell_alerts(self):
        # 自建一条已挂接、钥匙仍在柜内（BOX-A 22）的周转单
        code, _, _, _ = self.fresh_appointment("KR")
        t = self.svc.arrive(GATE, code)["turn_id"]
        self.svc.assign_slot(DISP1, t, "A105")
        self.svc.unhitch(DISP1, t)
        self.svc.chock(DISP1, t)
        self.svc.deposit_key(KEY, t, "BOX-A", "22")
        self.svc.start_pickup(DISP2, t, 12, 1)
        seal = self.conn.execute(
            "SELECT seal_no FROM trailers WHERE id=(SELECT trailer_id FROM turns WHERE id=?)",
            (t,)).fetchone()[0]
        self.svc.verify_seal(INSP, t, seal)
        self.svc.inspect_appearance(INSP, t, "departure", "pass")
        self.svc.hitch(DISP1, t)
        # 从错误格口取钥 -> 错格告警，钥匙保留在柜内
        with self.assertRaises(ServiceError) as cm:
            self.svc.release_key(KEY, t, cell_no="99")
        self.assertEqual(cm.exception.code, "key_wrong_cell")
        # 错格取钥失败：挂接保持、状态不变、钥匙仍在柜
        self.assertIsNotNone(self.turn(t)["hitched_at"])
        self.assertEqual(self.turn(t)["status"], "pickup_started")
        self.assertEqual(self.conn.execute(
            "SELECT status FROM keys WHERE turn_id=?", (t,)).fetchone()[0],
            "wrong_cell")
        # 从登记格口取出成功，随后可正常出园
        self.svc.release_key(KEY, t)
        self.svc.exit_gate(GATE, t)
        self.assertEqual(self.turn(t)["status"], "closed")


class TestAbnormalRelease(YardTestBase):
    def test_seal_mismatch_hold_and_supervisor_flow(self):
        # 样例 turn6 已因铅封不符挂起并有 pending 审批
        t6 = self.turn(6)
        self.assertEqual(t6["status"], "held")
        ap = self.conn.execute(
            "SELECT * FROM approvals WHERE turn_id=6 AND status='pending'").fetchone()
        self.assertIsNotNone(ap)

        # 调度员/门岗无权审批
        with self.assertRaises(ServiceError) as cm:
            self.svc.decide_approval(DISP1, ap["id"], True)
        self.assertEqual(cm.exception.http, 403)

        # 主管批准
        self.svc.decide_approval(SUP, ap["id"], True)
        self.assertEqual(self.turn(6)["abnormal"], 1)

        # 铅封仍需重新核对（异常批准解除挂起，业务步骤不跳过）
        seal = self.conn.execute(
            "SELECT seal_no FROM trailers WHERE id=(SELECT trailer_id FROM turns WHERE id=6)"
        ).fetchone()[0]
        self.svc.verify_seal(INSP, 6, seal)
        self.svc.inspect_appearance(INSP, 6, "departure", "pass")
        self.svc.hitch(DISP2, 6)
        self.svc.release_key(KEY, 6)
        r = self.svc.exit_gate(GATE, 6)
        self.assertTrue(r["abnormal_release"])
        self.assertEqual(self.turn(6)["status"], "closed")

    def test_abnormal_exit_without_approval_rejected(self):
        """异常车辆未经主管批准不得放行。"""
        # turn7：损伤已批准；复制其路径到一台未批准的新挂车
        code, tr, tl, dv = self.fresh_appointment("AR")
        t = self.svc.arrive(GATE, code)["turn_id"]
        self.svc.assign_slot(DISP1, t, "A104")
        self.svc.unhitch(DISP1, t)
        self.svc.chock(DISP1, t)
        self.svc.deposit_key(KEY, t, "BOX-A", "23")
        self.svc.start_pickup(DISP2, t, tr, dv)
        seal = self.conn.execute(
            "SELECT seal_no FROM trailers WHERE id=?", (tl,)).fetchone()[0]
        self.svc.verify_seal(INSP, t, seal)
        self.svc.inspect_appearance(INSP, t, "departure", "damage",
                                   "尾门变形")
        # 已挂起，未提批准
        self.assertEqual(self.turn(t)["status"], "held")
        with self.assertRaises(ServiceError) as cm:
            self.svc.exit_gate(GATE, t)
        self.assertEqual(cm.exception.code, "bad_state")

    def test_rejected_approval_stays_held(self):
        t6 = 6
        ap = self.conn.execute(
            "SELECT id FROM approvals WHERE turn_id=6 AND status='pending'").fetchone()
        self.svc.decide_approval(SUP, ap["id"], False)
        self.assertEqual(self.turn(6)["status"], "held")
        self.assertEqual(self.turn(6)["abnormal"], 0)
        with self.assertRaises(ServiceError):
            self.svc.hitch(DISP2, 6)

    def test_damage_seed_turn7_approved_and_waiting_gate(self):
        t7 = self.turn(7)
        self.assertEqual(t7["abnormal"], 1)
        self.assertEqual(t7["status"], "ready_to_exit")
        r = self.svc.exit_gate(GATE, 7)
        self.assertTrue(r["abnormal_release"])

    def test_gate_fault_blocks_exit(self):
        self.svc.gate.set_fault("GATE-OUT-01", True)
        with self.assertRaises(ServiceError) as cm:
            self.svc.exit_gate(GATE, 9)  # 正常待出园
        self.assertEqual(cm.exception.code, "gate_fault")
        # 道闸修复后可放行
        self.svc.gate.set_fault("GATE-OUT-01", False)
        self.svc.exit_gate(GATE, 9)
        self.assertEqual(self.turn(9)["status"], "closed")


class TestRoles(unittest.TestCase):
    def test_permission_matrix_structure(self):
        for role in ("gate", "dispatcher", "keyclerk", "inspector", "supervisor"):
            self.assertIn(role, PERMISSIONS)
            self.assertTrue(len(PERMISSIONS[role]) >= 1)
        # 危险动作仅主管
        self.assertFalse(can("gate", "decide_approval"))
        self.assertFalse(can("dispatcher", "decide_approval"))
        self.assertTrue(can("supervisor", "decide_approval"))
        # 职责隔离：钥匙员不能调度，调度员不能放道闸
        self.assertFalse(can("keyclerk", "assign_slot"))
        self.assertFalse(can("dispatcher", "exit_gate"))
        self.assertFalse(can("inspector", "deposit_key"))
        self.assertTrue(can("gate", "arrive"))
        self.assertTrue(can("dispatcher", "unhitch"))

    def test_every_role_denies_unknown_action(self):
        for role in PERMISSIONS:
            self.assertFalse(can(role, "drop_table"))


class TestRoleEnforcement(YardTestBase):
    def test_gate_cannot_assign_slot(self):
        with self.assertRaises(ServiceError) as cm:
            self.svc.assign_slot(GATE, 2, "A101")
        self.assertEqual(cm.exception.http, 403)

    def test_inspector_cannot_hitch(self):
        with self.assertRaises(ServiceError) as cm:
            self.svc.hitch(INSP, 7)
        self.assertEqual(cm.exception.http, 403)

    def test_supervisor_cannot_arrive(self):
        with self.assertRaises(ServiceError) as cm:
            self.svc.arrive(SUP, "AP0930-11")
        self.assertEqual(cm.exception.http, 403)

    def test_dispatcher_cannot_deposit_key(self):
        with self.assertRaises(ServiceError) as cm:
            self.svc.deposit_key(DISP1, 4, "BOX-A", "01")
        self.assertEqual(cm.exception.http, 403)


class TestEventsAndExceptions(YardTestBase):
    def test_late_arrival_recorded(self):
        # AP0930-11 计划 08:00，当前时钟 09:00，超过 15 分钟宽限
        r = self.svc.arrive(GATE, "AP0930-11")
        self.assertTrue(r["late"])
        n = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE turn_id=? AND type='appointment_late'",
            (r["turn_id"],)).fetchone()[0]
        self.assertEqual(n, 1)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM appointments WHERE code='AP0930-11'").fetchone()[0],
            "late")

    def test_driver_substitution_recorded(self):
        code, _, _, _ = self.fresh_appointment("DS")
        r = self.svc.arrive(GATE, code, actual_driver_id=2)
        n = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE turn_id=? AND type='driver_substituted'",
            (r["turn_id"],)).fetchone()[0]
        self.assertEqual(n, 1)

    def test_wait_timeout_scan(self):
        # turn2 已排队 34 分钟（样例已记一次）；新增一条排队 45 分钟的
        code, _, _, _ = self.fresh_appointment("WT")
        r = self.svc.arrive(GATE, code)
        self.conn.execute("UPDATE turns SET queued_at=? WHERE id=?",
                          ("2026-09-30 08:15:00", r["turn_id"]))
        hits = self.svc.scan_wait_timeouts()
        self.assertIn(r["turn_id"], hits)
        # 幂等：再扫一次不重复
        hits2 = self.svc.scan_wait_timeouts()
        self.assertNotIn(r["turn_id"], hits2)

    def test_cannot_pickup_held_and_event(self):
        t5 = 5  # handed_over
        self.svc.start_pickup(DISP1, t5, 12, 1)
        seal = self.conn.execute(
            "SELECT seal_no FROM trailers WHERE id=(SELECT trailer_id FROM turns WHERE id=5)"
        ).fetchone()[0]
        self.svc.verify_seal(INSP, t5, seal)
        self.svc.cannot_pickup(DISP2, t5, "鞍座故障")
        self.assertEqual(self.turn(5)["status"], "held")
        n = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE turn_id=5 AND type='cannot_pickup'"
        ).fetchone()[0]
        self.assertEqual(n, 1)

    def test_adapters_basic_results(self):
        gate = GateAdapter()
        g = gate.open_barrier("T1", "测牌", "in")
        self.assertTrue(g.ok)
        self.assertEqual(g.barrier, "opened")
        cab = CabinetAdapter()
        self.assertTrue(cab.put("K1", "B", "1").ok)
        bad = cab.put("K2", "B", "1")
        self.assertFalse(bad.ok)
        self.assertEqual(bad.occupied_by, "K1")
        fetch_bad = cab.fetch("K2", "B", "1")
        self.assertFalse(fetch_bad.ok)
        self.assertTrue(cab.fetch("K1", "B", "1").ok)


if __name__ == "__main__":
    unittest.main()
