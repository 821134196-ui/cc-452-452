"""平台规则测试：

  1. 挂接唯一性（一个挂车同一时刻仅一组有效挂接）
  2. 车位竞争（两个调度员抢同一车位仅首个成功）
  3. 交接步骤（解绑→停妥查验→轮挡→钥匙入柜，顺序强制）
  4. 异常放行（门岗申请、仅主管可批准、批准后才能出园）
  5. 园区角色权限（越权被拒）

另含迟到、钥匙错格、铅封不符、司机替换、挂车损伤、无法提挂、等待超时。
"""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app.adapters import GateAdapter, KeyCabinetAdapter, PhotoStore
from app.db import connect, init_db
from app.seed import DRIVERS, TRACTORS, TRAILERS, _insert_masters
from app.service import ServiceError, YardService, iso


class _Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def add(self, seconds: int):
        self.t += timedelta(seconds=seconds)


class YardTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.conn = connect(self.dir / "t.db")
        init_db(self.conn)
        _insert_masters(self.conn)
        self.clock = _Clock(datetime(2026, 9, 30, 8, 0, 0))
        self.svc = YardService(
            self.conn, PhotoStore(self.dir / "photos"),
            GateAdapter(), KeyCabinetAdapter(), clock=self.clock)
        self._seq = 0

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    # -- 构造预约并签到的辅助 --
    def book_and_checkin(self, idx: int = 1, planned_offset_min: int = 30,
                         user: str = "zhang", trailer_idx: int | None = None):
        self._seq += 1
        t_plate = TRACTORS[idx - 1][0]
        v_plate = TRAILERS[(trailer_idx or idx) - 1][0]
        lic = DRIVERS[(idx - 1) % len(DRIVERS)][0]
        planned = self.clock.t + timedelta(minutes=planned_offset_min)
        self.conn.execute(
            "INSERT INTO appointments(code,tractor_plate,trailer_plate,driver_license,"
            "purpose,planned_arrival,status) VALUES(?,?,?,?,?,?,'booked')",
            (f"AP-T{self._seq:02d}", t_plate, v_plate, lic, "drop", iso(planned)))
        self.conn.commit()
        return self.svc.check_in(user, t_plate)["cycle"]

    def full_drop(self, idx=1, stall="A-101", seal="SEAL-1", cell=None):
        code = self.book_and_checkin(idx)
        self.svc.assign_stall("li", code, stall)
        self.svc.unhitch("li", code)
        self.svc.intake_check("wu", code, seal)
        self.svc.chock("li", code)
        token = "KB-01"
        self.svc.store_keys("zhou", code, token, cell or KeyCabinetAdapter().cell_for_token(token))
        return code


# --------------------------------------------------------------- 1. 挂接唯一性

