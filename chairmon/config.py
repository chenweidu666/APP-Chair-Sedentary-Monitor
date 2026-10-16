"""应用配置（~/.config/chair-monitor/config.json）。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

_CFG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "chair-monitor"
_CFG_FILE = _CFG_DIR / "config.json"


@dataclass
class AppConfig:
    # 设备
    address: str = ""                 # 留空 = 启动时扫描 WT/BWT 设备
    mount: str = "backrest"           # backrest | seat
    # 检测参数覆盖（留空用 chairmon.model 的预设/自适应值）
    overrides: Dict[str, Any] = field(default_factory=dict)
    # 久坐策略
    sitting_limit_min: float = 45.0
    repeat_min: float = 15.0
    standing_reset_min: float = 2.0
    notify: bool = True
    notification_urgency: str = "normal"   # low | normal | critical
    # 采集
    poll_battery_min: float = 5.0     # 每 N 分钟读一次电量
    low_battery_percent: int = 20

    def save(self) -> None:
        _CFG_DIR.mkdir(parents=True, exist_ok=True)
        with open(_CFG_FILE, "w") as fh:
            json.dump(asdict(self), fh, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls) -> "AppConfig":
        cfg = cls()
        try:
            with open(_CFG_FILE) as fh:
                data = json.load(fh)
            for key, value in data.items():
                if hasattr(cfg, key):
                    setattr(cfg, key, value)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return cfg


def config_path() -> Path:
    return _CFG_FILE
