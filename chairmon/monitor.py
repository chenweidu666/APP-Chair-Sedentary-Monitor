"""采集与业务循环：BLE 连接 → 占用检测 → 久坐提醒 → 电量。

BLE 部分复用 APP-WT9011DCL-BT50-Linux 的 `wt9011` 库（同一套协议实现），
本模块只负责：自动连接/自动重连、把数据喂给检测器、按策略提醒、定期读电量。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable, Optional

from . import notifier
from .config import AppConfig
from .model import STATE_EMPTY, STATE_OCCUPIED, STATE_UNKNOWN, config_for
from .occupancy import OccupancyDetector
from .sedentary import Reminder, SedentaryConfig, SedentaryTracker


@dataclass
class Status:
    """对界面/日志暴露的一次状态快照。"""

    state: str = STATE_UNKNOWN
    connected: bool = False
    address: str = ""
    battery_percent: Optional[int] = None
    battery_voltage: Optional[float] = None
    sitting_minutes: float = 0.0
    next_reminder_min: Optional[float] = None
    confidence: float = 0.0
    tilt_deg: float = 0.0
    breath_hz: float = 0.0
    breath_amp: float = 0.0
    motion_rms: float = 0.0
    reminders: int = 0
    breaks: int = 0
    last_error: str = ""

    def summary(self) -> str:
        state_cn = {STATE_OCCUPIED: "有人", STATE_EMPTY: "无人", STATE_UNKNOWN: "未知"}[self.state]
        battery = f"{self.battery_percent}%" if self.battery_percent is not None else "—"
        nxt = f"{self.next_reminder_min:.1f} min" if self.next_reminder_min else "—"
        return (f"{state_cn}  已坐 {self.sitting_minutes:.1f} min（下次提醒 {nxt}）"
                f"  电量 {battery}  倾角 {self.tilt_deg:.1f}°  呼吸 {self.breath_hz:.2f}Hz")


class ChairMonitor:
    """把 BLE 数据流接到检测器 + 久坐策略上（异步）。"""

    def __init__(self, app_cfg: Optional[AppConfig] = None,
                 on_status: Optional[Callable[[Status], None]] = None,
                 on_log: Optional[Callable[[str], None]] = None,
                 client_factory: Optional[Callable[[], object]] = None) -> None:
        self.app = app_cfg or AppConfig.load()
        self.on_status = on_status
        self.on_log = on_log

        det_cfg = config_for(self.app.mount, **self.app.overrides)
        self.detector = OccupancyDetector(det_cfg)
        self.tracker = SedentaryTracker(SedentaryConfig(
            sitting_limit_min=self.app.sitting_limit_min,
            repeat_min=self.app.repeat_min,
            standing_reset_min=self.app.standing_reset_min))
        self.status = Status(address=self.app.address)
        self._client = None
        self._client_factory = client_factory      # 便于测试注入假客户端
        self._last_battery_t = 0.0
        self._running = False

    # ---------------------------------------------------------------- 采集回调
    def _log(self, msg: str) -> None:
        if self.on_log:
            self.on_log(msg)
        else:
            print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    def handle_sample(self, s) -> None:
        """收到一帧数据（wt9011.Sample 或 chairmon.model.Sample 都可以）。"""
        from .model import Sample as ChairSample

        sample = ChairSample(t=getattr(s, "t", time.time()), ax=s.ax, ay=s.ay, az=s.az,
                             gx=s.gx, gy=s.gy, gz=s.gz, roll=getattr(s, "roll", 0.0),
                             pitch=getattr(s, "pitch", 0.0), yaw=getattr(s, "yaw", 0.0))
        prev_state = self.detector.state
        state = self.detector.update(sample)
        if state != prev_state:
            self._log(f"状态变化：{prev_state} → {state}"
                      f"（倾角 {self.detector.features.tilt_deg:.1f}°，"
                      f"呼吸 {'有' if self.detector.features.breath_ok else '无'}）")
        self._sync_status()

    def _sync_status(self) -> None:
        f = self.detector.features
        self.status.state = self.detector.state
        self.status.confidence = f.confidence
        self.status.tilt_deg = f.tilt_deg
        self.status.breath_hz = f.breath_hz
        self.status.breath_amp = f.breath_amp
        self.status.motion_rms = f.motion_rms
        if self.on_status:
            self.on_status(self.status)

    def tick(self, now: Optional[float] = None) -> Optional[Reminder]:
        """主循环里的定时调用：心跳判掉线 + 久坐计时 + 提醒。"""
        now = time.time() if now is None else now
        self.detector.heartbeat(now)
        reminder = self.tracker.update(self.detector.state, now)
        self.status.sitting_minutes = self.tracker.sitting_minutes
        nxt = self.tracker.next_reminder_in()
        self.status.next_reminder_min = (nxt / 60.0) if nxt else None
        self.status.reminders = self.tracker.stats.reminders
        self.status.breaks = self.tracker.stats.breaks
        if reminder:
            self._log(f"提醒：{reminder.text}")
            if self.app.notify:
                notifier.notify_reminder(reminder.sitting_minutes, reminder.text,
                                         self.app.notification_urgency)
        self._sync_status()
        return reminder

    # ---------------------------------------------------------------- BLE
    async def run(self, reconnect_wait: float = 5.0, simulator: bool = False) -> None:
        """主循环：连接 → 收数据；掉线自动重连；永不退出（除取消）。"""
        self._running = True
        while self._running:
            try:
                await self._connect(simulator)
                self._log("已连接，开始检测（离开椅子会自动重连/继续计时）")
                await self._serve()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.status.last_error = f"{type(exc).__name__}: {exc}"
                self.status.connected = False
                self._log(f"连接/采集异常：{self.status.last_error}，{reconnect_wait:.0f} 秒后重试")
            finally:
                await self._safe_disconnect()
            if self._running:
                await asyncio.sleep(reconnect_wait)

    async def _connect(self, simulator: bool) -> None:
        if self._client_factory is not None:
            self._client = self._client_factory()
        elif simulator:
            from wt9011.sim import SimulatedWitClient
            self._client = SimulatedWitClient(on_sample=self.handle_sample,
                                              on_status=self._log)
        else:
            from wt9011.client import WitClient
            self._client = WitClient(address=self.app.address or None,
                                     on_sample=self.handle_sample,
                                     on_status=self._log,
                                     on_disconnect=self._on_disconnect)
        await self._client.connect(self.app.address or None)
        self.status.connected = True
        addr = getattr(self._client, "address", None) or self.app.address
        self.status.address = addr or ""
        if addr and addr != self.app.address and ":" in addr:
            self.app.address = addr
            self.app.save()
        if self.app.notify:
            notifier.notify_connected(self.status.address)
        await self._read_battery()

    def _on_disconnect(self, manual: bool) -> None:
        self.status.connected = False
        if not manual:
            self._log("BLE 掉线（会自动重连）")

    async def _serve(self) -> None:
        """周期任务：心跳/久坐计时/电量轮询，直到连接断开。"""
        while self._running:
            await asyncio.sleep(1.0)
            self.tick()
            client = self._client
            if client is None or not getattr(client, "is_connected", False):
                return
            now = time.time()
            if now - self._last_battery_t >= self.app.poll_battery_min * 60:
                await self._read_battery()

    async def _read_battery(self) -> None:
        client = self._client
        if client is None:
            return
        try:
            info = await client.read_battery()
        except Exception:  # noqa: BLE001
            return
        self._last_battery_t = time.time()
        self.status.battery_percent = info["percent"]
        self.status.battery_voltage = info["voltage"]
        if (self.app.notify and info["percent"] and info["percent"] <= self.app.low_battery_percent
                and info["raw"] > 0):
            notifier.notify_low_battery(info["percent"])
        self._sync_status()

    async def _safe_disconnect(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        self._running = False

    # ---------------------------------------------------------------- 人工操作
    def mark_empty_now(self) -> None:
        """人工声明"椅子现在是空的"（挪动过传感器/重新安装后调一次）。"""
        self.detector.mark_empty()
        self._log("已把当前姿态作为「空椅」基线")

    def reset_sedentary(self) -> None:
        self.tracker.reset_all()
        self._log("久坐计时已清零")