class TestCouplingUniqueness(YardTestBase):
    def test_trailer_single_active_coupling(self):
        # 挂车 G2001 完成甩挂；两辆牵引车先后尝试挂同一挂车
        code = self.full_drop(idx=1, stall="A-101")
        self.svc.start_pickup("li", code, TRACTORS[1][0], DRIVERS[1][0])  # T1002
        self.svc.verify_pickup("wu", code, DRIVERS[1][0], "SEAL-1")
        self.svc.retrieve_keys("zhou", code, "KB-01",
                               KeyCabinetAdapter().cell_for_token("KB-01"))
        self.svc.hitch("li", code)

        active = self.conn.execute(
            "SELECT COUNT(*) c FROM couplings WHERE trailer_plate=? AND deleted=0",
            (TRAILERS[0][0],)).fetchone()["c"]
        self.assertEqual(active, 1)

        # 数据库层兜底：直接再插一条同挂车有效挂接必被部分唯一索引拒绝
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO couplings(tractor_plate,trailer_plate,coupled_at,by_user) "
                "VALUES(?,?,?,?)",
                (TRACTORS[2][0], TRAILERS[0][0], iso(self.clock.t), "tester"))

    def test_tractor_cannot_double_hitch(self):
        # 牵引车 T1002 挂了 G2002 后，再去挂 G2001 应被业务规则拒绝
        c2 = self.full_drop(idx=2, stall="A-102", seal="S2")
        self.svc.start_pickup("li", c2, TRACTORS[1][0], DRIVERS[1][0])
        self.svc.verify_pickup("wu", c2, DRIVERS[1][0], "S2")
        self.svc.hitch("li", c2)  # T1002 ↔ G2002

        c1 = self.full_drop(idx=1, stall="A-101", seal="S1")
        self.svc.start_pickup("li", c1, TRACTORS[1][0], DRIVERS[0][0])  # 仍用 T1002
        self.svc.verify_pickup("wu", c1, DRIVERS[0][0], "S1")
        with self.assertRaises(ServiceError) as cm:
            self.svc.hitch("li", c1)
        self.assertEqual(cm.exception.code, "coupling_busy")

    def test_concurrent_hitch_only_one_wins(self):
        # 两个线程用各自连接并发挂同一挂车：唯一索引决定只有一个成功
        barrier = threading.Barrier(2)
        results = []

        def try_insert(tractor):
            c = connect(self.dir / "t.db")
            barrier.wait()
            try:
                c.execute(
                    "INSERT INTO couplings(tractor_plate,trailer_plate,coupled_at,by_user) "
                    "VALUES(?,?,?,?)",
                    (tractor, TRAILERS[0][0], iso(self.clock.t), "race"))
                c.commit()
                results.append("ok")
            except sqlite3.IntegrityError:
                results.append("lost")
            finally:
                c.close()

        t1 = threading.Thread(target=try_insert, args=(TRACTORS[0][0],))
        t2 = threading.Thread(target=try_insert, args=(TRACTORS[1][0],))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(sorted(results), ["lost", "ok"])


# --------------------------------------------------------------- 2. 车位竞争

class TestStallCompetition(YardTestBase):
    def test_first_dispatcher_wins(self):
        c1 = self.book_and_checkin(1)
        c2 = self.book_and_checkin(2)
        r1 = self.svc.assign_stall("li", c1, "A-101")
        self.assertEqual(r1["stall"], "A-101")
        with self.assertRaises(ServiceError) as cm:
            self.svc.assign_stall("wang", c2, "A-101")
        self.assertEqual(cm.exception.code, "stall_race")
        # 失败方产生“车位占用”异常，且其工单仍在排队
        exc = self.conn.execute(
            "SELECT COUNT(*) c FROM exceptions WHERE kind='stall_occupied'").fetchone()["c"]
        self.assertEqual(exc, 1)
        phase = self.conn.execute("SELECT phase FROM cycles WHERE code=?", (c2,)).fetchone()[0]
        self.assertEqual(phase, "queued")

    def test_parallel_assign_only_one_succeeds(self):
        c1 = self.book_and_checkin(1)
        c2 = self.book_and_checkin(2)
        out = {}
        barrier = threading.Barrier(2)

        def assign(who, code):
            # 独立连接，模拟两个调度员在各自请求线程中同时分位
            c = connect(self.dir / "t.db")
            svc = YardService(c, PhotoStore(self.dir / "photos"),
                              GateAdapter(), KeyCabinetAdapter(), clock=self.clock)
            barrier.wait()
            try:
                svc.assign_stall(who, code, "B-205")
                out[who] = "ok"
            except ServiceError:
                out[who] = "lost"
            finally:
                c.close()

        t1 = threading.Thread(target=assign, args=("li", c1))
        t2 = threading.Thread(target=assign, args=("wang", c2))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(sorted(out.values()), ["lost", "ok"])
        occupant = self.conn.execute(
            "SELECT o.trailer_plate FROM stall_occupancy o JOIN stalls s ON s.id=o.stall_id "
            "WHERE s.code='B-205' AND o.released=0").fetchone()
        self.assertIsNotNone(occupant)

    def test_stall_freed_after_hitch(self):
        code = self.full_drop(idx=1, stall="C-308")
        self.svc.start_pickup("li", code, TRACTORS[1][0], DRIVERS[1][0])
        self.svc.verify_pickup("wu", code, DRIVERS[1][0], "SEAL-1")
        self.svc.retrieve_keys("zhou", code, "KB-01",
                               KeyCabinetAdapter().cell_for_token("KB-01"))
        self.svc.hitch("li", code)
        status = self.conn.execute(
            "SELECT status FROM stalls WHERE code='C-308'").fetchone()[0]
        self.assertEqual(status, "free")


