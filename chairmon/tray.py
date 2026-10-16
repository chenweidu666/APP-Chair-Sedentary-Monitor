"""系统托盘界面（PySide6）：状态 / 已坐时长 / 电量 / 暂停 / 清零 / 退出。

BLE 事件循环跑在独立线程里（与 APP-WT9011DCL-BT50-Linux 的界面同一套路），
托盘图标随状态变色：无人=灰，有人=绿，久坐超限=红，未连接=半透明。
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from typing import Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from .config import AppConfig
from .model import STATE_EMPTY, STATE_OCCUPIED, STATE_UNKNOWN
from .monitor import ChairMonitor, Status

COLORS = {
    STATE_EMPTY: "#7f8c8d",
    STATE_OCCUPIED: "#2ecc71",
    STATE_UNKNOWN: "#b7950b",
}


def make_icon(color: str, alert: bool = False) -> QIcon:
    """画一个简单的圆形图标（避免依赖图片资源）。"""
    size = 64
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(QColor("#ffffff" if not alert else "#ff0000"))
    painter.drawEllipse(6, 6, size - 12, size - 12)
    if alert:
        painter.setPen(QColor("#ffffff"))
        font = painter.font()
        font.setBold(True)
        font.setPointSize(22)
        painter.setFont(font)
        painter.drawText(pix.rect(), Qt.AlignCenter, "!")
    painter.end()
    return QIcon(pix)


class MonitorThread(threading.Thread):
    """在后台线程跑 ChairMonitor 的 asyncio 事件循环。"""

    def __init__(self, app_cfg: AppConfig) -> None:
        super().__init__(daemon=True)
        self.app_cfg = app_cfg
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.monitor = ChairMonitor(app_cfg, on_status=self.on_status, on_log=self.on_log)
        self.on_status_signal = None
        self.on_log_signal = None

    def run(self) -> None:
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.monitor.run())
        except asyncio.CancelledError:
            pass
        finally:
            self.loop.close()

    def call(self, fn) -> None:
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(fn)


class TrayApp(QObject):
    statusSignal = Signal(object)
    logSignal = Signal(str)

    def __init__(self, args) -> None:
        super().__init__()
        self.app_cfg = AppConfig.load()
        if getattr(args, "addr", None):
            self.app_cfg.address = args.addr
        if getattr(args, "mount", None):
            self.app_cfg.mount = args.mount

        self.tray = QSystemTrayIcon(make_icon(COLORS[STATE_UNKNOWN]))
        self.tray.setToolTip("椅子久坐检测：启动中…")
        menu = QMenu()
        self.act_status = QAction("状态：启动中…", menu)
        self.act_status.setEnabled(False)
        self.act_sitting = QAction("已坐：—", menu)
        self.act_sitting.setEnabled(False)
        self.act_battery = QAction("电量：—", menu)
        self.act_battery.setEnabled(False)
        menu.addAction(self.act_status)
        menu.addAction(self.act_sitting)
        menu.addAction(self.act_battery)
        menu.addSeparator()
        self.act_pause = QAction("暂停提醒 30 分钟", menu)
        self.act_pause.triggered.connect(self._pause)
        menu.addAction(self.act_pause)
        self.act_reset = QAction("久坐计时清零", menu)
        self.act_reset.triggered.connect(lambda: self.thread.call(self.thread.monitor.reset_sedentary))
        menu.addAction(self.act_reset)
        self.act_mark = QAction("把当前姿态记为「空椅」", menu)
        self.act_mark.triggered.connect(lambda: self.thread.call(self.thread.monitor.mark_empty_now))
        menu.addAction(self.act_mark)
        menu.addSeparator()
        act_quit = QAction("退出", menu)
        act_quit.triggered.connect(self._quit)
        menu.addAction(act_quit)
        self.menu = menu
        self.tray.setContextMenu(menu)
        self.tray.show()

        self.thread = MonitorThread(self.app_cfg)
        self.thread.on_status_signal = self.statusSignal
        self.thread.on_log_signal = self.logSignal
        # 后台线程直接发 Qt 信号（跨线程自动排队）
        self.thread.monitor.on_status = lambda st: self.statusSignal.emit(st)
        self.thread.monitor.on_log = lambda msg: self.logSignal.emit(msg)
        self.thread.start()

        self.statusSignal.connect(self._on_status)
        self.logSignal.connect(lambda msg: print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True))
        self.paused_until = 0.0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._refresh_actions)
        self.timer.start(1000)

    # ---------------------------------------------------------------- 槽
    def _on_status(self, status: Status) -> None:
        color = COLORS.get(status.state, COLORS[STATE_UNKNOWN])
        alert = (status.sitting_minutes >= self.app_cfg.sitting_limit_min
                 and status.state == STATE_OCCUPIED)
        self.tray.setIcon(make_icon(color, alert=alert))
        state_cn = {STATE_OCCUPIED: "有人", STATE_EMPTY: "无人", STATE_UNKNOWN: "未知"}[status.state]
        conn = "已连接" if status.connected else "未连接"
        self.act_status.setText(f"状态：{state_cn}（{conn}）")
        self.act_sitting.setText(f"已坐：{status.sitting_minutes:.1f} 分钟"
                                 + (f"（提醒 {status.next_reminder_min:.0f} 分钟后）"
                                    if status.next_reminder_min else ""))
        bat = f"{status.battery_percent}%（{status.battery_voltage:.2f} V）" \
            if status.battery_percent is not None else "—"
        self.act_battery.setText(f"电量：{bat}")
        self.tray.setToolTip(f"椅子久坐检测 · {state_cn} · 已坐 {status.sitting_minutes:.0f} min · "
                             f"电量 {status.battery_percent if status.battery_percent is not None else '—'}"
                             f"{'%' if status.battery_percent is not None else ''}")

    def _pause(self) -> None:
        self.paused_until = time.time() + 30 * 60
        self.act_pause.setText("恢复提醒")
        self.app_cfg.notify = False
        self._log("已暂停提醒 30 分钟")

    def _refresh_actions(self) -> None:
        if self.paused_until and time.time() > self.paused_until:
            self.paused_until = 0.0
            self.app_cfg.notify = True
            self.act_pause.setText("暂停提醒 30 分钟")
            self._log("提醒已恢复")

    def _log(self, msg: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    def _quit(self) -> None:
        self.thread.call(self.thread.monitor.stop)
        QTimer.singleShot(300, QApplication.instance().quit)


def run_tray(args) -> int:
    app = QApplication(sys.argv[:1] + ["chair-monitor"])
    app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("当前桌面没有系统托盘（Wayland/GNOME 需装 AppIndicator 扩展）；"
              "可改用无界面模式：chair-monitor run", file=sys.stderr)
    tray = TrayApp(args)
    return app.exec()
