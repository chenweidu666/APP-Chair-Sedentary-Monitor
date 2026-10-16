"""桌面通知（notify-send / D-Bus），失败时退回标准输出。"""

from __future__ import annotations

import shutil
import subprocess
from typing import Optional


def notify(title: str, body: str, urgency: str = "normal",
           icon: str = "appointment-soon", timeout_ms: int = 20000) -> bool:
    """弹一条桌面通知；返回是否成功。"""
    if shutil.which("notify-send"):
        try:
            subprocess.run(
                ["notify-send", "-u", urgency, "-i", icon, "-t", str(timeout_ms), title, body],
                check=False, timeout=10, capture_output=True)
            return True
        except (OSError, subprocess.SubprocessError):
            pass
    # 无桌面环境（例如 systemd 服务里没有 DISPLAY）：至少记到日志
    print(f"[通知] {title} —— {body}", flush=True)
    return False


def notify_reminder(minutes: float, text: str, urgency: str = "normal") -> bool:
    return notify(f"该起来了（已坐 {minutes:.0f} 分钟）", text, urgency=urgency,
                  icon="appointment-soon")


def notify_low_battery(percent: int) -> bool:
    return notify("椅子传感器电量低", f"电量 {percent}%，请给模块充电", urgency="normal",
                  icon="battery-low")


def notify_connected(address: str, name: Optional[str] = None) -> bool:
    return notify("椅子传感器已连接", f"{name or ''} {address}".strip(), urgency="low",
                  icon="bluetooth-active")
