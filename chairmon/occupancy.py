"""占用检测器：特征提取 + 迟滞状态机（纯算法，无外部依赖，可单测）。

用法：
    det = OccupancyDetector(config_for("backrest"))
    for s in samples:                 # s.t 为单调递增秒
        state = det.update(s)
        print(state, det.features)
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional, Tuple

from .model import (STATE_EMPTY, STATE_OCCUPIED, STATE_UNKNOWN, OccupancyConfig, Sample,
                    config_for)


@dataclass
class Features:
    """当前特征值（用于显示与调参）。"""

    tilt_deg: float = 0.0          # 相对空椅基线的倾角偏移
    tilt_axis: Tuple[float, float, float] = (0.0, 0.0, 0.0)  # 各轴差值（调试用）
    motion_rms: float = 0.0        # 加速度残差 RMS（g）
    gyro_mean: float = 0.0         # 角速度幅值均值（°/s）
    breath_amp: float = 0.0        # 呼吸幅度估计（g）
    breath_hz: float = 0.0         # 呼吸主频（Hz），0 表示未检出
    breath_ok: bool = False        # 是否判为存在周期性呼吸
    impact: float = 0.0            # 最近瞬态的强度（g）
    seconds_in_state: float = 0.0
    confidence: float = 0.0        # 0..1，越大越确信"有人"

    def to_dict(self) -> Dict[str, object]:
        d = dict(self.__dict__)
        d["tilt_axis"] = [round(v, 4) for v in self.tilt_axis]
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()}


def _goertzel_band_peak(signal: List[float], fs: float, fmin: float, fmax: float,
                        step: float = 0.02) -> Tuple[float, float, float]:
    """在 [fmin, fmax] 上做 Goertzel 扫频，返回 (峰值频率, 峰值功率, 带内中位功率)。"""
    n = len(signal)
    if n < 8:
        return 0.0, 0.0, 0.0
    mean = sum(signal) / n
    x = [v - mean for v in signal]
    powers: List[Tuple[float, float]] = []
    f = fmin
    while f <= fmax + 1e-9:
        w = 2.0 * math.pi * f / fs
        coeff = 2.0 * math.cos(w)
        s_prev = s_prev2 = 0.0
        for v in x:
            s = v + coeff * s_prev - s_prev2
            s_prev2 = s_prev
            s_prev = s
        power = s_prev2 * s_prev2 + s_prev * s_prev - coeff * s_prev * s_prev2
        powers.append((f, power))
        f += step
    best_f, best_p = max(powers, key=lambda item: item[1])
    sorted_p = sorted(p for _f, p in powers)
    median_p = sorted_p[len(sorted_p) // 2]
    return best_f, best_p, median_p


class OccupancyDetector:
    """占用检测状态机。"""

    def __init__(self, config: Optional[OccupancyConfig] = None, now: float = 0.0) -> None:
        self.cfg = config or config_for("backrest")
        self.state = STATE_UNKNOWN
        self.features = Features()

        # 基线（空椅）
        self._acc_ema: Optional[Tuple[float, float, float]] = None
        self._g_ref: Optional[Tuple[float, float, float]] = None
        self._worst_tilt_during_learn = 0.0

        # 运动统计
        self._motion_sq = 0.0
        self._gyro_mean = 0.0
        self._noise_rms: float = 0.003          # 空椅噪声底（学习后确定）
        self._noise_gyro: float = 0.5           # 空椅角速度噪声底
        self._noise_breath: float = 0.0         # 呼吸幅度噪声底

        # 呼吸：沿重力方向的分量序列
        fs = 10.0  # 名义采样率，首个样本到达后会按实际时间戳更新
        self._fs = fs
        self._grav_samples: Deque[Tuple[float, float]] = deque(maxlen=4096)
        self._last_breath_calc = 0.0
        self._breath: Tuple[float, float, bool] = (0.0, 0.0, False)

        # 瞬态
        self._impact = 0.0
        self._impact_t = -1e9
        self._last_t: Optional[float] = None
        self._first_t: Optional[float] = None

        # 状态机计时
        self._state_since = now
        self._sit_accum = 0.0           # "有人"证据累计（带容错，单帧毛刺不清零）
        self._empty_accum = 0.0         # "没人"证据累计
        self._sit_last_t: Optional[float] = None
        self._empty_last_t: Optional[float] = None
        self._last_update_t = now
        self._last_dt = 0.1

    # ---------------------------------------------------------------- 对外接口
    @property
    def is_occupied(self) -> bool:
        return self.state == STATE_OCCUPIED

    def mark_empty(self, now: Optional[float] = None) -> None:
        """人工声明"现在没人"，立即用当前姿态重建基线（安装/挪动后调用）。"""
        if self._acc_ema is not None:
            self._g_ref = self._acc_ema
        self.state = STATE_EMPTY
        self._state_since = now if now is not None else (self._last_t or 0.0)
        self._sit_accum = 0.0
        self._empty_accum = 0.0
        self._sit_last_t = None
        self._empty_last_t = None

    def update(self, sample: Sample) -> str:
        """喂一帧数据，返回当前状态。"""
        cfg = self.cfg
        t = sample.t
        prev_t = self._last_update_t
        self._last_dt = max(0.0, min(t - prev_t, 1.0)) if prev_t else 0.1
        self._last_update_t = t

        # --- 采样率估计（前若干帧）
        if self._first_t is None:
            self._first_t = t
        if self._last_t is not None and t > self._last_t:
            dt = t - self._last_t
            if 0.001 < dt < 1.0:
                self._fs = 0.9 * self._fs + 0.1 * (1.0 / dt)
        self._last_t = t

        acc = (sample.ax, sample.ay, sample.az)
        acc_mag = math.sqrt(acc[0] ** 2 + acc[1] ** 2 + acc[2] ** 2)
        gyro_mag = math.sqrt(sample.gx ** 2 + sample.gy ** 2 + sample.gz ** 2)

        # --- 姿态 EMA（慢速，滤掉呼吸/微动）
        if self._acc_ema is None:
            self._acc_ema = acc
        else:
            a = cfg.tilt_alpha
            self._acc_ema = tuple((1 - a) * old + a * new for old, new in zip(self._acc_ema, acc))

        # --- 运动能量：去重力残差的 RMS（EMA 近似）
        # 学习阶段用"相对当前重力估计"的残差，与运行期（相对基线）口径一致，
        # 否则学到的噪声底偏小，退出"有人"的阈值会卡住。
        ref = self._g_ref if self._g_ref is not None else self._acc_ema
        if ref is not None:
            residual = math.sqrt(sum((c - b) ** 2 for c, b in zip(acc, ref)))
        else:
            residual = abs(acc_mag - 1.0)
        self._motion_sq = (1 - cfg.motion_alpha) * self._motion_sq + cfg.motion_alpha * residual ** 2
        self._gyro_mean = (1 - cfg.motion_alpha) * self._gyro_mean + cfg.motion_alpha * gyro_mag

        # --- 瞬态（落座/起身/磕碰）
        impact = max(abs(acc_mag - 1.0), gyro_mag / 200.0)
        if abs(acc_mag - 1.0) > cfg.impact_g or gyro_mag > cfg.impact_gyro_dps:
            self._impact = impact
            self._impact_t = t

        # --- 自学习基线
        elapsed = t - (self._first_t or t)
        learning = elapsed < cfg.learn_seconds
        if learning:
            tilt_now = self._angle_to_ref()
            self._worst_tilt_during_learn = max(self._worst_tilt_during_learn, tilt_now)
        elif self._g_ref is None:
            self._g_ref = self._acc_ema
            # 用学习期间测到的噪声水平自适应阈值：不同椅子/安装/环境噪声差别很大，
            # 写死阈值必然一边误报一边漏报。
            self._noise_rms = max(math.sqrt(self._motion_sq), 0.0015)
            self._noise_gyro = max(self._gyro_mean, 0.2)
        elif self.state == STATE_EMPTY:
            # 空椅时慢速自适应（温漂/轻微挪动），已在动就不跟
            a = cfg.baseline_alpha
            if math.sqrt(self._motion_sq) < self._motion_off():
                self._g_ref = tuple((1 - a) * g + a * c for g, c in zip(self._g_ref, acc))

        # --- 呼吸：沿重力方向的分量序列（椅背/座板受压的微起伏）
        if self._g_ref is not None and not learning:
            gnorm = math.sqrt(sum(v * v for v in self._g_ref))
            if gnorm > 1e-6:
                proj = sum(c * g / gnorm for c, g in zip(acc, self._g_ref))
                self._grav_samples.append((t, proj))
        if t - self._last_breath_calc >= cfg.breathing_recalc_s:
            self._last_breath_calc = t
            self._breath = self._detect_breathing()

        # 呼吸噪声底：只用"明显低于配置门槛"的幅度来学习。
        # 否则会出现自我掩蔽：人坐直不靠椅背时，真实呼吸幅度被当成噪声学进底噪，
        # 门槛被抬高到超过呼吸本身 → 永远检不出人。
        if (self._breath[1] > 0 and self.state in (STATE_EMPTY, STATE_UNKNOWN)
                and self._breath[1] < cfg.breathing_min_amp_g):
            a = 0.05
            self._noise_breath = (1 - a) * self._noise_breath + a * self._breath[1]

        # --- 汇总特征
        tilt_deg = self._angle_to_ref()
        tilt_axis = (0.0, 0.0, 0.0)
        if self._g_ref is not None:
            tilt_axis = tuple(c - g for c, g in zip(acc, self._g_ref))
        breath_hz, breath_amp, breath_ok = self._breath
        confidence = self._confidence(tilt_deg, breath_ok)

        seconds_in_state = t - self._state_since
        self.features = Features(
            tilt_deg=tilt_deg,
            tilt_axis=tilt_axis,  # type: ignore[arg-type]
            motion_rms=math.sqrt(self._motion_sq),
            gyro_mean=self._gyro_mean,
            breath_amp=breath_amp,
            breath_hz=breath_hz,
            breath_ok=breath_ok,
            impact=self._impact if t - self._impact_t < 3.0 else 0.0,
            seconds_in_state=max(0.0, seconds_in_state),
            confidence=confidence,
        )

        # --- 状态机
        if learning:
            self.state = STATE_UNKNOWN
            self._state_since = t
            return self.state

        recent_impact = (t - self._impact_t) < 1.5
        evidence_sit = self._evidence_occupied(tilt_deg, breath_ok, recent_impact)
        evidence_empty = self._evidence_empty(tilt_deg, breath_ok)

        # 证据累计：单帧毛刺（空椅噪声偶发超阈值）不应该把计时清零，
        # 只有证据持续缺失超过 hold 秒才归零。
        dt = self._last_dt
        hold = cfg.evidence_hold_s
        if evidence_sit:
            self._sit_accum += dt
            self._sit_last_t = t
        elif self._sit_last_t is None or t - self._sit_last_t > hold:
            self._sit_accum = 0.0
        if evidence_empty:
            self._empty_accum += dt
            self._empty_last_t = t
        elif self._empty_last_t is None or t - self._empty_last_t > hold:
            self._empty_accum = 0.0

        if self.state in (STATE_UNKNOWN, STATE_EMPTY):
            fast = recent_impact and tilt_deg >= self.cfg.tilt_off_deg
            if fast or self._sit_accum >= cfg.enter_dwell_s:
                self._set_state(STATE_OCCUPIED, t)
            elif self.state == STATE_UNKNOWN and self._empty_accum >= cfg.enter_dwell_s:
                # 自学习结束、椅子一直空着 → 明确报"空"（否则界面会一直显示"未知"）
                self._set_state(STATE_EMPTY, t)
        else:  # OCCUPIED
            if self._empty_accum >= cfg.exit_dwell_s:
                self._set_state(STATE_EMPTY, t)
            elif (recent_impact and not breath_ok
                  and self._empty_accum >= cfg.quick_exit_s):
                # 起身瞬态：倾角已回落且无呼吸 → 可提前判空
                self._set_state(STATE_EMPTY, t)

        return self.state

    def heartbeat(self, now: float) -> str:
        """没有新数据时调用（例如 BLE 掉线），超时则进入 UNKNOWN。"""
        if self.state != STATE_UNKNOWN and now - self._last_update_t > self.cfg.stale_gap_s:
            self.state = STATE_UNKNOWN
            self._state_since = now
        return self.state

    # ---------------------------------------------------------------- 自适应阈值
    def _motion_on(self) -> float:
        """进入"有运动"的阈值：不小于配置值，且至少是空椅噪声的 2.5 倍。"""
        return max(self.cfg.motion_on_g, self._noise_rms * 2.5)

    def _motion_off(self) -> float:
        """"静止"阈值：空椅噪声的 1.6 倍（留够余量，否则永远退不出"有人"）。"""
        return max(self.cfg.motion_off_g, self._noise_rms * 1.6)

    def _breath_min_amp(self) -> float:
        """呼吸幅度门槛：配置值与噪声底的 2.2 倍取大，最多放宽到配置值的 2.2 倍
        （上限防止噪声底把真实呼吸掩蔽掉）。"""
        adaptive = min(self._noise_breath * 2.2, self.cfg.breathing_min_amp_g * 2.2)
        return max(self.cfg.breathing_min_amp_g, adaptive)

    # ---------------------------------------------------------------- 内部
    def _set_state(self, state: str, t: float) -> None:
        if state != self.state:
            self.state = state
            self._state_since = t
            self._sit_accum = 0.0
            self._empty_accum = 0.0
            self._sit_last_t = None
            self._empty_last_t = None

    def _angle_to_ref(self) -> float:
        if self._g_ref is None or self._acc_ema is None:
            return 0.0
        a, b = self._acc_ema, self._g_ref
        na = math.sqrt(sum(v * v for v in a))
        nb = math.sqrt(sum(v * v for v in b))
        if na < 1e-6 or nb < 1e-6:
            return 0.0
        cos = max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b)) / (na * nb)))
        return math.degrees(math.acos(cos))

    def _evidence_occupied(self, tilt_deg: float, breath_ok: bool, recent_impact: bool) -> bool:
        cfg = self.cfg
        motion = math.sqrt(self._motion_sq)
        if tilt_deg >= cfg.tilt_on_deg:
            return True
        # 倾角不大：靠"呼吸 + 高于空椅噪声底的运动"判定（坐直不靠椅背的典型场景）
        if breath_ok and (tilt_deg >= cfg.tilt_off_deg or motion >= self._motion_off()):
            return True
        if breath_ok and self._gyro_mean >= cfg.gyro_on_dps * 0.5:
            return True
        if recent_impact and tilt_deg >= cfg.tilt_off_deg:
            return True
        if (motion >= self._motion_on() and self._gyro_mean >= cfg.gyro_on_dps
                and tilt_deg >= cfg.tilt_off_deg):
            return True
        return False

    def _evidence_empty(self, tilt_deg: float, breath_ok: bool) -> bool:
        cfg = self.cfg
        motion = math.sqrt(self._motion_sq)
        # 角速度门槛同样按学习到的噪声底放宽，否则空椅自身噪声会间歇性超阈
        gyro_limit = max(cfg.gyro_on_dps * 0.6, self._noise_gyro * 2.0)
        return (tilt_deg < cfg.tilt_off_deg and motion < self._motion_off()
                and not breath_ok and self._gyro_mean < gyro_limit)

    def _detect_breathing(self) -> Tuple[float, float, bool]:
        """在重力方向分量上找 0.15–0.55 Hz 的周期成分。"""
        cfg = self.cfg
        window = [v for (t, v) in self._grav_samples if t >= self._last_update_t - cfg.breathing_window_s]
        if len(window) < int(cfg.breathing_window_s * self._fs * 0.5):
            return 0.0, 0.0, False
        freq, power, median_power = _goertzel_band_peak(
            window, self._fs, cfg.breathing_min_hz, cfg.breathing_max_hz)
        if power <= 0:
            return freq, 0.0, False
        n = len(window)
        amp = 2.0 * math.sqrt(power) / n
        periodic = median_power <= 0 or power >= cfg.breathing_peak_ratio ** 2 * median_power
        ok = amp >= self._breath_min_amp() and periodic
        return freq, amp, ok

    def _confidence(self, tilt_deg: float, breath_ok: bool) -> float:
        cfg = self.cfg
        score = min(1.0, tilt_deg / max(cfg.tilt_on_deg, 1e-6)) * 0.6
        score += min(1.0, math.sqrt(self._motion_sq) / max(self._motion_on(), 1e-6)) * 0.25
        if breath_ok:
            score = min(1.0, score + 0.35)
        return max(0.0, min(1.0, score))