# --------------------------------------------------------------- 3. 交接步骤

class TestHandoffSteps(YardTestBase):
    def test_phase_order_enforced(self):
        code = self.book_and_checkin(1)
        # 未分位不能解绑
        with self.assertRaises(ServiceError) as cm:
            self.svc.unhitch("li", code)
        self.assertEqual(cm.exception.code, "bad_phase")
        self.svc.assign_stall("li", code, "A-101")
        # 未查验不能轮挡
        self.svc.unhitch("li", code)
        with self.assertRaises(ServiceError):
            self.svc.chock("li", code)
        # 未停妥不能入钥匙
        with self.assertRaises(ServiceError):
            self.svc.store_keys("zhou", code, "KB-01", "R1-01")

    def test_handoff_requires_all_three(self):
        # 停妥、轮挡、钥匙入柜缺一不可：仅前两步完成不算交接完成
        code = self.book_and_checkin(1)
        self.svc.assign_stall("li", code, "A-101")
        self.svc.unhitch("li", code)
        self.svc.intake_check("wu", code, "SEAL-9")
        self.svc.chock("li", code)
        phase = self.conn.execute("SELECT phase FROM cycles WHERE code=?", (code,)).fetchone()[0]
        self.assertEqual(phase, "chocked")
        # 错格不入柜
        with self.assertRaises(ServiceError) as cm:
            self.svc.store_keys("zhou", code, "KB-09", "R1-02")
        self.assertEqual(cm.exception.code, "key_wrong_cell")
        phase2 = self.conn.execute("SELECT phase FROM cycles WHERE code=?", (code,)).fetchone()[0]
        self.assertEqual(phase2, "chocked", "错格不应推进交接")
        # 正确格口才完成
        self.svc.store_keys("zhou", code, "KB-09",
                            KeyCabinetAdapter().cell_for_token("KB-09"))
        phase3 = self.conn.execute("SELECT phase FROM cycles WHERE code=?", (code,)).fetchone()[0]
        self.assertEqual(phase3, "keys_cabinet")

    def test_appearance_photos_and_damage(self):
        code = self.book_and_checkin(1)
        self.svc.assign_stall("li", code, "A-101")
        self.svc.unhitch("li", code)
        res = self.svc.intake_check("wu", code, "SEAL-1",
                                    damages={"left": "划痕", "roof": "凹陷"})
        self.assertEqual(len(res["photos"]), 5)
        for f in res["photos"]:
            self.assertTrue((self.dir / "photos" / f).exists())
        dmg = self.conn.execute(
            "SELECT COUNT(*) c FROM exceptions WHERE kind='trailer_damage'").fetchone()["c"]
        self.assertEqual(dmg, 2)


# --------------------------------------------------------------- 4. 异常放行

