"""久坐计时与提醒策略（纯算法，可单测）。

规则（都可配）：
- 连续坐满 `sitting_limit_min`（默认 45 min）→ 第一次提醒；
- 之后每 `repeat_min`（默认 15 min）再提醒一次；
- 起身 **累计** 满 `standing_reset_min`（默认 2 min）→ 连续久坐清零，记一次"活动"；
- 起身但不足 2 min（比如去倒水 40 秒）→ **不清零**，只把计时暂停，避免"抖一下就把 45 分钟清掉"；
- 数据中断（UNKNOWN）→ 暂停计时（不清零、也不算久坐），因为不知道人到底在不在。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import List, Optional

from .model import STATE_EMPTY, STATE_OCCUPIED, STATE_UNKNOWN


@dataclass
class SedentaryConfig:
    sitting_limit_min: float = 45.0     # 首次提醒
    repeat_min: float = 15.0            # 之后重复提醒间隔
    standing_reset_min: float = 2.0     # 起身多久算"活动过"（清零）
    min_sitting_min: float = 1.0        # 少于这么久不算一次久坐（防抖动统计）


@dataclass
class Reminder:
    at: float                # 触发时间戳
    sitting_minutes: float   # 触发时的连续久坐时长
    kind: str                # "first" | "repeat"
    text: str


@dataclass
class SedentaryStats:
    continuous_minutes: float = 0.0   # 当前连续久坐（含暂停累计）
    today_sitting_minutes: float = 0.0
    breaks: int = 0                   # 有效活动次数（起身≥阈值）
    reminders: int = 0
    longest_minutes: float = 0.0
    last_reminder_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


class SedentaryTracker:
    def __init__(self, config: Optional[SedentaryConfig] = None) -> None:
        self.cfg = config or SedentaryConfig()
        self.stats = SedentaryStats()
        self._state = STATE_UNKNOWN
        self._state_since = 0.0
        self._last_t: Optional[float] = None
        self._sitting_accum = 0.0     # 当前这轮连续久坐累计
        self._standing_accum = 0.0    # 本轮起身累计（用于判断是否有效活动）
        self._next_reminder_at = 0.0
        self._paused = False

    # ---------------------------------------------------------------- 对外
    @property
    def sitting_minutes(self) -> float:
        return self._sitting_accum / 60.0

    @property
    def sitting_limit_reached(self) -> bool:
        return self._sitting_accum >= self.cfg.sitting_limit_min * 60

    def next_reminder_in(self) -> Optional[float]:
        """距离下一次提醒还有多少秒（None = 当前不需要提醒）。"""
        if self._next_reminder_at <= 0 or self._paused:
            return None
        return max(0.0, self._next_reminder_at - (self._last_t or 0.0))

    def update(self, state: str, now: Optional[float] = None) -> Optional[Reminder]:
        """喂入占用状态，返回需要弹出的提醒（没有则 None）。"""
        now = time.time() if now is None else now
        if self._last_t is None:
            self._last_t = now
            self._state = state
            self._state_since = now
            return None
        dt = max(0.0, now - self._last_t)
        self._last_t = now

        if state != self._state:
            self._on_state_change(self._state, state, now)
            self._state = state
            self._state_since = now

        if state == STATE_OCCUPIED:
            self._sitting_accum += dt
            self.stats.today_sitting_minutes += dt / 60.0
            self.stats.longest_minutes = max(self.stats.longest_minutes, self.sitting_minutes)
            self._paused = False
            self._standing_accum = 0.0
            if self._next_reminder_at and now >= self._next_reminder_at:
                return self._fire(now)
            if not self._next_reminder_at and self._sitting_accum >= self.cfg.sitting_limit_min * 60:
                return self._fire(now)
        elif state == STATE_EMPTY:
            # 起身累计：暂停久坐计时
            self._standing_accum += dt
            self._paused = True
            if self._standing_accum >= self.cfg.standing_reset_min * 60:
                self._end_session()
        else:  # UNKNOWN：数据中断，暂停但不判空
            self._paused = True

        self.stats.continuous_minutes = self.sitting_minutes
        return None

    def reset_all(self) -> None:
        self.stats = SedentaryStats()
        self._sitting_accum = 0.0
        self._standing_accum = 0.0
        self._next_reminder_at = 0.0
        self._paused = False

    # ---------------------------------------------------------------- 内部
    def _on_state_change(self, old: str, new: str, now: float) -> None:
        # 从"有人"离开时先不改总时长，交给起身累计判定（避免 5 秒起身就清零）
        return

    def _fire(self, now: float) -> Reminder:
        first = self.stats.reminders == 0
        self.stats.reminders += 1
        self.stats.last_reminder_at = now
        self._next_reminder_at = now + self.cfg.repeat_min * 60
        minutes = self.sitting_minutes
        text = (f"已经连续坐了 {minutes:.0f} 分钟，起来活动一下"
                if first else f"又坐了 {self.cfg.repeat_min:.0f} 分钟（本轮累计 {minutes:.0f} 分钟），该起身了")
        return Reminder(at=now, sitting_minutes=minutes, kind="first" if first else "repeat", text=text)

    def _end_session(self) -> None:
        """一次有效活动：结束本轮久坐。"""
        if self.sitting_minutes >= self.cfg.min_sitting_min:
            self.stats.breaks += 1
        self._sitting_accum = 0.0
        self._standing_accum = 0.0
        self._next_reminder_at = 0.0
        self._paused = False
        self.stats.continuous_minutes = 0.0
