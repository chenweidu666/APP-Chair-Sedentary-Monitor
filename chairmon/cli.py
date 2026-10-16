"""命令行：无界面运行、抓数据调参、连通性检查。

    chair-monitor run              # 常驻检测（systemd 用户服务用的就是它）
    chair-monitor tray             # 系统托盘界面
    chair-monitor sim              # 合成数据演示（无需硬件）
    chair-monitor status           # 连一次，打印电量/姿态/检测结果
    chair-monitor record --label empty --seconds 60 --csv empty.csv   # 采带标签数据用于调参
    chair-monitor tune             # 用采集的带标签数据推荐阈值
    chair-monitor mark-empty       # 把当前姿态记为"空椅"基线（挪动过传感器后跑）
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
import time
from typing import List, Optional

from .config import AppConfig, config_path
from .monitor import ChairMonitor
from .occupancy import OccupancyDetector
from .model import config_for
from .sedentary import SedentaryConfig, SedentaryTracker
from .synth import SyntheticChair


# ---------------------------------------------------------------- run / sim
def cmd_run(args) -> int:
    app = AppConfig.load()
    if args.mount:
        app.mount = args.mount
    if args.addr:
        app.address = args.addr
    mon = ChairMonitor(app)
    try:
        asyncio.run(mon.run())
    except KeyboardInterrupt:
        print()
    return 0


def cmd_sim(args) -> int:
    """用合成场景跑一遍，打印状态时间线（不开蓝牙）。"""
    from .synth import replay

    scenario = _parse_scenario(args.scenario)
    det = OccupancyDetector(config_for(args.mount))
    gen = SyntheticChair(fs=args.fs)
    changes = replay(gen.stream(scenario), det)
    print(f"合成数据回放（mount={args.mount}）：")
    for t, state in changes:
        print(f"  {t:7.1f}s  →  {state}")
    print(f"最终状态 {det.state}，倾角 {det.features.tilt_deg:.2f}°，"
          f"呼吸 {det.features.breath_hz:.2f} Hz / {det.features.breath_amp:.5f} g，"
          f"运动 {det.features.motion_rms:.5f} g")
    return 0


def _parse_scenario(spec: str):
    out = []
    for part in spec.split(","):
        name, _, dur = part.partition(":")
        out.append((name.strip(), float(dur or 30)))
    return out


# ---------------------------------------------------------------- status
def cmd_status(args) -> int:
    from wt9011.client import WitClient

    async def job():
        client = WitClient(address=args.addr or None, on_status=lambda m: print(f"  · {m}"))
        await client.connect(args.addr or None)
        try:
            info = await client.read_device_info()
            bat = info.get("battery", {})
            print(f"设备      : {client.address}")
            if bat:
                print(f"电量      : {bat['percent']}%  ({bat['voltage']:.2f} V)")
            print(f"版本/温度 : {info.get('version', '—')} / "
                  f"{info.get('temperature', '—')} ℃")
            det = OccupancyDetector(config_for(args.mount))
            n = 0
            print("检测中（6 秒，看倾角/呼吸/运动特征）…")
            t0 = time.time()
            async for s in client.stream():
                det.update(s)
                n += 1
                if time.time() - t0 > 6:
                    break
            f = det.features
            print(f"状态      : {det.state}（置信 {f.confidence:.2f}）")
            print(f"倾角偏移  : {f.tilt_deg:.2f}°   运动 RMS {f.motion_rms:.5f} g   "
                  f"角速度 {f.gyro_mean:.2f} °/s")
            print(f"呼吸      : {'检出' if f.breath_ok else '未检出'} "
                  f"{f.breath_hz:.2f} Hz / {f.breath_amp:.5f} g")
            print(f"阈值参考  : 运动进入 {det._motion_on():.5f} / 退出 {det._motion_off():.5f} g，"
                  f"呼吸门槛 {det._breath_min_amp():.5f} g（噪声底 RMS {det._noise_rms:.5f} g）")
        finally:
            await client.disconnect()

    asyncio.run(job())
    return 0


# ---------------------------------------------------------------- record / tune
CSV_FIELDS = ["t", "label", "ax", "ay", "az", "gx", "gy", "gz", "tilt_deg", "motion_rms",
              "gyro_mean", "breath_hz", "breath_amp", "breath_ok", "state"]


def cmd_record(args) -> int:
    """采一段带标签的数据（label=empty/upright/leaning/...），用于事后调阈值。"""
    from wt9011.client import WitClient

    det = OccupancyDetector(config_for(args.mount))

    async def job():
        client = WitClient(address=args.addr or None, on_status=lambda m: print(f"  · {m}"))
        await client.connect(args.addr or None)
        try:
            fh = open(args.csv, "w", newline="")
            writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
            writer.writeheader()
            print(f"开始采集 {args.seconds:.0f} s，标签={args.label} → {args.csv}")
            t0 = time.time()
            async for s in client.stream():
                state = det.update(s)
                f = det.features
                writer.writerow({"t": round(s.t - t0, 3), "label": args.label,
                                 "ax": s.ax, "ay": s.ay, "az": s.az,
                                 "gx": s.gx, "gy": s.gy, "gz": s.gz,
                                 "tilt_deg": round(f.tilt_deg, 3),
                                 "motion_rms": round(f.motion_rms, 5),
                                 "gyro_mean": round(f.gyro_mean, 3),
                                 "breath_hz": round(f.breath_hz, 3),
                                 "breath_amp": round(f.breath_amp, 5),
                                 "breath_ok": int(f.breath_ok), "state": state})
                if time.time() - t0 >= args.seconds:
                    break
            fh.close()
            print(f"完成：{args.csv}（空椅/坐姿各采一段，再用 chair-monitor tune 推荐阈值）")
        finally:
            await client.disconnect()

    asyncio.run(job())
    return 0


def cmd_tune(args) -> int:
    """用带标签数据推荐阈值（简单统计：feature 在 empty 与 occupied 上的分位数）。"""
    rows = list(csv.DictReader(open(args.csv)))
    if not rows:
        print("CSV 是空的")
        return 1
    groups = {}
    for r in rows:
        groups.setdefault(r["label"], []).append(r)

    def pct(values, p):
        values = sorted(values)
        return values[min(len(values) - 1, int(len(values) * p))]

    print(f"各标签样本数：" + "，".join(f"{k}={len(v)}" for k, v in groups.items()))
    for feature in ("tilt_deg", "motion_rms", "gyro_mean", "breath_amp"):
        line = [f"{feature:11s}"]
        for label, group in groups.items():
            vals = [float(r[feature]) for r in group]
            line.append(f"{label}: 中位 {pct(vals, 0.5):.3f} p95 {pct(vals, 0.95):.3f}")
        print("  " + " | ".join(line))
    empties = groups.get("empty", [])
    if empties:
        print("\n建议（空椅 p95 → 门槛取 1.5~2 倍，倾角取 95 分的 2~3 倍）：")
        print(f"  motion_on_g ≈ {pct([float(r['motion_rms']) for r in empties], 0.95) * 2:.4f}"
              f"   motion_off_g ≈ {pct([float(r['motion_rms']) for r in empties], 0.95) * 1.3:.4f}")
        print(f"  breathing_min_amp_g ≈ "
              f"{pct([float(r['breath_amp']) for r in empties], 0.95) * 2:.5f}")
        print(f"  gyro_on_dps ≈ {pct([float(r['gyro_mean']) for r in empties], 0.95) * 2:.2f}")
    return 0


def cmd_mark_empty(args) -> int:
    """把当前姿态记为「空椅」基线（安装/挪动传感器后跑一次）。"""
    app = AppConfig.load()
    if args.addr:
        app.address = args.addr
    mon = ChairMonitor(app)

    async def job():
        await mon._connect(simulator=False)   # noqa: SLF001
        print("请确认椅子现在是空的（无人），3 秒后重建基线…")
        await asyncio.sleep(3)
        mon.mark_empty_now()
        print("完成。")
        await mon._safe_disconnect()          # noqa: SLF001

    asyncio.run(job())
    return 0


def cmd_tray(args) -> int:
    from .tray import run_tray
    return run_tray(args)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="chair-monitor", description="椅子久坐检测（BLE 5.0 姿态传感器）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--addr", help="设备 MAC（省略则用配置或自动扫描）")
        p.add_argument("--mount", choices=["backrest", "seat"], help="安装位置（默认取配置）")
        return p

    p = common(sub.add_parser("run", help="常驻检测（自动连接/自动重连/久坐提醒）"))
    p.set_defaults(func=cmd_run)

    p = common(sub.add_parser("tray", help="系统托盘界面"))
    p.set_defaults(func=cmd_tray)

    p = sub.add_parser("sim", help="合成数据演示（无需硬件）")
    p.add_argument("--mount", choices=["backrest", "seat"], default="backrest")
    p.add_argument("--fs", type=float, default=10.0)
    p.add_argument("--scenario", default="empty:25,sit_impact:1.2,leaning:90,empty:60",
                   help="场景序列，如 empty:25,leaning:60,empty:60")
    p.set_defaults(func=cmd_sim)

    p = common(sub.add_parser("status", help="连一次并打印检测特征（排查用）"))
    p.set_defaults(func=cmd_status)

    p = common(sub.add_parser("record", help="采带标签数据用于调参"))
    p.add_argument("--label", required=True, help="标签：empty / upright / leaning / ...")
    p.add_argument("--seconds", type=float, default=60)
    p.add_argument("--csv", default="chair_data.csv")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("tune", help="用带标签数据推荐阈值")
    p.add_argument("csv")
    p.set_defaults(func=cmd_tune)

    p = common(sub.add_parser("mark-empty", help="把当前姿态记为「空椅」基线"))
    p.set_defaults(func=cmd_mark_empty)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
