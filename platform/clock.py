"""可注入的时钟，便于高峰样例与测试控制时间。"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional


class Clock:
    def __init__(self, fixed: Optional[datetime] = None):
        self._fixed = fixed
        self._offset = timedelta(0)

    def now(self) -> datetime:
        if self._fixed is not None:
            return self._fixed
        return datetime.now() + self._offset

    def set(self, dt: datetime) -> None:
        self._fixed = dt

    def advance(self, minutes: float = 0, seconds: float = 0) -> datetime:
        self._fixed = self.now() + timedelta(minutes=minutes, seconds=seconds)
        return self._fixed


def iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def parse(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
