"""模拟外部适配器：门岗扫描、钥匙柜、本地外观照片。

真实部署中这些会调用园区硬件；此处返回确定性的模拟结果，
并把外观照片以 SVG 形式写入本地文件夹 photos/。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SIDES = ("front", "rear", "left", "right", "roof")
SIDE_CN = {"front": "前", "rear": "后", "left": "左", "right": "右", "roof": "顶"}


class GateAdapter:
    """门岗道闸：车牌识别 + 抬杆。"""

    def scan_plate(self, camera: str, plate: str) -> dict:
        # 摄像头读到的车牌；真实硬件可能识别错，这里回显并放行
        return {"camera": camera, "read_plate": plate, "confidence": 0.98}

    def raise_barrier(self, lane: str, plate: str, reason: str) -> dict:
        return {"barrier": "raised", "lane": lane, "plate": plate, "reason": reason}


class KeyCabinetAdapter:
    """智能钥匙柜：按格口存放/取出，扫描核对。"""

    def cell_for_token(self, token: str) -> str:
        # 牌号 01..NN 映射柜格：行 1..3 × 列 1..8
        n = int(token.split("-")[-1])
        row = (n - 1) // 8 + 1
        col = (n - 1) % 8 + 1
        return f"R{row}-{col:02d}"

    def store(self, token: str, cell: str) -> dict:
        expected = self.cell_for_token(token)
        ok = cell == expected
        return {
            "ok": ok,
            "token": token,
            "cell": cell,
            "expected_cell": expected,
            "message": "格口正确，已入柜" if ok else f"错格！应放入 {expected}",
        }

    def retrieve(self, token: str, cell: str) -> dict:
        expected = self.cell_for_token(token)
        ok = cell == expected
        return {
            "ok": ok,
            "token": token,
            "cell": cell,
            "expected_cell": expected,
            "message": "格口核对通过，可取走" if ok else f"钥匙错格：实际在 {expected}",
        }


def _damage_band(damage_flags: dict[str, str], side: str) -> tuple[str, str]:
    """返回该方位的状态色与标注文字。"""
    if side in damage_flags:
        return "#d23b3b", damage_flags[side]
    return "#3fae6a", "无明显损伤"


def render_photo(trailer_plate: str, trailer_number: str, side: str,
                 stage: str, damage_note: str | None = None) -> str:
    """生成一张外观照片占位图（SVG），返回文件名字符串。"""
    damage_flags = {side: damage_note} if damage_note else {}
    color, label = _damage_band(damage_flags, side)
    stage_cn = "入位检查" if stage == "intake" else "提挂复核"
    side_cn = SIDE_CN[side]

    # 挂车轮廓（侧视为长矩形；前/后为端面；顶为俯视长条）
    if side in ("left", "right"):
        body = '<rect x="70" y="120" width="460" height="120" rx="10" fill="#4a4d52" stroke="#f4c20d" stroke-width="3"/>'
        wheels = ('<circle cx="130" cy="250" r="22" fill="#222"/><circle cx="170" cy="250" r="22" fill="#222"/>'
                  '<circle cx="430" cy="250" r="22" fill="#222"/><circle cx="470" cy="250" r="22" fill="#222"/>')
    elif side == "roof":
        body = '<rect x="70" y="150" width="460" height="90" rx="14" fill="#4a4d52" stroke="#f4c20d" stroke-width="3"/>'
        wheels = ""
    else:  # front / rear 端面
        body = '<rect x="180" y="100" width="240" height="160" rx="10" fill="#4a4d52" stroke="#f4c20d" stroke-width="3"/>'
        wheels = ('<circle cx="230" cy="270" r="20" fill="#222"/><circle cx="370" cy="270" r="20" fill="#222"/>')

    mark = ""
    if side in damage_flags:
        # 损伤处用信号黄底红叉圈出
        mark = (f'<circle cx="300" cy="175" r="26" fill="none" stroke="#d23b3b" stroke-width="5"/>'
                f'<line x1="282" y1="157" x2="318" y2="193" stroke="#d23b3b" stroke-width="5"/>'
                f'<line x1="318" y1="157" x2="282" y2="193" stroke="#d23b3b" stroke-width="5"/>')

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="600" height="320" viewBox="0 0 600 320">
  <rect width="600" height="320" fill="#2f3236"/>
  <rect x="0" y="0" width="600" height="46" fill="#202327"/>
  <text x="20" y="30" fill="#f4c20d" font-family="monospace" font-size="18">{trailer_plate} · {trailer_number}</text>
  <text x="580" y="30" fill="#e8e6e0" font-family="monospace" font-size="15" text-anchor="end">{stage_cn} · {side_cn}部</text>
  {body}{wheels}{mark}
  <rect x="20" y="286" width="560" height="24" rx="4" fill={color!r}/>
  <text x="300" y="303" fill="#ffffff" font-family="monospace" font-size="14" text-anchor="middle">[{side_cn}部] {label}</text>
</svg>"""


class PhotoStore:
    """把外观照片写到本地文件夹。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, trailer_number: str, trailer_plate: str, stage: str,
             side: str, damage_note: str | None = None) -> str:
        fname = f"{trailer_number}_{stage}_{side}.svg"
        path = self.root / fname
        path.write_text(
            render_photo(trailer_plate, trailer_number, side, stage, damage_note),
            encoding="utf-8",
        )
        return fname


def trailer_number_of(conn: sqlite3.Connection, plate: str) -> str:
    row = conn.execute("SELECT number FROM trailers WHERE plate=?", (plate,)).fetchone()
    return row["number"] if row else "UNKNOWN"