class TestAbnormalExit(YardTestBase):
    def test_abnormal_exit_requires_supervisor(self):
        code = self.full_drop(idx=1, stall="A-101")
        # 尚未挂接，直接出园被拒
        with self.assertRaises(ServiceError) as cm:
            self.svc.exit_confirm("zhang", code)
        self.assertEqual(cm.exception.code, "exit_not_allowed")

        eid = self.svc.request_abnormal_exit("zhang", code, "铅封不符需先放行")
        # 调度员不能审批
        with self.assertRaises(ServiceError) as cm:
            self.svc.decide_exception("li", eid, True)
        self.assertEqual(cm.exception.code, "forbidden")
        # 主管批准前仍不能出园
        with self.assertRaises(ServiceError) as cm:
            self.svc.exit_confirm("zhang", code)
        self.assertEqual(cm.exception.code, "exit_not_allowed")
        # 主管批准
        self.svc.decide_exception("zhao", eid, True, "现场核准")
        res = self.svc.exit_confirm("zhang", code)
        self.assertTrue(res["abnormal"])
        phase = self.conn.execute("SELECT phase FROM cycles WHERE code=?", (code,)).fetchone()[0]
        self.assertEqual(phase, "closed")

    def test_supervisor_reject_blocks_exit(self):
        code = self.full_drop(idx=1, stall="A-101")
        eid = self.svc.request_abnormal_exit("zhang", code, "异常")
        self.svc.decide_exception("zhao", eid, False, "不符合条件")
        with self.assertRaises(ServiceError):
            self.svc.exit_confirm("zhang", code)

    def test_normal_exit_after_full_pickup(self):
        code = self.full_drop(idx=1, stall="A-101")
        self.svc.start_pickup("li", code, TRACTORS[1][0], DRIVERS[1][0])
        self.svc.verify_pickup("wu", code, DRIVERS[1][0], "SEAL-1")
        self.svc.retrieve_keys("zhou", code, "KB-01",
                               KeyCabinetAdapter().cell_for_token("KB-01"))
        self.svc.hitch("li", code)
        res = self.svc.exit_confirm("zhang", code)
        self.assertFalse(res["abnormal"])


# --------------------------------------------------------------- 5. 角色权限

class TestRolePermissions(YardTestBase):
    def test_cross_role_actions_forbidden(self):
        code = self.book_and_checkin(1)
        # 钥匙员不能分位
        with self.assertRaises(ServiceError) as cm:
            self.svc.assign_stall("zhou", code, "A-101")
        self.assertEqual(cm.exception.code, "forbidden")
        # 调度员不能门岗签到（无预约也先被权限拦截）
        with self.assertRaises(ServiceError) as cm:
            self.svc.check_in("li", TRACTORS[1][0])
        self.assertEqual(cm.exception.code, "forbidden")
        # 门岗不能解绑
        with self.assertRaises(ServiceError) as cm:
            self.svc.unhitch("zhang", code)
        self.assertEqual(cm.exception.code, "forbidden")
        # 非主管不能审批异常
        with self.assertRaises(ServiceError) as cm:
            self.svc.decide_exception("li", 999, True)
        self.assertEqual(cm.exception.code, "forbidden")

    def test_each_role_allownce(self):
        # 门岗签到
        code = self.book_and_checkin(1, user="zhang")
        # 调度分位/解绑/轮挡
        self.svc.assign_stall("li", code, "A-101")
        self.svc.unhitch("wang", code)
        # 查验员入位
        self.svc.intake_check("wu", code, "SEAL-1")
        self.svc.chock("li", code)
        # 钥匙员入柜
        self.svc.store_keys("zhou", code, "KB-01",
                            KeyCabinetAdapter().cell_for_token("KB-01"))
        # 调度安排提挂
        self.svc.start_pickup("wang", code, TRACTORS[1][0], DRIVERS[1][0])
        self.svc.verify_pickup("wu", code, DRIVERS[1][0], "SEAL-1")
        self.svc.retrieve_keys("zhou", code, "KB-01",
                               KeyCabinetAdapter().cell_for_token("KB-01"))
        self.svc.hitch("li", code)
        self.svc.exit_confirm("zhang", code)
        self.assertEqual(
            self.conn.execute("SELECT phase FROM cycles WHERE code=?", (code,)).fetchone()[0],
            "closed")

    def test_unknown_user(self):
        with self.assertRaises(ServiceError) as cm:
            self.svc.assign_stall("nobody", "X", "A-101")
        self.assertEqual(cm.exception.code, "forbidden")


