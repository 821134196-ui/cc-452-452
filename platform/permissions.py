"""业务错误与角色权限矩阵。"""
from __future__ import annotations


class ServiceError(Exception):
    def __init__(self, code: str, message: str, http: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http = http


# 角色 -> 可执行动作
PERMISSIONS: dict[str, frozenset[str]] = {
    "gate": frozenset({
        "arrive", "exit_gate",
    }),
    "dispatcher": frozenset({
        "assign_slot", "unhitch", "chock",
        "start_pickup", "hitch", "cannot_pickup",
    }),
    "keyclerk": frozenset({
        "deposit_key", "release_key",
    }),
    "inspector": frozenset({
        "verify_seal", "inspect_appearance",
    }),
    "supervisor": frozenset({
        "decide_approval",
    }),
}


def can(role: str, action: str) -> bool:
    return action in PERMISSIONS.get(role, frozenset())


def require(role: str, action: str) -> None:
    if not can(role, action):
        raise ServiceError(
            "forbidden",
            f"角色“{role}”无权执行 {action}，该操作需要对应岗位权限",
            http=403,
        )
