"""久坐检测：状态模型、特征与判据参数。

核心问题：**怎么判断"有人坐 / 没人坐"**。

判据（三种，按可靠性排序）：

1. **倾角偏移 Δθ**：坐着时椅背/座板受力变形，姿态相对"空椅基线"稳定偏转几度。
   空椅只有温漂（<0.5°），因此 Δθ 是最好的主判据 —— 但它有漏判场景：
   人坐得笔直完全不靠椅背时 Δθ≈0。
2. **呼吸/微动**：与椅背或座板接触的人，会有 0.15–0.55 Hz 的周期性微起伏
   （呼吸、心跳、换姿）。这是"有人"的本质证据，恰好补上第 1 条的漏判。
   空椅只有传感器噪声与建筑振动，没有这个频段的周期性成分。
3. **落座/起身瞬态**：坐下瞬间的冲击（|acc| 偏离 1 g、角速度尖峰）与起身时的反向瞬态，
   用来快速触发状态切换，而不是只靠持续时间。

工程要点：**迟滞 + 驻留时间 + 基线自学习**。
进入门槛高（避免误报），退出门槛低但要求持续几十秒（避免人短暂不动就判空）。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional

# 状态
STATE_UNKNOWN = "UNKNOWN"      # 启动自学习 / 数据中断
STATE_EMPTY = "EMPTY"          # 没人
STATE_OCCUPIED = "OCCUPIED"    # 有人


@dataclass
class Sample:
    """与传输层解耦的一帧数据（wt9011 的 Sample 可直接映射过来）。"""

    t: float
    ax: float = 0.0
    ay: float = 0.0
    az: float = 0.0
    gx: float = 0.0
    gy: float = 0.0
    gz: float = 0.0
    roll: float = 0.0
    pitch: float = 0.0
    yaw: float = 0.0


@dataclass
class OccupancyConfig:
    """检测参数。默认值针对**椅背安装**（mount='backrest'）调；座板安装见 MOUNT_PRESETS。"""

    mount: str = "backrest"

    # --- 自学习与基线
    learn_seconds: float = 20.0        # 启动后用于学习"空椅基线"的时长
    baseline_alpha: float = 0.01       # 空椅基线慢速自适应（温漂/微移）
    tilt_alpha: float = 0.05           # 姿态 EMA（≈2 s @10Hz）

    # --- 判据 1：倾角偏移（度）
    tilt_on_deg: float = 2.5           # 进入"有人"
    tilt_off_deg: float = 1.2          # 退出"有人"

    # --- 判据 2：运动能量
    motion_alpha: float = 0.3
    motion_on_g: float = 0.012         # 加速度残差 RMS（g）
    motion_off_g: float = 0.005
    gyro_on_dps: float = 1.5           # 角速度均值（°/s）

    # --- 判据 3：呼吸周期
    breathing_min_hz: float = 0.15
    breathing_max_hz: float = 0.55
    breathing_window_s: float = 25.0   # 需要足够长的窗口才能分辨 0.3 Hz
    breathing_min_amp_g: float = 0.0020
    breathing_peak_ratio: float = 3.0  # 峰值功率 / 带内中位功率
    breathing_recalc_s: float = 0.5

    # --- 瞬态
    impact_g: float = 0.06             # |acc|-1g 的瞬态阈值
    impact_gyro_dps: float = 12.0

    # --- 状态机
    enter_dwell_s: float = 2.0         # 判据成立后需持续多久才认为"有人"
    exit_dwell_s: float = 30.0         # "没人"证据需持续多久才认为真的没人
    quick_exit_s: float = 10.0         # 起身瞬态 + 无呼吸时的提前判空确认时间
    evidence_hold_s: float = 5.0       # 证据中断多久才把累计清零（抗单帧毛刺）
    stale_gap_s: float = 3.0           # 超过这么久没数据 → UNKNOWN（计时不重置）

    def to_dict(self) -> dict:
        return asdict(self)


# 椅子不同安装位置的预设
MOUNT_PRESETS: Dict[str, dict] = {
    # 椅背：靠上去变形明显、呼吸传导好；倾角是主判据，呼吸补"坐直不靠"的漏判
    "backrest": dict(tilt_on_deg=2.5, tilt_off_deg=1.2, motion_on_g=0.012, motion_off_g=0.005,
                     breathing_min_amp_g=0.0020, enter_dwell_s=2.0, exit_dwell_s=30.0),
    # 座板（下方中央）：受压变形 → 倾角变化更大，呼吸传导略弱
    "seat": dict(tilt_on_deg=2.0, tilt_off_deg=1.0, motion_on_g=0.010, motion_off_g=0.004,
                 breathing_min_amp_g=0.0025, enter_dwell_s=2.0, exit_dwell_s=45.0),
}


def config_for(mount: str = "backrest", **overrides) -> OccupancyConfig:
    cfg = OccupancyConfig(mount=mount)
    for key, value in MOUNT_PRESETS.get(mount, {}).items():
        setattr(cfg, key, value)
    for key, value in overrides.items():
        if not hasattr(cfg, key):
            raise AttributeError(f"未知配置项：{key}")
        setattr(cfg, key, value)
    return cfg