# --------------------------------------------------------------- 其他业务规则

class TestOtherRules(YardTestBase):
    def test_late_arrival_exception(self):
        # 计划时刻已过 → 签到判迟到并开异常单，但仍可入园
        code = self.book_and_checkin(1, planned_offset_min=-10)
        exc = self.conn.execute(
            "SELECT kind FROM exceptions WHERE kind='late_arrival'").fetchone()
        self.assertIsNotNone(exc)
        phase = self.conn.execute("SELECT phase FROM cycles WHERE code=?", (code,)).fetchone()[0]
        self.assertEqual(phase, "queued")

    def test_seal_mismatch_blocks_verify(self):
        code = self.full_drop(idx=1, stall="A-101", seal="SEAL-OK")
        self.svc.start_pickup("li", code, TRACTORS[1][0], DRIVERS[1][0])
        with self.assertRaises(ServiceError) as cm:
            self.svc.verify_pickup("wu", code, DRIVERS[1][0], "SEAL-BAD")
        self.assertEqual(cm.exception.code, "verify_failed")
        phase = self.conn.execute("SELECT phase FROM cycles WHERE code=?", (code,)).fetchone()[0]
        self.assertEqual(phase, "pickup_pending")

    def test_driver_swap_recorded(self):
        code = self.full_drop(idx=1, stall="A-101")
        self.svc.start_pickup("li", code, TRACTORS[1][0], DRIVERS[4][0])
        sw = self.conn.execute(
            "SELECT COUNT(*) c FROM exceptions WHERE kind='driver_swap'").fetchone()["c"]
        self.assertEqual(sw, 1)
        # 到场司机与登记不符也会开异常并拦截
        with self.assertRaises(ServiceError):
            self.svc.verify_pickup("wu", code, DRIVERS[5][0], "SEAL-1")

    def test_cannot_pick_goes_back_to_pending(self):
        code = self.full_drop(idx=1, stall="A-101")
        self.svc.start_pickup("li", code, TRACTORS[1][0], DRIVERS[1][0])
        self.svc.cannot_pick("li", code, "鞍座故障")
        phase = self.conn.execute("SELECT phase FROM cycles WHERE code=?", (code,)).fetchone()[0]
        self.assertEqual(phase, "pickup_pending")
        self.assertTrue(self.conn.execute(
            "SELECT COUNT(*) c FROM exceptions WHERE kind='cannot_pick'").fetchone()["c"])

    def test_wait_timeout_scan(self):
        code = self.book_and_checkin(1)
        # 插入一条早已超时的排队等待
        self.conn.execute(
            "UPDATE wait_tracking SET started_at=?, threshold_s=300 "
            "WHERE cycle_id=(SELECT id FROM cycles WHERE code=?) AND kind='queue'",
            (iso(self.clock.t - timedelta(seconds=360)), code))
        self.conn.commit()
        alerts = self.svc.scan_timeouts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["kind"], "queue")
        self.assertGreaterEqual(alerts[0]["minutes"], 6)
        # 只提醒一次
        self.assertEqual(self.svc.scan_timeouts(), [])

    def test_pickup_new_damage_recorded(self):
        code = self.full_drop(idx=1, stall="A-101")
        self.svc.start_pickup("li", code, TRACTORS[1][0], DRIVERS[1][0])
        with self.assertRaises(ServiceError):
            self.svc.verify_pickup("wu", code, DRIVERS[1][0], "SEAL-1",
                                   pickup_damages={"rear": "新增刮擦"})
        dmg = self.conn.execute(
            "SELECT COUNT(*) c FROM exceptions WHERE kind='trailer_damage'").fetchone()["c"]
        self.assertEqual(dmg, 1)

    def test_checkin_without_appointment(self):
        with self.assertRaises(ServiceError) as cm:
            self.svc.check_in("zhang", "沪D·T9999")
        self.assertEqual(cm.exception.code, "no_appointment")


if __name__ == "__main__":
    unittest.main(verbosity=2)
