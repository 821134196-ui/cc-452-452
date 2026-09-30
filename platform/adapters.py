"""模拟外部适配器：园区门岗道闸与智能钥匙柜。

真实场地由硬件 SDK / HTTP 回调返回结果；此处用进程内状态模拟，
并保留一致的结果结构，便于对接替换。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class GateResult:
    ok: bool
    gate_id: str
    direction: str
    barrier: str           # opened / closed / fault
    message: str


@dataclass
class CabinetResult:
    ok: bool
    box_id: str
    cell_no: str
    tag: Optional[str]
    occupied_by: Optional[str]
    message: str


class GateAdapter:
    """门岗道闸模拟器：记录抬杆流水，偶发故障可由 fault_until 控制。"""

    def __init__(self) -> None:
        self.log: list[dict] = []
        self.fault_gates: set[str] = set()

    def _gate_for(self, direction: str) -> str:
        return "GATE-IN-01" if direction == "in" else "GATE-OUT-01"

    def open_barrier(self, turn_code: str, plate: str, direction: str) -> GateResult:
        gate_id = self._gate_for(direction)
        if gate_id in self.fault_gates:
            res = GateResult(False, gate_id, direction, "fault",
                             "道闸故障，已通知维保，转人工核验")
        else:
            res = GateResult(True, gate_id, direction, "opened",
                             "道闸已抬起，请按车道线缓行")
        self.log.append({"turn": turn_code, "plate": plate,
                         "direction": direction, "result": res.__dict__})
        return res

    def set_fault(self, gate_id: str, on: bool) -> None:
        if on:
            self.fault_gates.add(gate_id)
        else:
            self.fault_gates.discard(gate_id)


class CabinetAdapter:
    """智能钥匙柜模拟器：以 box:cell 为键记录格口占用。

    放入已被其他钥匙占用的格口 -> 错格结果；
    从与登记不符的格口取钥匙 -> 错格告警。
    """

    def __init__(self) -> None:
        # "BOX-A:12" -> tag
        self._cells: dict[str, str] = {}

    def _key(self, box_id: str, cell_no: str) -> str:
        return f"{box_id}:{cell_no}"

    def put(self, tag: str, box_id: str, cell_no: str) -> CabinetResult:
        key = self._key(box_id, cell_no)
        occupant = self._cells.get(key)
        if occupant and occupant != tag:
            return CabinetResult(False, box_id, cell_no, tag, occupant,
                                 f"格口 {cell_no} 已被 {occupant} 占用，钥匙错格")
        self._cells[key] = tag
        return CabinetResult(True, box_id, cell_no, tag, None,
                             f"钥匙已入柜 {box_id}-{cell_no}，格口已锁闭")

    def fetch(self, tag: str, box_id: str, cell_no: str) -> CabinetResult:
        key = self._key(box_id, cell_no)
        occupant = self._cells.get(key)
        if occupant is None:
            return CabinetResult(False, box_id, cell_no, tag, None,
                                 f"格口 {cell_no} 为空，未找到钥匙，疑似错格")
        if occupant != tag:
            return CabinetResult(False, box_id, cell_no, tag, occupant,
                                 f"格口内为 {occupant}，与请求标签 {tag} 不符，钥匙错格")
        self._cells.pop(key, None)
        return CabinetResult(True, box_id, cell_no, tag, occupant,
                             f"格口 {cell_no} 已弹出，钥匙取出成功")

    def snapshot(self) -> dict[str, str]:
        return dict(self._cells)

    def reset(self) -> None:
        self._cells.clear()
