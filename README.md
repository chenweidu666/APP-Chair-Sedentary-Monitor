# 椅子久坐检测（Linux · BLE 姿态传感器）

[English](#english) | **简体中文**

用一只贴在你椅子上的 **BLE 5.0 姿态传感器**（维特智能 WT9011DCL-BT50 / BWT901BLE5.0 系列）
做**久坐检测**：判断"椅子上有没有人"，累计连续久坐时间，到点弹桌面通知提醒你起来活动。

- 仓库：<https://github.com/chenweidu666/APP-Chair-Sedentary-Monitor>
- 协议/上位机基础库：[APP-WT9011DCL-BT50-Linux](https://github.com/chenweidu666/APP-WT9011DCL-BT50-Linux)
- 许可：MIT

## 它解决什么

| 能力 | 说明 |
|---|---|
| **判断有没有人坐** | 倾角偏移 + 呼吸微动 + 落座/起身瞬态，三判据融合（见 [原理](docs/久坐检测原理.md)） |
| 久坐计时与提醒 | 连续 45 分钟 → 提醒；之后每 15 分钟再提醒；起身累计 2 分钟才清零 |
| 电量显示 | 托盘/日志显示电量百分比与电压，低电量提醒 |
| **开机自动连接** | systemd 用户服务 + 内部重连：开机、掉线、传感器断电重启都自动恢复 |
| 无界面也能跑 | 通知走 `notify-send`，无桌面环境自动退化为写日志 |
| 可调参 | 采带标签数据 → `tune` 推荐阈值；支持椅背/座板两种安装预设 |

```bash
chair-monitor run        # 常驻检测（systemd 服务就是它）
chair-monitor tray       # 托盘界面：状态 / 已坐时长 / 电量 / 暂停 / 清零
chair-monitor sim        # 无硬件演示：合成数据跑一遍状态机
```

## 快速开始

```bash
git clone https://github.com/chenweidu666/APP-Chair-Sedentary-Monitor.git
cd APP-Chair-Sedentary-Monitor
pip install -r requirements.txt
./install.sh                # 安装 + systemd 用户服务（开机自动连接）

chair-monitor status        # 连通性自检：电量 / 版本 / 倾角 / 呼吸 / 运动特征
chair-monitor mark-empty    # 椅子空着时做一次基线校准
journalctl --user -u chair-monitor -f     # 看实时状态
```

安装细节与排错见 [docs/安装与开机自启.md](docs/安装与开机自启.md)。

## 安装位置（很重要）

- **椅背**：贴在椅背**背面中上部**，靠近框架/变形明显处，硬质固定；
- **座板**：贴在座板**下方中央**（受压变形最大处）；
- 别贴在软垫表面、别让线材绷紧拉扯 —— 传感器一旦能相对椅子晃动，判据就废了。

⚠️ **贴椅背要注意人体挡信号**：人坐在电脑与传感器之间时，2.4 GHz 会被人体吸收，
RSSI 可能掉 10–20 dB。对策：USB 蓝牙棒用延长线挪到椅子侧后方，或把回传速率降到 5–10 Hz。

## 工作原理（一句话版）

空椅只有噪声；人坐下后椅背/座板受压 → **倾角稳定偏移**、**呼吸带来 0.2–0.4 Hz 微起伏**、
**落座瞬间有冲击**。三者组合 + 迟滞状态机 + 20 s 自学习空椅基线（含噪声底自适应）即可稳定判别。
细节、阈值、边界（放重物会误判、硬木椅灵敏度下降）都在
[docs/久坐检测原理.md](docs/久坐检测原理.md)。

## 项目结构

```
chairmon/
├── model.py      状态/配置/安装位置预设
├── occupancy.py  占用检测：特征提取（倾角/运动/呼吸 Goertzel）+ 迟滞状态机  ← 核心算法
├── sedentary.py  久坐计时与提醒策略
├── monitor.py    BLE 接入（复用 wt9011 库）、自动重连、电量轮询
├── notifier.py   桌面通知（notify-send，无桌面退化为日志）
├── tray.py       PySide6 系统托盘
├── cli.py        run / tray / sim / status / record / tune / mark-empty
└── synth.py      合成 IMU 数据（离线开发与单测）
tests/            17 项算法单测：空椅不误报、坐直靠呼吸检出、起身回落、掉线不误判…
docs/             检测原理、安装与开机自启
```

## 验证情况

```bash
python3 -m unittest discover -s tests -v      # 17/17 通过
python3 -m chairmon.cli sim                   # 合成场景状态机时间线
chair-monitor status --addr <MAC>             # 真机：100% / 4.27 V，实测噪声 RMS 0.001 g
```

- ✅ 算法：17 项单测（含"空椅被碰不误报""坐直不靠椅背靠呼吸检出""起身 45 s 内回落"
  "BLE 掉线转 UNKNOWN 且不清零久坐"）
- ✅ 真机：连接、电量、版本、温度、特征提取全部正常
- ⏳ **待实测**：装到椅子上后的误报/漏报率 —— 需要按
  [调参流程](docs/久坐检测原理.md#6-调参流程上真机后必做) 采两段带标签数据后确定阈值

## 局限

1. 纯 IMU 无法区分"人"与"重物"（放包会判有人）；要彻底解决需加 FSR 压力片/称重传感器。
2. 硬木椅（几乎不变形）灵敏度下降；完全不靠椅背且极度静止时，靠呼吸判据检出较慢（20–40 s）。
3. 风扇直吹传感器可能误报，需避开气流或调高呼吸门槛。

---

## English

A **sedentary/occupancy monitor** for your chair, using a **BLE 5.0 IMU**
(WitMotion WT9011DCL-BT50 or the BWT901BLE5.0 family) mounted on the backrest or under the seat.

**How "someone is sitting" is decided** — three fused criteria (details in
[docs/久坐检测原理.md](docs/久坐检测原理.md)):

| Signal | Empty chair | Someone sitting | Role |
|---|---|---|---|
| Tilt offset Δθ | drift <0.5° | 2–10° (backrest/seat flexes under load) | primary, fastest |
| Respiration (0.15–0.55 Hz band, Goertzel) | none | ~0.2–0.4 Hz, ~0.005 g | catches "sitting upright, not leaning" |
| Sit-down / stand-up transient | none | impact >0.06 g + gyro spike | makes transitions prompt |

Plus a 20 s self-calibration of the empty-chair baseline and **adaptive noise floors**, dwell
times with tolerance against single-frame glitches, and hysteresis (enter ≥2 s, exit ≥30 s).

Features: occupancy state, continuous sitting timer with 45 min / 15 min reminders, 2 min
stand-up reset, battery display with low-battery notification, **auto-connect on boot**
(systemd user service + internal reconnect), headless operation with desktop notifications,
and a data-driven tuning workflow (`record --label …` + `tune`).

```bash
pip install -r requirements.txt && ./install.sh   # headless + autostart
chair-monitor sim                                  # run the state machine on synthetic data
python3 -m unittest discover -s tests -v           # 17 algorithm tests
```

Known limits: a pure IMU cannot tell a person from a heavy object on the chair (an FSR/load-cell
would fix that); rigid wooden chairs give a weak tilt signal; poor mounting (sensor can wobble
relative to the chair) breaks the detection. Body-blocking of 2.4 GHz is a real concern when the
sensor is behind the backrest — see the mounting notes above.

License: MIT. Independent third-party project, not affiliated with WitMotion.

---
