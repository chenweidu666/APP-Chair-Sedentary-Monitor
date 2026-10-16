"""合成 IMU 数据源：离线开发、单元测试、以及"回放调参"用。

它把"椅子状态"翻译成传感器该输出什么，这样在没有椅子的情况下也能验证检测算法：

    gen = SyntheticChair(fs=10.0)
    for s in gen.stream([("empty", 25), ("leaning", 60), ("empty", 40)]):
        ...
"""

from __future__ import annotations

import math
import random
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple

from .occupancy import Sample

# 场景：名字 + 时长（秒）
Scenario = Tuple[str, float]

SCENES = {
    # 椅子空着：只有重力 + 传感器噪声 + 极小的建筑振动
    "empty": dict(tilt_deg=0.0, noise_g=0.0035, gyro_noise=0.4, breath=None, impact=False),
    # 人靠着椅背（典型坐姿）：枕背受压后倾，伴随呼吸微起伏
    "leaning": dict(tilt_deg=6.0, noise_g=0.010, gyro_noise=2.0, breath=(0.30, 0.0060), impact=False),
    # 人坐直、不靠椅背：倾角很小，但有呼吸/心跳/换姿微动
    "upright": dict(tilt_deg=1.0, noise_g=0.008, gyro_noise=1.6, breath=(0.32, 0.0055), impact=False),
    # 刚坐下/起身瞬间：冲击 + 角速度尖峰
    "sit_impact": dict(tilt_deg=0.0, noise_g=0.02, gyro_noise=15.0, breath=None, impact=True, duration=1.2),
    # 空椅上放了个包（会压出倾角但没有生命体征）——已知局限用例
    "object": dict(tilt_deg=3.0, noise_g=0.002, gyro_noise=0.4, breath=None, impact=False),
    # 有人坐下后乱动/挪椅子（有倾角、有明显运动）
    "fidget": dict(tilt_deg=6.0, noise_g=0.03, gyro_noise=8.0, breath=(0.30, 0.006), impact=False),
    # 空椅被碰了一下（有人走过、推桌子、挪椅子）：只有运动，没有倾角与呼吸
    "bump": dict(tilt_deg=0.0, noise_g=0.03, gyro_noise=9.0, breath=None, impact=False),
    # 数据中断前的一帧（配合 heartbeat 使用）
    "still_leaning": dict(tilt_deg=6.0, noise_g=0.002, gyro_noise=0.3, breath=None, impact=False),
}


class SyntheticChair:
    """按场景生成连续的 IMU 采样（默认 10 Hz）。"""

    def __init__(self, fs: float = 10.0, seed: int = 7) -> None:
        self.fs = fs
        self.rng = random.Random(seed)
        self.t = 0.0

    def _gravity_vector(self, tilt_deg: float) -> Tuple[float, float, float]:
        """把重力矢量绕 X 轴转 tilt_deg（模拟椅背后倾）。"""
        rad = math.radians(tilt_deg)
        return 0.0, math.sin(rad), math.cos(rad)

    def samples(self, scene: str, duration: float, t0: Optional[float] = None) -> Iterator[Sample]:
        cfg = dict(SCENES[scene])
        if t0 is not None:
            self.t = t0
        # 用固定的场景种子，保证测试可复现（不要用 hash()：进程间不稳定）
        scene_seed = sum(ord(c) * (i + 3) for i, c in enumerate(scene))
        rng = random.Random(scene_seed * 7919 + 13)
        n = int(duration * self.fs)
        gx0, gy0, gz0 = self._gravity_vector(cfg["tilt_deg"])
        phase = 0.0
        for _ in range(n):
            self.t += 1.0 / self.fs
            ax = gx0 + rng.gauss(0, cfg["noise_g"])
            ay = gy0 + rng.gauss(0, cfg["noise_g"])
            az = gz0 + rng.gauss(0, cfg["noise_g"])
            if cfg.get("breath"):
                hz, amp = cfg["breath"]
                phase += 2 * math.pi * hz / self.fs
                # 呼吸沿着重力方向做微起伏（椅背受压周期性变化）
                ax += amp * math.sin(phase) * gx0
                ay += amp * math.sin(phase) * gy0
                az += amp * math.sin(phase) * gz0
            if cfg.get("impact") and int(self.t / self.fs) % int(10 * self.fs) == 0:
                ax += 0.18 * gx0
                ay += 0.18 * gy0
                az += 0.18 * gz0
            yield Sample(t=self.t, ax=ax, ay=ay, az=az,
                         gx=rng.gauss(0, cfg["gyro_noise"]),
                         gy=rng.gauss(0, cfg["gyro_noise"]),
                         gz=rng.gauss(0, cfg["gyro_noise"]))

    def stream(self, scenario: Sequence[Scenario]) -> Iterator[Sample]:
        """按场景序列连续输出（场景之间不重置时间轴）。"""
        for scene, duration in scenario:
            yield from self.samples(scene, duration)


def replay(samples: Iterable[Sample], detector) -> List[Tuple[float, str]]:
    """回放数据并记录状态变化时间点，便于断言与画图。"""
    changes: List[Tuple[float, str]] = []
    last = None
    for s in samples:
        state = detector.update(s)
        if state != last:
            changes.append((round(s.t, 2), state))
            last = state
    return changes
