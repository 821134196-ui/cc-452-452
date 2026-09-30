"""平台设置：文件路径与超时阈值（秒）。"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("YP_DATA", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "yard.db"
PHOTO_DIR = DATA_DIR / "photos"

# 等待超时阈值（秒）。高峰样例与测试可调小。
QUEUE_TIMEOUT_S = int(os.environ.get("YP_QUEUE_TIMEOUT", "300"))   # 待卸挂排队
PICKUP_TIMEOUT_S = int(os.environ.get("YP_PICKUP_TIMEOUT", "420"))  # 待提挂
TICK_S = int(os.environ.get("YP_TICK", "5"))

ROLES = {
    "guard": "门岗",
    "dispatcher": "调度员",
    "key_clerk": "钥匙员",
    "inspector": "查验员",
    "supervisor": "值班主管",
}

# 各操作允许的角色
PERMISSIONS = {
    "check_in": {"guard"},
    "assign_stall": {"dispatcher"},
    "unhitch": {"dispatcher"},
    "intake_check": {"inspector"},
    "park": {"dispatcher"},
    "chock": {"dispatcher"},
    "store_keys": {"key_clerk"},
    "start_pickup": {"dispatcher"},
    "verify_pickup": {"inspector"},
    "retrieve_keys": {"key_clerk"},
    "hitch": {"dispatcher"},
    "cannot_pick": {"dispatcher"},
    "request_abnormal_exit": {"guard"},
    "decide_exception": {"supervisor"},
    "exit_confirm": {"guard"},
}
